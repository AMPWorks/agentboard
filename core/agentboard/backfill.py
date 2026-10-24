"""Import past Claude and Codex transcripts into the AgentBoard database."""

import json
import os
from datetime import datetime, timedelta, timezone
import time
from pathlib import Path

from . import db
from . import diag
from . import notes
from .hook import EXIT_COMMANDS, clean_prompt_text, is_untrusted_daemon_pid, normalize_branch
from .reconcile import is_pid_alive

HEAD_SCAN_MAX_LINES = 500
HEAD_SCAN_MAX_BYTES = 2 * 1024 * 1024
TAIL_SCAN_MAX_BYTES = 8 * 1024 * 1024
# Once the tail scan knows everything but a custom title, it keeps looking
# for one this much further (a custom title already stored is kept anyway,
# since backfill never replaces a custom title with an ai-title).
CUSTOM_TITLE_SCAN_BYTES = 2 * 1024 * 1024
TAIL_CHUNK_SIZE = 65536

def claude_projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def codex_sessions_dir() -> Path:
    return Path.home() / ".codex" / "sessions"


MAX_TEXT_CHARS = 4000


def _truncate(text):
    if text is None:
        return None
    text = str(text)
    return text[:MAX_TEXT_CHARS] if len(text) > MAX_TEXT_CHARS else text


def _first_line(text, limit=120):
    if not text:
        return None
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line[:limit]


def iter_tail_lines(path, max_bytes=TAIL_SCAN_MAX_BYTES):
    """Yield lines from the end of the file, most-recent first.

    Bounded by max_bytes so a single huge file cannot be scanned in full.
    """
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        pos = fh.tell()
        read_total = 0
        tail = b""
        while pos > 0 and read_total < max_bytes:
            read_size = min(TAIL_CHUNK_SIZE, pos)
            pos -= read_size
            fh.seek(pos)
            chunk = fh.read(read_size)
            read_total += read_size
            tail = chunk + tail
            parts = tail.split(b"\n")
            tail = parts[0]
            for line in reversed(parts[1:]):
                if line:
                    yield line.decode("utf-8", "replace")
        if tail and pos == 0:
            yield tail.decode("utf-8", "replace")


def iter_head_lines(path, max_lines=HEAD_SCAN_MAX_LINES, max_bytes=HEAD_SCAN_MAX_BYTES):
    read_bytes = 0
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            if i >= max_lines or read_bytes >= max_bytes:
                break
            read_bytes += len(line)
            yield line


def _within_days(path, days):
    if days is None:
        return True
    cutoff = datetime.now().timestamp() - days * 86400
    try:
        return os.path.getmtime(path) >= cutoff
    except OSError:
        return False


# -- Claude -----------------------------------------------------------------

TITLE_TYPES = {"summary": "summary", "custom-title": "customTitle", "ai-title": "aiTitle"}

# Where a session's title came from (sessions.title_source). Precedence, top
# wins: a user's own /rename ("custom"); the newest of an LLM retitle
# ("generated") or `agentboard title` ("cli"); Claude Code's own summary
# ("ai"/"summary"); the first prompt's first line ("fallback").
TITLE_SOURCES = {"summary": "summary", "custom-title": "custom", "ai-title": "ai"}
CUSTOM_TITLE_MARKER = '"custom-title"'


SYNTHETIC_USER_TAGS = (
    "<local-command-caveat>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<bash-stdout>",
)

# Prompts Claude Code injected itself are not the user's prompts either:
# `prompt_injected_prefixes` from config.json, set once per backfill run
# (set_injected_prefixes), defaulting to notes.DEFAULT_INJECTED_PREFIXES.
_injected_prefixes = notes.DEFAULT_INJECTED_PREFIXES


def set_injected_prefixes(prefixes) -> None:
    global _injected_prefixes
    _injected_prefixes = tuple(prefixes)


def load_injected_prefixes() -> None:
    """Read `prompt_injected_prefixes` from config.json for this run."""
    set_injected_prefixes(notes.injected_prefixes(notes.load_config(db.home_dir())))


def _is_synthetic(text) -> bool:
    if text.startswith(SYNTHETIC_USER_TAGS):
        return True
    stripped = text.lstrip()
    return any(stripped.startswith(prefix) for prefix in _injected_prefixes)


def _claude_user_text(message_content):
    """Return plain prompt text for a 'user' entry, or None if not real."""
    if isinstance(message_content, str):
        if _is_synthetic(message_content):
            return None
        return clean_prompt_text(message_content)
    if isinstance(message_content, list):
        kinds = {block.get("type") for block in message_content if isinstance(block, dict)}
        if kinds and kinds <= {"tool_result"}:
            return None
        texts = [
            block.get("text")
            for block in message_content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        text = "\n".join(t for t in texts if t) or None
        if text and _is_synthetic(text):
            return None
        return clean_prompt_text(text)
    return None


def _claude_assistant_text(message_content):
    if not isinstance(message_content, list):
        return None
    texts = [
        block.get("text")
        for block in message_content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n".join(t for t in texts if t) or None


TASK_OPEN_STATUSES = {"pending", "in_progress"}


# Lines worth a json.loads in the todo scan: an assistant tool_use of one
# of the todo/task tools. Every other line (most of a long transcript) is
# skipped on a substring test.
TODO_SCAN_TOOL_MARKERS = (b"TodoWrite", b"TaskCreate", b"TaskUpdate", b"TaskStop")


def empty_todo_scan_state() -> dict:
    return {"offset": 0, "task_status": {}, "todowrite_open": None, "last_mechanism": None}


def _valid_todo_scan_state(state):
    """A clean copy of a stored scan state, or a fresh one when it is
    missing or malformed (the scan then restarts from the beginning)."""
    if not isinstance(state, dict):
        return empty_todo_scan_state()
    offset = state.get("offset")
    task_status = state.get("task_status")
    todowrite_open = state.get("todowrite_open")
    last_mechanism = state.get("last_mechanism")
    if (
        isinstance(offset, bool) or not isinstance(offset, int) or offset < 0
        or not isinstance(task_status, dict)
        or not (todowrite_open is None or isinstance(todowrite_open, int))
        or last_mechanism not in (None, "todowrite", "task")
    ):
        return empty_todo_scan_state()
    return {
        "offset": offset,
        "task_status": {str(k): v for k, v in task_status.items()},
        "todowrite_open": todowrite_open,
        "last_mechanism": last_mechanism,
    }


def _apply_todo_tool_use(state, name, tool_input) -> None:
    if name == "TodoWrite":
        todos = tool_input.get("todos")
        if isinstance(todos, list):
            state["todowrite_open"] = sum(
                1 for t in todos
                if isinstance(t, dict) and t.get("status") != "completed"
            )
            state["last_mechanism"] = "todowrite"
    elif name == "TaskCreate":
        tasks = tool_input.get("tasks")
        if isinstance(tasks, str):
            try:
                tasks = json.loads(tasks)
            except ValueError:
                tasks = None
        if isinstance(tasks, list):
            state["task_status"] = {str(i + 1): "pending" for i in range(len(tasks))}
            state["last_mechanism"] = "task"
    elif name == "TaskUpdate":
        task_id = tool_input.get("taskId") or tool_input.get("task_id")
        status = tool_input.get("status")
        if task_id is not None and status:
            state["task_status"][str(task_id)] = status
            state["last_mechanism"] = "task"
    elif name == "TaskStop":
        task_id = tool_input.get("task_id") or tool_input.get("taskId")
        if task_id is not None:
            state["task_status"][str(task_id)] = "stopped"
            state["last_mechanism"] = "task"


def _todos_open_from_state(state):
    if state["last_mechanism"] == "todowrite":
        return state["todowrite_open"]
    if state["last_mechanism"] == "task":
        return sum(1 for status in state["task_status"].values() if status in TASK_OPEN_STATUSES)
    return None


def _scan_claude_todos(path: Path, state=None):
    """Reconstruct the todo/task list state from a transcript and return
    (todos_open, new_state). todos_open is the count still open, or None
    if no list was ever created (NULL means "unknown/no list", which counts
    as no unfinished todos for auto-done purposes).

    Incremental: `state` is what the previous scan returned ({"offset",
    "task_status", "todowrite_open", "last_mechanism"}). The scan resumes at
    its byte offset when that still lies within the file just after a
    newline; otherwise (the file shrank or was rewritten) it restarts from
    the beginning with an empty state. It reads to the end, consuming only
    complete lines: a trailing partial line (Claude Code mid-write) is left
    for the next run, so the returned offset is the end of the last
    complete line. The file is read in binary, so offsets are bytes.

    TodoWrite is self-contained (every call carries the whole list).
    TaskCreate/TaskUpdate/TaskStop are deltas against task ids that are
    just the 1-based position in the most recent TaskCreate call -- there
    is no batch/list id, so a fresh TaskCreate replaces the active list.
    Whichever mechanism was touched most recently wins (sessions use one
    or the other, not both, but let's not assume).
    """
    state = _valid_todo_scan_state(state)
    with open(path, "rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        offset = state["offset"]
        resume = False
        if 0 < offset <= size:
            fh.seek(offset - 1)
            resume = fh.read(1) == b"\n"
        if not resume:
            state = empty_todo_scan_state()
            offset = 0
        fh.seek(offset)
        for line in fh:
            if not line.endswith(b"\n"):
                break
            offset += len(line)
            if b'"tool_use"' not in line or not any(
                marker in line for marker in TODO_SCAN_TOOL_MARKERS
            ):
                continue
            try:
                entry = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            if not isinstance(entry, dict) or entry.get("type") != "assistant":
                continue
            message = entry.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                tool_input = block.get("input")
                if isinstance(tool_input, dict):
                    _apply_todo_tool_use(state, block.get("name"), tool_input)
    state["offset"] = offset
    return _todos_open_from_state(state), state


def _compute_claude_todos_open(path: Path):
    """The open todo count from a full scan (see _scan_claude_todos)."""
    return _scan_claude_todos(path)[0]


def parse_claude_transcript(path: Path, todos_state=None) -> dict:
    """Extract session fields from one ~/.claude/projects/*/*.jsonl file.

    `todos_state` is the session's stored todo scan state (see
    _scan_claude_todos), so the todo count resumes where the last parse
    stopped; the result carries the new one as todos_scan_offset and
    todos_scan_state."""
    result = {
        "cwd": None,
        "branch": None,
        "title": None,
        "first_prompt": None,
        "last_prompt": None,
        "last_assistant": None,
        "started_at": None,
        "updated_at": None,
        "last_event": "Stop",
        "last_event_at": None,
        "transcript_path": str(path),
        "interactive": 1,
        "entrypoint": None,
        "userType": None,
        "todos_open": None,
        "title_source": None,
    }

    for raw in iter_head_lines(path):
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if result["started_at"] is None and entry.get("timestamp"):
            result["started_at"] = entry["timestamp"]
        if entry.get("cwd") and result["cwd"] is None:
            result["cwd"] = entry["cwd"]
        if entry.get("gitBranch") and result["branch"] is None:
            result["branch"] = normalize_branch(entry["gitBranch"])
        if entry.get("entrypoint") and result["entrypoint"] is None:
            result["entrypoint"] = entry["entrypoint"]
        if entry.get("userType") and result["userType"] is None:
            result["userType"] = entry["userType"]
        if entry.get("type") == "user" and not entry.get("isMeta") and result["first_prompt"] is None:
            text = _claude_user_text(entry.get("message", {}).get("content"))
            if text:
                result["first_prompt"] = text
        if result["first_prompt"] and result["cwd"] and result["started_at"]:
            break

    last_substantive_type = None
    tail_branch = None
    custom_title = None
    other_title = None
    other_source = None
    scanned = 0
    hunt_started = None
    for raw in iter_tail_lines(path):
        scanned += len(raw) + 1
        raw = raw.strip()
        if not raw:
            continue
        # Once everything else is known, the tail is scanned only for a
        # user's custom title, which beats any newer ai-title, and only for
        # CUSTOM_TITLE_SCAN_BYTES more: skip the JSON parse of every other
        # line.
        if (
            result["last_prompt"] is not None
            and result["last_assistant"] is not None
            and result["updated_at"] is not None
            and other_title is not None
        ):
            if hunt_started is None:
                hunt_started = scanned
            elif scanned - hunt_started > CUSTOM_TITLE_SCAN_BYTES:
                break
            if CUSTOM_TITLE_MARKER not in raw:
                continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue

        if result["updated_at"] is None and entry.get("timestamp"):
            result["updated_at"] = entry["timestamp"]
        if entry.get("cwd") and result["cwd"] is None:
            result["cwd"] = entry["cwd"]
        if entry.get("gitBranch") and tail_branch is None:
            tail_branch = normalize_branch(entry["gitBranch"])

        entry_type = entry.get("type")
        if entry_type in TITLE_TYPES:
            value = entry.get(TITLE_TYPES[entry_type])
            if value and isinstance(value, str):
                if entry_type == "custom-title":
                    if custom_title is None:
                        custom_title = value
                elif other_title is None:
                    other_title, other_source = value, TITLE_SOURCES[entry_type]

        if entry_type == "user" and not entry.get("isMeta"):
            text = _claude_user_text(entry.get("message", {}).get("content"))
            if text and result["last_prompt"] is None:
                result["last_prompt"] = text
                if last_substantive_type is None:
                    last_substantive_type = "user"
        elif entry_type == "assistant":
            text = _claude_assistant_text(entry.get("message", {}).get("content"))
            if text and result["last_assistant"] is None:
                result["last_assistant"] = text
                if last_substantive_type is None:
                    last_substantive_type = "assistant"

        if (
            result["last_prompt"] is not None
            and result["last_assistant"] is not None
            and custom_title is not None
            and result["updated_at"] is not None
        ):
            break

    if tail_branch:
        result["branch"] = tail_branch
    if custom_title:
        result["title"], result["title_source"] = custom_title, "custom"
    elif other_title:
        result["title"], result["title_source"] = other_title, other_source

    if last_substantive_type == "user":
        result["last_event"] = "UserPromptSubmit"
        result["last_event_at"] = result["updated_at"]
    else:
        result["last_event"] = "Stop"
        result["last_event_at"] = result["updated_at"]

    if not result["title"]:
        result["title"] = _first_line(result["first_prompt"])
        result["title_source"] = "fallback" if result["title"] else None

    if result["entrypoint"] == "sdk-cli":
        result["interactive"] = 0
    result["first_prompt"] = _truncate(result["first_prompt"])
    result["last_prompt"] = _truncate(result["last_prompt"])
    result["last_assistant"] = _truncate(result["last_assistant"])
    todos_open, scan_state = _scan_claude_todos(path, todos_state)
    result["todos_open"] = todos_open
    result["todos_scan_offset"] = scan_state.pop("offset")
    result["todos_scan_state"] = json.dumps(scan_state, sort_keys=True)
    return result


def iter_claude_transcripts(days):
    projects_dir = claude_projects_dir()
    if not projects_dir.is_dir():
        return
    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        for path in project_dir.glob("*.jsonl"):
            if "subagents" in path.parts:
                continue
            if not _within_days(path, days):
                continue
            session_id = path.stem
            yield session_id, path


# -- Codex --------------------------------------------------------------------

def _item_completed_text(payload):
    """Extract (role, text) from a newer-schema event_msg item_completed."""
    item = payload.get("item") or {}
    item_type = item.get("type")
    if item_type not in ("UserMessage", "AgentMessage"):
        return None, None
    texts = [
        block.get("text")
        for block in item.get("content", [])
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    text = "\n".join(t for t in texts if t) or None
    role = "user" if item_type == "UserMessage" else "agent"
    return role, text


def _compute_codex_todos_open(path: Path):
    """Count open (non-completed) steps in the most recent update_plan
    call, or None if the plan tool was never used. Self-contained each
    call (full plan list every time), unlike Claude's Task* family.
    """
    last_plan = None
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if entry.get("type") != "response_item":
                continue
            payload = entry.get("payload", {})
            if payload.get("type") != "function_call" or payload.get("name") != "update_plan":
                continue
            arguments = payload.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    continue
            if isinstance(arguments, dict):
                plan = arguments.get("plan")
                if isinstance(plan, list):
                    last_plan = plan

    if last_plan is None:
        return None
    return sum(
        1 for step in last_plan
        if isinstance(step, dict) and step.get("status") != "completed"
    )


def parse_codex_rollout(path: Path) -> dict:
    result = {
        "session_id": None,
        "cwd": None,
        "branch": None,
        "started_at": None,
        "updated_at": None,
        "first_prompt": None,
        "last_prompt": None,
        "last_assistant": None,
        "last_event": "Stop",
        "last_event_at": None,
        "transcript_path": str(path),
        "interactive": 1,
        "source": None,
        "todos_open": None,
    }

    for raw in iter_head_lines(path, max_lines=5):
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if entry.get("type") == "session_meta":
            payload = entry.get("payload", {})
            result["session_id"] = payload.get("id") or payload.get("session_id")
            result["cwd"] = payload.get("cwd")
            result["source"] = payload.get("source")
            result["started_at"] = payload.get("timestamp") or entry.get("timestamp")
            git = payload.get("git") or {}
            result["branch"] = normalize_branch(git.get("branch"))
            break

    last_substantive_type = None
    for raw in iter_tail_lines(path):
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue

        if result["updated_at"] is None and entry.get("timestamp"):
            result["updated_at"] = entry["timestamp"]

        if entry.get("type") != "event_msg":
            continue
        payload = entry.get("payload", {})
        ptype = payload.get("type")

        if ptype == "user_message" and result["last_prompt"] is None:
            cleaned = clean_prompt_text(payload.get("message"))
            if cleaned:
                result["last_prompt"] = cleaned
                if last_substantive_type is None:
                    last_substantive_type = "user_message"
        elif ptype == "agent_message" and result["last_assistant"] is None:
            result["last_assistant"] = payload.get("message")
            if last_substantive_type is None:
                last_substantive_type = "agent_message"
        elif ptype == "item_completed":
            role, text = _item_completed_text(payload)
            if role == "user" and text and result["last_prompt"] is None:
                cleaned = clean_prompt_text(text)
                if cleaned:
                    result["last_prompt"] = cleaned
                    if last_substantive_type is None:
                        last_substantive_type = "user_message"
            elif role == "agent" and text and result["last_assistant"] is None:
                result["last_assistant"] = text
                if last_substantive_type is None:
                    last_substantive_type = "agent_message"
        elif ptype in ("task_complete", "turn_aborted", "task_started"):
            if last_substantive_type is None:
                last_substantive_type = ptype

        if (
            result["last_prompt"] is not None
            and result["last_assistant"] is not None
            and result["updated_at"] is not None
        ):
            break

    if result["first_prompt"] is None:
        for raw in iter_head_lines(path, max_lines=HEAD_SCAN_MAX_LINES):
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if entry.get("type") != "event_msg":
                continue
            payload = entry["payload"]
            if payload.get("type") == "user_message":
                cleaned = clean_prompt_text(payload.get("message"))
                if cleaned:
                    result["first_prompt"] = cleaned
                    break
            if payload.get("type") == "item_completed":
                role, text = _item_completed_text(payload)
                if role == "user" and text:
                    cleaned = clean_prompt_text(text)
                    if cleaned:
                        result["first_prompt"] = cleaned
                        break

    if last_substantive_type in ("task_complete",):
        result["last_event"] = "Stop"
    elif last_substantive_type == "turn_aborted":
        result["last_event"] = "Interrupt"
    elif last_substantive_type in ("task_started", "user_message"):
        result["last_event"] = "UserPromptSubmit"
    else:
        result["last_event"] = "Stop"
    result["last_event_at"] = result["updated_at"]

    if result["source"] == "exec":
        result["interactive"] = 0

    result["first_prompt"] = _truncate(result["first_prompt"])
    result["last_prompt"] = _truncate(result["last_prompt"])
    result["last_assistant"] = _truncate(result["last_assistant"])
    result["todos_open"] = _compute_codex_todos_open(path)
    return result


def iter_codex_rollouts(days):
    sessions_dir = codex_sessions_dir()
    if not sessions_dir.is_dir():
        return
    for path in sessions_dir.glob("*/*/*/rollout-*.jsonl"):
        if not _within_days(path, days):
            continue
        yield path


# -- DB upsert ----------------------------------------------------------------

BACKFILL_COLUMNS = [
    "cwd", "branch", "title", "first_prompt", "last_prompt", "last_assistant",
    "started_at", "updated_at", "last_event", "last_event_at",
    "transcript_path", "interactive", "todos_open",
]

# todos_open is always backfill-authoritative, even on a hook-origin row:
# reconstructing it correctly (TaskUpdate/TaskStop are deltas against
# whatever TaskCreate/TodoWrite came before) needs the whole transcript,
# which only backfill's scan has (incrementally, from a stored byte offset)
# -- the hook only ever sees one PostToolUse payload at a time.
ALWAYS_OVERWRITE_COLUMNS = {"todos_open"}

# Where the incremental Claude todo scan stopped (see _scan_claude_todos).
TODO_SCAN_COLUMNS = ["todos_scan_offset", "todos_scan_state"]


def stored_todo_scan(conn, tool, session_id):
    """(stored offset, scan state to resume from) for a session, read with a
    plain SELECT before parsing (outside any transaction). The offset is
    what upsert_backfill_session compares against before writing a new
    one; the state is None when there is nothing to resume."""
    row = conn.execute(
        "SELECT todos_scan_offset, todos_scan_state FROM sessions WHERE tool=? AND session_id=?",
        (tool, session_id),
    ).fetchone()
    if row is None or row[0] is None:
        return None, None
    try:
        state = json.loads(row[1]) if row[1] else None
    except ValueError:
        state = None
    if not isinstance(state, dict):
        return row[0], None
    return row[0], dict(state, offset=row[0])


def _todo_columns(row, fields):
    """(write_todos, scan_columns) for an upsert: whether todos_open may be
    written, and the todo scan columns to write with it. A parse that
    resumed from a stored offset (fields["todos_scan_base"]) writes only if
    the row still holds that offset: a concurrent run that advanced it
    wins, and this one leaves the todo columns alone."""
    if "todos_scan_offset" not in fields:
        return True, []
    if row is not None and "todos_scan_base" in fields:
        if row["todos_scan_offset"] != fields["todos_scan_base"]:
            return False, []
    return True, list(TODO_SCAN_COLUMNS)


def _is_known_bad_value(col, value):
    """Legacy values written before the command-markup/HEAD-branch/exit fixes.

    Backfill is allowed to correct these even on a hook-origin row that's
    otherwise protected -- they're not real hook-captured data, just stale
    pre-fix junk that only backfill's richer transcript parsing can repair.
    """
    if value is None:
        return False
    if col == "branch":
        return value == "HEAD"
    if col in ("first_prompt", "last_prompt", "title"):
        if "<command-name>" in value or "<command-message>" in value:
            return True
        return value.strip().lower() in EXIT_COMMANDS
    return False


def _restores_interactive(col, current, new_value, values) -> bool:
    """True when a re-parse may flip a hook-origin row's interactive 0 back
    to 1 (a documented exception, like _is_known_bad_value): the hook marks
    a session that ended with no prompt as a helper (interactive=0), but if
    its hooks were installed mid-way the transcript can still show a real
    first prompt. The parse already sets 0 for an sdk-cli entrypoint (and
    a Codex exec rollout), so a parsed 1 with a first prompt is a real
    interactive session."""
    return (
        col == "interactive" and current == 0 and new_value == 1
        and bool(values.get("first_prompt"))
    )


# Title sources that only the user or an explicit retitle set, which a
# transcript re-parse must not undo: "custom" yields only to a newer custom
# title, "generated"/"cli" only to a custom one.
PROTECTED_TITLE_SOURCES = {"custom", "generated", "cli"}


def title_update(current_title, current_source, new_title, new_source):
    """(title, title_source) a re-parse should write, or None to keep.

    The hook never writes titles, so unlike the other columns a title may
    be replaced on a hook-origin row too (a documented exception, like
    _is_known_bad_value): by a parsed custom/ai/summary title that differs.
    A first-prompt fallback never replaces a real title (or a legacy one
    whose source is unknown); a custom title yields only to a different
    custom title; a generated or `agentboard title` title only to a custom
    one. A missing new source counts as "ai" (an older caller's title).
    """
    if not new_title:
        if current_title is not None and _is_known_bad_value("title", current_title):
            return None, None
        return None
    new_source = new_source or "ai"
    if current_title is None or _is_known_bad_value("title", current_title):
        return new_title, new_source
    if current_source == "custom":
        if new_source == "custom" and new_title != current_title:
            return new_title, new_source
        return None
    if current_source in PROTECTED_TITLE_SOURCES:
        return (new_title, new_source) if new_source == "custom" else None
    if new_source == "fallback":
        if current_source == "fallback" and new_title != current_title:
            return new_title, new_source
        return None
    if new_title != current_title or new_source != current_source:
        return new_title, new_source
    return None


def upsert_backfill_session(conn, tool, session_id, fields: dict):
    row = conn.execute(
        "SELECT * FROM sessions WHERE tool=? AND session_id=?", (tool, session_id)
    ).fetchone()

    values = {col: fields.get(col) for col in BACKFILL_COLUMNS}
    title_source = fields.get("title_source") or ("ai" if values["title"] else None)
    write_todos, scan_columns = _todo_columns(row, fields)

    if row is None:
        columns = (["tool", "session_id", "origin"] + BACKFILL_COLUMNS + ["title_source"]
                   + scan_columns)
        placeholders = ",".join("?" for _ in columns)
        params = ([tool, session_id, "backfill"] + [values[c] for c in BACKFILL_COLUMNS]
                  + [title_source] + [fields.get(c) for c in scan_columns])
        conn.execute(
            f"INSERT INTO sessions({','.join(columns)}) VALUES ({placeholders})",
            params,
        )
        return "inserted"

    title = title_update(row["title"], row["title_source"], values["title"], title_source)

    if row["origin"] == "hook":
        sets = []
        params = []
        if title is not None:
            sets += ["title=?", "title_source=?"]
            params += list(title)
        for col in BACKFILL_COLUMNS:
            if col == "title" or (col == "todos_open" and not write_todos):
                continue
            current = row[col]
            new_value = values[col]
            if col in ALWAYS_OVERWRITE_COLUMNS:
                sets.append(f"{col}=?")
                params.append(new_value)
            elif current is None:
                if new_value is not None:
                    sets.append(f"{col}=?")
                    params.append(new_value)
            elif _is_known_bad_value(col, current):
                sets.append(f"{col}=?")
                params.append(new_value)
            elif _restores_interactive(col, current, new_value, values):
                sets.append(f"{col}=?")
                params.append(new_value)
        for col in scan_columns:
            if row[col] != fields.get(col):
                sets.append(f"{col}=?")
                params.append(fields.get(col))
        if not sets:
            return "unchanged"
        params += [tool, session_id]
        conn.execute(
            f"UPDATE sessions SET {','.join(sets)} WHERE tool=? AND session_id=?",
            params,
        )
        return "filled"

    columns = [col for col in BACKFILL_COLUMNS
               if col != "title" and (col != "todos_open" or write_todos)]
    sets = [f"{col}=?" for col in columns + scan_columns]
    params = [values[col] for col in columns] + [fields.get(col) for col in scan_columns]
    if title is not None:
        sets += ["title=?", "title_source=?"]
        params += list(title)
    params += [tool, session_id]
    conn.execute(
        f"UPDATE sessions SET {','.join(sets)} WHERE tool=? AND session_id=?",
        params,
    )
    return "updated"


def correct_daemon_pid_rows(conn) -> int:
    """Null out pid/terminal fields for codex rows that can no longer be
    trusted as a real per-session client process.

    Live `ps` check, not transcript-based -- the daemon's pid is only
    known by inspecting the running system. Covers two cases: the stored
    pid is *currently* shaped like the shared app-server-daemon (ppid 1,
    no tty), or it's simply not running at all any more. The daemon
    itself gets restarted (e.g. on a Codex upgrade) with a new pid, so an
    old daemon pid from before this fix often can't be positively
    re-identified as the daemon once it's gone -- but a dead pid has zero
    remaining liveness value either way, so clearing it is always safe:
    reconcile already falls back to the rollout file's mtime for a
    pid-less codex session.
    """
    rows = conn.execute(
        "SELECT session_id, pid FROM sessions WHERE tool='codex' AND pid IS NOT NULL"
    ).fetchall()
    corrected = 0
    for row in rows:
        pid = row["pid"]
        suspect = is_untrusted_daemon_pid(pid) or not is_pid_alive(pid)
        if suspect:
            conn.execute(
                "UPDATE sessions SET pid=NULL, iterm_session_id=NULL, "
                "term_program=NULL, tty=NULL WHERE tool='codex' AND session_id=?",
                (row["session_id"],),
            )
            corrected += 1
    return corrected


def backfill_single_session(conn, tool, session_id, transcript_path=None):
    """Re-parse exactly one session's transcript and upsert it.

    Used by `agentboard backfill --session TOOL:ID` -- in particular, the
    hook spawns this (detached) right after Stop/SessionEnd so fields that
    need the whole transcript to compute correctly (todos_open, chiefly)
    are authoritative immediately, not just after the next full backfill
    at app launch. Same parse + upsert path as the full backfill, just
    scoped to one file instead of scanning every session's.
    """
    if not transcript_path:
        row = conn.execute(
            "SELECT transcript_path FROM sessions WHERE tool=? AND session_id=?",
            (tool, session_id),
        ).fetchone()
        transcript_path = row["transcript_path"] if row else None
    if not transcript_path:
        return "no-transcript"

    path = Path(transcript_path)
    if not path.is_file():
        return "missing-file"

    load_injected_prefixes()
    try:
        if tool == "claude":
            base_offset, todos_state = stored_todo_scan(conn, tool, session_id)
            fields = parse_claude_transcript(path, todos_state=todos_state)
            fields["todos_scan_base"] = base_offset
        elif tool == "codex":
            fields = parse_codex_rollout(path)
        else:
            return "unknown-tool"
    except Exception as exc:
        diag.warn("backfill", "parse_error", file=path, exc=type(exc).__name__, msg=str(exc))
        return "parse-error"

    conn.execute("BEGIN IMMEDIATE")
    try:
        upsert_backfill_session(conn, tool, session_id, fields)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return "updated"


def _upsert_in_own_transaction(conn, tool, session_id, fields, stats, path) -> bool:
    """One short write per session: a failure rolls back only this
    session's upsert and counts in stats["errors"]."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            upsert_backfill_session(conn, tool, session_id, fields)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    except Exception as exc:
        stats["errors"] += 1
        diag.warn("backfill", "upsert_error", tool=tool, file=path,
                  exc=type(exc).__name__, msg=str(exc))
        return False
    return True


def run_backfill(conn, days=30):
    """Import every transcript modified within `days`. Transcripts are
    parsed with no transaction open; each session is then written in its
    own short BEGIN IMMEDIATE, so a hook firing meanwhile waits for at
    most one upsert, never for the whole run."""
    stats = {"claude": 0, "codex": 0, "errors": 0, "daemon_pid_corrected": 0}
    started = time.monotonic()
    load_injected_prefixes()

    conn.execute("BEGIN IMMEDIATE")
    try:
        stats["daemon_pid_corrected"] = correct_daemon_pid_rows(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    for session_id, path in iter_claude_transcripts(days):
        base_offset, todos_state = stored_todo_scan(conn, "claude", session_id)
        try:
            fields = parse_claude_transcript(path, todos_state=todos_state)
        except Exception as exc:
            stats["errors"] += 1
            diag.warn("backfill", "parse_error", tool="claude", file=path,
                      exc=type(exc).__name__, msg=str(exc))
            continue
        fields["todos_scan_base"] = base_offset
        if _upsert_in_own_transaction(conn, "claude", session_id, fields, stats, path):
            stats["claude"] += 1

    for path in iter_codex_rollouts(days):
        try:
            fields = parse_codex_rollout(path)
        except Exception as exc:
            stats["errors"] += 1
            diag.warn("backfill", "parse_error", tool="codex", file=path,
                      exc=type(exc).__name__, msg=str(exc))
            continue
        session_id = fields.get("session_id")
        if not session_id:
            stats["errors"] += 1
            diag.warn("backfill", "parse_error", tool="codex", file=path,
                      msg="no session id in rollout")
            continue
        if _upsert_in_own_transaction(conn, "codex", session_id, fields, stats, path):
            stats["codex"] += 1

    diag.info("backfill", "done", days=days, ms=diag.ms_since(started), **stats)
    return stats
