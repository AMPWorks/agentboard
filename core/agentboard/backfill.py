"""Import past Claude and Codex transcripts into the AgentBoard database."""

import json
import os
from datetime import datetime, timedelta, timezone
import time
from pathlib import Path

from . import diag
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
    "<task-notification>",
)


def _claude_user_text(message_content):
    """Return plain prompt text for a 'user' entry, or None if not real."""
    if isinstance(message_content, str):
        if message_content.startswith(SYNTHETIC_USER_TAGS):
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


def _compute_claude_todos_open(path: Path):
    """Reconstruct the final todo/task list state from a full transcript
    scan and return the count still open, or None if no list was ever
    created (NULL means "unknown/no list", which counts as no unfinished
    todos for auto-done purposes).

    TodoWrite is self-contained (every call carries the whole list).
    TaskCreate/TaskUpdate/TaskStop are deltas against task ids that are
    just the 1-based position in the most recent TaskCreate call -- there
    is no batch/list id, so a fresh TaskCreate replaces the active list.
    Whichever mechanism was touched most recently wins (sessions use one
    or the other, not both, but let's not assume).
    """
    task_status = {}
    todowrite_open = None
    last_mechanism = None

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if entry.get("type") != "assistant":
                continue
            content = entry.get("message", {}).get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = block.get("name")
                tool_input = block.get("input")
                if not isinstance(tool_input, dict):
                    continue
                if name == "TodoWrite":
                    todos = tool_input.get("todos")
                    if isinstance(todos, list):
                        todowrite_open = sum(
                            1 for t in todos
                            if isinstance(t, dict) and t.get("status") != "completed"
                        )
                        last_mechanism = "todowrite"
                elif name == "TaskCreate":
                    tasks = tool_input.get("tasks")
                    if isinstance(tasks, str):
                        try:
                            tasks = json.loads(tasks)
                        except ValueError:
                            tasks = None
                    if isinstance(tasks, list):
                        task_status = {str(i + 1): "pending" for i in range(len(tasks))}
                        last_mechanism = "task"
                elif name == "TaskUpdate":
                    task_id = tool_input.get("taskId") or tool_input.get("task_id")
                    status = tool_input.get("status")
                    if task_id is not None and status:
                        task_status[str(task_id)] = status
                        last_mechanism = "task"
                elif name == "TaskStop":
                    task_id = tool_input.get("task_id") or tool_input.get("taskId")
                    if task_id is not None:
                        task_status[str(task_id)] = "stopped"
                        last_mechanism = "task"

    if last_mechanism == "todowrite":
        return todowrite_open
    if last_mechanism == "task":
        return sum(1 for status in task_status.values() if status in TASK_OPEN_STATUSES)
    return None


def parse_claude_transcript(path: Path) -> dict:
    """Extract session fields from one ~/.claude/projects/*/*.jsonl file."""
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
    result["todos_open"] = _compute_claude_todos_open(path)
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
# which only backfill's full scan has -- the hook only ever sees one
# PostToolUse payload at a time.
ALWAYS_OVERWRITE_COLUMNS = {"todos_open"}


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

    if row is None:
        columns = ["tool", "session_id", "origin"] + BACKFILL_COLUMNS + ["title_source"]
        placeholders = ",".join("?" for _ in columns)
        params = ([tool, session_id, "backfill"] + [values[c] for c in BACKFILL_COLUMNS]
                  + [title_source])
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
            if col == "title":
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
        if not sets:
            return "unchanged"
        params += [tool, session_id]
        conn.execute(
            f"UPDATE sessions SET {','.join(sets)} WHERE tool=? AND session_id=?",
            params,
        )
        return "filled"

    columns = [col for col in BACKFILL_COLUMNS if col != "title"]
    sets = [f"{col}=?" for col in columns]
    params = [values[col] for col in columns]
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

    try:
        if tool == "claude":
            fields = parse_claude_transcript(path)
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


def run_backfill(conn, days=30):
    stats = {"claude": 0, "codex": 0, "errors": 0, "daemon_pid_corrected": 0}
    started = time.monotonic()

    conn.execute("BEGIN IMMEDIATE")
    try:
        stats["daemon_pid_corrected"] = correct_daemon_pid_rows(conn)

        for session_id, path in iter_claude_transcripts(days):
            try:
                fields = parse_claude_transcript(path)
            except Exception as exc:
                stats["errors"] += 1
                diag.warn("backfill", "parse_error", tool="claude", file=path,
                          exc=type(exc).__name__, msg=str(exc))
                continue
            upsert_backfill_session(conn, "claude", session_id, fields)
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
            upsert_backfill_session(conn, "codex", session_id, fields)
            stats["codex"] += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    diag.info("backfill", "done", days=days, ms=diag.ms_since(started), **stats)
    return stats
