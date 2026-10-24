"""Hook entrypoint: records Claude Code / Codex lifecycle events.

Contract: read a JSON payload from stdin, print nothing to stdout, always
exit 0, and never let an exception escape to the caller.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import db

MAX_TEXT_CHARS = 4000
MAX_LOG_BYTES = 1024 * 1024
GIT_TIMEOUT_SECS = 1
PS_TIMEOUT_SECS = 1
AGENT_PID_WALK_LIMIT = 5

# Events after which a session's todos_open may have just become stale
# (TaskCreate/TaskUpdate deltas need the whole transcript to resolve
# correctly) -- worth a detached single-session re-parse so auto-done
# doesn't have to wait for the next full `agentboard backfill`.
REPARSE_TRIGGER_EVENTS = {"Stop", "SessionEnd"}
REPARSE_GUARD_SECONDS = 5

# Codex events worth a detached codexmap.py mapping attempt (see
# spawn_codex_map) -- re-run on every UserPromptSubmit too, since `codex
# resume` inside an existing TUI switches which session its pid/tty belongs
# to, not just at SessionStart.
CODEX_MAP_TRIGGER_EVENTS = {"SessionStart", "UserPromptSubmit"}

# Parent events after which the session's git branch is re-read: the turn
# boundaries. Stop and SessionEnd matter because a branch created mid-turn
# (a PR opened from a fresh feature branch) must be seen once the session
# goes idle or exits, or a merged PR on the old branch would still retire it.
BRANCH_REFRESH_EVENTS = {"SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"}

# Event names that represent in-progress activity (not needing the user,
# not a finished turn).
ACTIVITY_EVENTS = {
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "SubagentStart",
    "SubagentStop",
    "PreCompact",
    "PostCompact",
}

# Event names whose own occurrence signals "running", even when tagged
# with an agent_id (i.e. fired by a subagent) -- unless the session's turn
# is already over (see FINISHED_TURN_EVENTS).
SUBAGENT_MARKER_EVENTS = {"SubagentStart", "SubagentStop"}

NEEDS_YOU_NOTIFICATION_TYPES = {
    "permission_prompt",
    "elicitation_dialog",
    "agent_needs_input",
}

# Notification types that are allowed to change last_event/waiting_reason.
# Anything else (agent_completed, auth_success, ...) is recorded in the
# events log but must not disturb the session's cached state.
RELEVANT_NOTIFICATION_TYPES = NEEDS_YOU_NOTIFICATION_TYPES | {"idle_prompt"}

# PreToolUse tool names that mean the agent is blocked on the user, mapped
# to the waiting_reason shown for them. Add more here as they come up.
NEEDS_YOU_TOOL_REASONS = {
    "AskUserQuestion": "question",
    "ExitPlanMode": "plan approval",
}

# Synthetic last_event value used when a PreToolUse call in
# NEEDS_YOU_TOOL_REASONS means the agent is waiting on the user. Never a
# real hook_event_name, so it can't collide with one.
NEEDS_YOU_TOOL_SENTINEL = "NeedsYouToolUse"

# A session's last_event values that mean its turn is over: it stopped, was
# interrupted, went idle or is waiting on the user. A Notification only
# ever becomes last_event when it is relevant (idle_prompt or a needs_you
# type, see _notification_is_relevant), so no type check is needed. The
# needs_you members match reconcile.NEEDS_YOU_EVENTS (reconcile imports
# from here, not the reverse). A subagent-tagged SubagentStart/SubagentStop
# (a background agent finishing after the turn) never overwrites these, and
# whenever the hook keeps one of them it also keeps last_event_at, the time
# the turn ended.
FINISHED_TURN_EVENTS = {
    "Stop", "Interrupt", "Notification",
    "PermissionRequest", "StopFailure", NEEDS_YOU_TOOL_SENTINEL,
}

# SessionStart sources that continue the same conversation rather than
# start a new one: they never change a session's state (resume only while
# the session has not ended; see process_payload).
SESSION_START_KEEP_SOURCES = {"compact", "resume"}

# Background task statuses (Stop's background_tasks[].status) that mean the
# task is no longer in flight. The docs describe status only as "Current
# task status" and list no values, so this set is defensive and an unknown
# status counts as in flight.
TERMINAL_TASK_STATUSES = {"completed", "failed", "killed", "stopped", "cancelled"}

# Stored per cron: its prompt with whitespace collapsed, cut to this many
# characters, and at most CRON_PROMPTS_MAX of them.
CRON_PROMPT_CHARS = 200
CRON_PROMPTS_MAX = 20

# Slash commands handled entirely client-side (change CLI state, no model
# turn, no response). Submitting one of these is not a real prompt.
LOCAL_ONLY_COMMANDS = {"model", "clear", "config", "mcp", "effort", "compact", "plan"}

# Plain text that just ends the session -- not a real prompt either.
EXIT_COMMANDS = {"exit", "quit", "/exit", "/quit"}

_COMMAND_NAME_RE = re.compile(r"<command-name>/(?P<name>[^<]+)</command-name>")
_COMMAND_ARGS_RE = re.compile(r"<command-args>(?P<args>.*?)</command-args>", re.DOTALL)


def clean_prompt_text(text):
    """Resolve raw prompt/content text to the real prompt.

    Skill/command invocations (<command-name>/foo</command-name><command-args>ARGS</command-args>)
    resolve to "/foo ARGS". Local-only commands with no model turn (model,
    clear, config, mcp, effort, compact, plan) are not prompts at all and
    resolve to None, neither is a bare exit/quit. Anything else passes
    through unchanged.
    """
    if text is None:
        return None
    match = _COMMAND_NAME_RE.search(text)
    if match:
        name = match.group("name").strip()
        if name in LOCAL_ONLY_COMMANDS:
            return None
        args_match = _COMMAND_ARGS_RE.search(text)
        args = args_match.group("args").strip() if args_match else ""
        text = f"/{name} {args}".strip()
    if text.strip().lower() in EXIT_COMMANDS:
        return None
    return text


def normalize_branch(branch):
    """HEAD means detached (or a transcript's own placeholder): no branch."""
    if not branch or branch == "HEAD":
        return None
    return branch


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _truncate(text):
    if text is None:
        return None
    text = str(text)
    if len(text) > MAX_TEXT_CHARS:
        return text[:MAX_TEXT_CHARS]
    return text


def _diag(level: str, name: str, /, **fields) -> None:
    """Write a `hook.<event>` line to agentboard.log. diag is imported here,
    lazily, never at module top, so the shim's hook fast path keeps its
    import graph; like everything else in a hook, this never raises."""
    try:
        from . import diag

        diag.log(level, "hook", name, **fields)
    except Exception:
        pass


def _log_error(message: str) -> None:
    try:
        home = db.home_dir()
        home.mkdir(parents=True, exist_ok=True)
        path = home / "hook-errors.log"
        line = f"{_now_iso()} {message}\n".encode("utf-8", "replace")
        data = b""
        if path.exists():
            data = path.read_bytes()
        data += line
        if len(data) > MAX_LOG_BYTES:
            data = data[-MAX_LOG_BYTES:]
        path.write_bytes(data)
    except Exception:
        pass


def _ps_lookup(pid: int):
    """Return (ppid, tty, comm) for pid via a single-pid `ps` call, or None."""
    try:
        out = subprocess.run(
            ["ps", "-o", "ppid=,tty=,comm=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=PS_TIMEOUT_SECS,
        )
    except Exception:
        return None
    line = out.stdout.strip()
    if not line:
        return None
    parts = line.split(None, 2)
    if len(parts) < 3:
        return None
    try:
        ppid = int(parts[0])
    except ValueError:
        return None
    return ppid, parts[1], parts[2]


def _clean_tty(tty):
    """ps reports "??" (and sometimes "") for no controlling terminal."""
    if not tty or tty == "??":
        return None
    return tty


def is_untrusted_daemon(ppid, tty) -> bool:
    """True for a long-lived shared daemon rather than a per-session client.

    Codex 0.160+ spawns hooks from its shared app-server-daemon (ppid 1,
    no controlling terminal), which every session's hooks descend from --
    useless, and actively misleading, as a session-specific pid/liveness/
    terminal signal. The check is intentionally general (not Codex-named)
    since the same reparented-daemon shape could show up anywhere.
    """
    return ppid == 1 and _clean_tty(tty) is None


def is_untrusted_daemon_pid(pid) -> bool:
    """Like is_untrusted_daemon, but looked up fresh for an already-known pid."""
    if pid is None:
        return False
    entry = _ps_lookup(pid)
    if entry is None:
        return False
    ppid, tty, _comm = entry
    return is_untrusted_daemon(ppid, tty)


def _find_agent_pid(start_pid: int):
    """Walk up the process tree (<=5 levels) for a claude/codex process.

    Returns (pid, tty, is_daemon). pid is None if no claude/codex ancestor
    is found (never falls back to start_pid -- that's the hook's own
    short-lived shell wrapper, not the agent, and reads as dead almost
    immediately), OR if the matched ancestor is a shared daemon rather
    than a real per-session client (see is_untrusted_daemon) -- in that
    case is_daemon is True and tty is also suppressed, since both the pid
    and the hook's inherited terminal env belong to the daemon, not this
    session. tty is otherwise the resolved agent process's own tty when
    there is one; the first hop (which usually shares the hook's
    controlling terminal) is only a fallback -- hooks may be spawned
    detached (setsid), in which case the hook's own immediate parent has
    no controlling terminal even though the agent further up does.
    """
    pid = start_pid
    first_hop_tty = None
    for level in range(AGENT_PID_WALK_LIMIT):
        entry = _ps_lookup(pid)
        if entry is None:
            break
        ppid, hop_tty, comm = entry
        hop_tty = _clean_tty(hop_tty)
        if level == 0:
            first_hop_tty = hop_tty
        lowered = comm.lower()
        if "claude" in lowered or "codex" in lowered:
            if is_untrusted_daemon(ppid, hop_tty):
                return None, None, True
            return pid, hop_tty or first_hop_tty, False
        if ppid == pid or ppid <= 1:
            break
        pid = ppid
    return None, first_hop_tty, False


HEAD_REF_PREFIX = "ref: refs/heads/"
GIT_HEAD_MAX_BYTES = 4096


def _head_file_branch(cwd: str):
    """Read the branch straight from the repository's HEAD file, the way
    `git branch --show-current` resolves it, without starting a process
    (git costs tens of ms per call, the hook's whole budget). Returns
    (True, branch-or-None) when a HEAD was found and understood (None:
    detached), else (False, None) so the caller falls back to git: no
    `.git` found, a `.git` file that isn't `gitdir: ...`, or a HEAD in a
    form this does not know (e.g. reftable's placeholder)."""
    try:
        directory = Path(cwd)
        for candidate in (directory, *directory.parents):
            dot_git = candidate / ".git"
            if dot_git.is_dir():
                git_dir = dot_git
            elif dot_git.is_file():
                text = dot_git.read_text(errors="replace")[:GIT_HEAD_MAX_BYTES].strip()
                if not text.startswith("gitdir:"):
                    return False, None
                git_dir = Path(text[len("gitdir:"):].strip())
                if not git_dir.is_absolute():
                    git_dir = candidate / git_dir
            else:
                continue
            with open(git_dir / "HEAD", encoding="utf-8", errors="replace") as fh:
                head = fh.read(GIT_HEAD_MAX_BYTES).strip()
            if head.startswith(HEAD_REF_PREFIX):
                name = head[len(HEAD_REF_PREFIX):].strip()
                if not name or name == ".invalid":
                    return False, None
                return True, normalize_branch(name)
            if re.fullmatch(r"[0-9a-f]{40}([0-9a-f]{24})?", head):
                return True, None
            return False, None
    except (OSError, ValueError):
        return False, None
    return False, None


def _git_branch(cwd: str):
    if not cwd:
        return None
    found, branch = _head_file_branch(cwd)
    if found:
        return branch
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECS,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return normalize_branch(out.stdout.strip())


def _extract_waiting_reason(event: str, payload: dict):
    if event == "PermissionRequest":
        return payload.get("tool_name")
    if event == "Notification":
        return payload.get("message") or payload.get("notification_type")
    if event == "StopFailure":
        return payload.get("error_message") or payload.get("reason")
    return None


def _extract_extra(payload: dict) -> dict:
    extra = {}
    for key in ("source", "model", "turn_id", "reason", "error_message"):
        if key in payload:
            extra[key] = payload[key]
    return extra


def _background_info(payload: dict):
    """(count, types, cron_count, cron_prompts) from a Stop payload's
    background_tasks / session_crons arrays. A missing (or malformed) array
    gives None for its pair: older Claude Code sends neither. The count and
    types cover only tasks whose status is not terminal; types keep one
    label per task, duplicates included, so a finished subagent can remove
    exactly one."""
    count = types = cron_count = cron_prompts = None
    tasks = payload.get("background_tasks")
    if isinstance(tasks, list):
        types = []
        for task in tasks:
            if not isinstance(task, dict):
                continue
            status = str(task.get("status") or "").strip().lower()
            if status in TERMINAL_TASK_STATUSES:
                continue
            types.append(str(task.get("type") or "task"))
        count = len(types)
    crons = payload.get("session_crons")
    if isinstance(crons, list):
        cron_count = len(crons)
        cron_prompts = []
        for cron in crons:
            prompt = cron.get("prompt") if isinstance(cron, dict) else None
            if isinstance(prompt, str) and prompt.strip():
                cron_prompts.append(" ".join(prompt.split())[:CRON_PROMPT_CHARS])
            if len(cron_prompts) >= CRON_PROMPTS_MAX:
                break
    return count, types, cron_count, cron_prompts


def _notification_is_relevant(payload: dict) -> bool:
    return payload.get("notification_type") in RELEVANT_NOTIFICATION_TYPES


def _needs_you_tool_reason(event: str, tool_name):
    if event == "PreToolUse":
        return NEEDS_YOU_TOOL_REASONS.get(tool_name)
    return None


# Todo/task tools whose PostToolUse payload is self-contained enough to
# read a fresh open count directly from (no state to accumulate). The
# newer TaskUpdate/TaskStop tools only carry a single task's delta, not
# the whole list, so a live count from them isn't reliable here --
# backfill reconstructs the full picture from the transcript instead.
TODO_TOOL_NAMES = {"TodoWrite", "TaskCreate"}


def _todos_open_from_tool_use(tool_name, tool_input):
    if not isinstance(tool_input, dict):
        return None
    if tool_name == "TodoWrite":
        todos = tool_input.get("todos")
        if not isinstance(todos, list):
            return None
        return sum(1 for t in todos if isinstance(t, dict) and t.get("status") != "completed")
    if tool_name == "TaskCreate":
        tasks = tool_input.get("tasks")
        if isinstance(tasks, str):
            try:
                tasks = json.loads(tasks)
            except ValueError:
                return None
        if not isinstance(tasks, list):
            return None
        return len(tasks)
    return None


def _bin_path() -> Path:
    return Path(__file__).resolve().parent.parent / "bin" / "agentboard"


def _reparse_marker_path(tool, session_id):
    return db.home_dir() / "reparse-locks" / f"{tool}-{session_id}.marker"


def _recently_spawned_reparse(tool, session_id) -> bool:
    try:
        mtime = _reparse_marker_path(tool, session_id).stat().st_mtime
    except OSError:
        return False
    return (time.time() - mtime) < REPARSE_GUARD_SECONDS


def _mark_reparse_spawned(tool, session_id) -> None:
    path = _reparse_marker_path(tool, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def spawn_todos_reparse(tool, session_id, transcript_path) -> None:
    """Fire-and-forget single-session re-parse right after Stop/SessionEnd.

    Fully detached (own session, stdio dropped) and never waited on, so
    this can't slow the hook down; a marker file skips spawning again for
    the same session within REPARSE_GUARD_SECONDS, in case Stop and
    SessionEnd both fire close together.
    """
    if not transcript_path:
        return
    if _recently_spawned_reparse(tool, session_id):
        _diag("debug", "spawn", what="backfill", session=f"{tool}:{session_id}", outcome="guarded")
        return
    try:
        subprocess.Popen(
            [
                str(_bin_path()), "backfill",
                "--session", f"{tool}:{session_id}",
                "--transcript", str(transcript_path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        _diag("warn", "spawn", what="backfill", session=f"{tool}:{session_id}",
              outcome="error", exc=type(exc).__name__, msg=str(exc))
        return
    _diag("debug", "spawn", what="backfill", session=f"{tool}:{session_id}", outcome="spawned")
    _mark_reparse_spawned(tool, session_id)


def spawn_codex_map(session_id, cwd, at) -> None:
    """Fire-and-forget: map this Codex session to its real TUI process.

    The hook itself has no reliable pid/tty for a daemon-origin Codex
    session (see is_untrusted_daemon) -- codexmap.py's ps/lsof-based match
    against the live process list is too slow to run inline, so it's
    spawned detached exactly like spawn_todos_reparse above.
    """
    if not cwd:
        return
    try:
        subprocess.Popen(
            [
                str(_bin_path()), "map-codex",
                "--session", session_id,
                "--cwd", cwd,
                "--at", at,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        _diag("warn", "spawn", what="map-codex", session=f"codex:{session_id}",
              outcome="error", exc=type(exc).__name__, msg=str(exc))
        return
    _diag("debug", "spawn", what="map-codex", session=f"codex:{session_id}", outcome="spawned")


def _last_parent_prompt(conn, tool, session_id):
    """(text, prompt_kind) of the session's newest parent (not
    subagent-tagged), non-injected prompt, or (None, None).

    Read from events rather than sessions.last_prompt, so the hook decides
    "unattended" from the same prompt `notes --rebuild` tracks for the same
    Stop. An injected prompt (a task notification, another agent's message)
    is skipped: the turn it starts belongs to whoever drove the session
    before it. Walks the partial parent-events index backwards and stops at
    the first match.
    """
    found = conn.execute(
        "SELECT text, prompt_kind FROM events WHERE tool=? AND session_id=? "
        "AND agent_id IS NULL AND event='UserPromptSubmit' AND text IS NOT NULL "
        "AND text <> '' AND (prompt_kind IS NULL OR prompt_kind <> 'injected') "
        "ORDER BY ts DESC, id DESC LIMIT 1",
        (tool, session_id),
    ).fetchone()
    return (found[0], found[1]) if found else (None, None)


def _classify_prompt(text, row):
    """events.prompt_kind for a parent prompt (notes.prompt_kind with the
    configured prefixes and the session's stored cron prompts), or None
    when classifying fails: the event is still recorded, as legacy (human).
    `notes` is imported only for prompts and Stops."""
    try:
        from . import notes

        config = notes.load_config(db.home_dir())
        cron_prompts = ()
        if row is not None and row["cron_prompts"]:
            value = json.loads(row["cron_prompts"])
            if isinstance(value, list):
                cron_prompts = tuple(item for item in value if isinstance(item, str))
        return notes.prompt_kind(
            text,
            injected=notes.injected_prefixes(config),
            unattended=notes.unattended_prefixes(config),
            cron_prompts=cron_prompts,
        )
    except Exception as exc:
        _diag("warn", "prompt_kind", outcome="error", exc=type(exc).__name__, msg=str(exc))
        return None


def _record_stop_note(conn, tool, session_id, text, now, event_id) -> None:
    """Classify a parent Stop's final message and keep it as a note.

    Runs inside process_payload's transaction, under a savepoint, so a
    classifier or note-write failure is logged and rolled back on its own
    without losing the event. `notes` is imported here, only for parent
    Stop events (and, in _classify_prompt, parent prompts), so every other
    event adds no import or config read.
    """
    try:
        conn.execute("SAVEPOINT stop_note")
    except Exception as exc:
        _diag("warn", "notes", session=f"{tool}:{session_id}", outcome="error",
              exc=type(exc).__name__, msg=str(exc))
        return
    try:
        from . import notes

        config = notes.load_config(db.home_dir())
        prompt_text, kind_of_prompt = _last_parent_prompt(conn, tool, session_id)
        unattended = notes.kind_is_unattended(
            kind_of_prompt, prompt_text, notes.unattended_prefixes(config)
        )
        # The session's earlier notes are read only when this message names
        # a PR/issue reference, so most Stops add no query.
        seen_refs = notes.session_refs(conn, tool, session_id) if notes.refs(text) else None
        kind = notes.classify(
            text, markers=notes.markers(config), unattended=unattended, seen_refs=seen_refs
        )
        if kind:
            notes.record_note(conn, tool, session_id, kind, text, "stop", now, event_id=event_id)
            _diag("debug", "note", session=f"{tool}:{session_id}", kind=kind, event_id=event_id)
        conn.execute("RELEASE stop_note")
    except Exception as exc:
        try:
            conn.execute("ROLLBACK TO stop_note")
            conn.execute("RELEASE stop_note")
        except Exception:
            pass
        _diag("warn", "notes", session=f"{tool}:{session_id}", outcome="error",
              exc=type(exc).__name__, msg=str(exc))


def _drop_background_subagent(conn, tool, session_id, row) -> None:
    """Remove one "subagent" entry from the session's background_types and
    decrement background_tasks (floor 0), only if an entry was removed."""
    try:
        types = json.loads(row["background_types"] or "[]")
    except (TypeError, ValueError):
        return
    if not isinstance(types, list) or "subagent" not in types:
        return
    types.remove("subagent")
    count = max(0, (row["background_tasks"] or 0) - 1)
    conn.execute(
        "UPDATE sessions SET background_tasks=?, background_types=? "
        "WHERE tool=? AND session_id=?",
        (count, json.dumps(types), tool, session_id),
    )


def process_payload(conn, tool: str, payload: dict) -> None:
    """Record one hook event. Raises on error; caller must swallow it."""
    event = payload.get("hook_event_name") or payload.get("event") or ""
    session_id = payload.get("session_id")
    if not session_id:
        # A real drop: nothing is recorded for this event.
        _diag("info", "drop", tool=tool, event=event or None,
              reason="empty_payload" if not payload else "missing_session_id")
        return

    now = _now_iso()
    cwd = payload.get("cwd")
    transcript_path = payload.get("transcript_path")
    agent_id = payload.get("agent_id")
    tool_name = payload.get("tool_name")
    notification_type = payload.get("notification_type")

    prompt_text = clean_prompt_text(payload.get("prompt")) if event == "UserPromptSubmit" else None

    text = None
    if event == "UserPromptSubmit":
        text = _truncate(prompt_text)
    elif event in ("Stop",):
        text = _truncate(payload.get("last_assistant_message"))
    elif event == "Notification":
        text = _truncate(payload.get("message"))
    elif event == "StopFailure":
        text = _truncate(payload.get("error_message") or payload.get("reason"))

    extra = _extract_extra(payload)

    # A parent Stop lists what is still in flight (background tasks) and
    # scheduled (crons); both are stored on the session below.
    background = _background_info(payload) if event == "Stop" and not agent_id else None
    if background is not None:
        if background[0] is not None:
            extra["background_tasks"] = background[0]
        if background[2] is not None:
            extra["session_crons"] = background[2]

    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM sessions WHERE tool=? AND session_id=?",
            (tool, session_id),
        ).fetchone()

        is_subagent_tagged = bool(agent_id) and event not in SUBAGENT_MARKER_EVENTS

        # What kind of prompt a parent prompt is (human, loop, cron or
        # injected by Claude Code itself). An injected one still starts a
        # turn (last_event becomes UserPromptSubmit) but is not the user
        # replying: it neither reopens a done session nor replaces the
        # session's prompts.
        prompt_kind = None
        if event == "UserPromptSubmit" and not is_subagent_tagged:
            prompt_kind = _classify_prompt(text, row)
        injected = prompt_kind == "injected"

        cursor = conn.execute(
            "INSERT INTO events(ts, tool, session_id, event, agent_id, "
            "notification_type, tool_name, text, extra, prompt_kind) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                now,
                tool,
                session_id,
                event,
                agent_id,
                notification_type,
                tool_name,
                text,
                json.dumps(extra) if extra else None,
                prompt_kind,
            ),
        )

        if event == "Stop" and not is_subagent_tagged and text:
            _record_stop_note(conn, tool, session_id, text, now, cursor.lastrowid)

        if event == "UserPromptSubmit" and not is_subagent_tagged and not injected:
            conn.execute(
                "UPDATE triage SET status='open', updated_at=? "
                "WHERE tool=? AND session_id=? AND status='done'",
                (now, tool, session_id),
            )

        branch = None
        if (event in BRANCH_REFRESH_EVENTS and not is_subagent_tagged) or (row and not row["branch"]):
            branch = _git_branch(cwd)

        pid = None
        tty = None
        is_daemon = False
        if event in ("SessionStart", "UserPromptSubmit") or (row and not row["pid"]):
            pid, tty, is_daemon = _find_agent_pid(os.getppid())

        if is_daemon:
            # The resolved process is a shared daemon, not this session's
            # client -- its inherited terminal env isn't this session's
            # terminal either.
            iterm_session_id = None
            term_program = None
        else:
            iterm_session_id = (
                os.environ.get("ITERM_SESSION_ID") or os.environ.get("TERM_SESSION_ID")
            )
            term_program = os.environ.get("TERM_PROGRAM")

        irrelevant_notification = event == "Notification" and not _notification_is_relevant(payload)
        needs_you_tool_reason = _needs_you_tool_reason(event, tool_name)

        row_last_event = row["last_event"] if row else None
        # A background subagent starting or finishing after the turn ended
        # does not make the session busy again.
        marker_after_turn = (
            bool(agent_id) and event in SUBAGENT_MARKER_EVENTS
            and row_last_event in FINISHED_TURN_EVENTS
        )
        # Compaction (and resuming a live session) continues the same
        # conversation; resuming after a SessionEnd records SessionStart,
        # since keeping SessionEnd once `resumed` clears ended_at would
        # read as nothing at all.
        source = payload.get("source") if event == "SessionStart" else None
        continued_start = (
            row is not None and not is_subagent_tagged
            and source in SESSION_START_KEEP_SOURCES
            and (source == "compact" or row_last_event != "SessionEnd")
        )
        keep_state = (
            is_subagent_tagged or irrelevant_notification or marker_after_turn or continued_start
        )

        new_last_event_at = now
        if keep_state:
            new_last_event = row_last_event
            new_waiting_reason = row["waiting_reason"] if row else None
            # A kept finished turn keeps the time it finished; a kept
            # activity event (a subagent working mid-turn, a mid-turn
            # compaction) keeps advancing it, since it is compute_state's
            # freshness signal when liveness is unknown.
            if row is not None and row_last_event in FINISHED_TURN_EVENTS:
                new_last_event_at = row["last_event_at"] or now
        elif needs_you_tool_reason is not None:
            new_last_event = NEEDS_YOU_TOOL_SENTINEL
            new_waiting_reason = needs_you_tool_reason
        else:
            new_last_event = event
            new_waiting_reason = _extract_waiting_reason(event, payload)

        # A subagent's own prompt, or an injected one, is not the session's.
        sets_prompt = event == "UserPromptSubmit" and not is_subagent_tagged and not injected
        first_prompt = prompt_text if sets_prompt else None
        last_prompt = _truncate(prompt_text) if sets_prompt else None
        last_assistant = text if event == "Stop" else None
        ended_at = now if event == "SessionEnd" else None
        end_reason = payload.get("reason") if event == "SessionEnd" else None
        started_at = now if row is None else None
        todos_open = (
            _todos_open_from_tool_use(tool_name, payload.get("tool_input"))
            if event == "PostToolUse" else None
        )
        resumed = (
            row is not None and row["ended_at"]
            and event in ("SessionStart", "UserPromptSubmit")
            and not is_subagent_tagged
        )

        if row is None:
            conn.execute(
                "INSERT INTO sessions(tool, session_id, cwd, branch, title, "
                "first_prompt, last_prompt, last_assistant, started_at, "
                "updated_at, last_event, last_event_at, waiting_reason, pid, "
                "iterm_session_id, term_program, transcript_path, origin, "
                "interactive, ended_at, tty, end_reason, todos_open) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    tool,
                    session_id,
                    cwd,
                    branch,
                    None,
                    _truncate(first_prompt),
                    last_prompt,
                    last_assistant,
                    started_at,
                    now,
                    new_last_event,
                    now,
                    new_waiting_reason,
                    pid,
                    iterm_session_id,
                    term_program,
                    transcript_path,
                    "hook",
                    None,
                    ended_at,
                    tty,
                    end_reason,
                    todos_open,
                ),
            )
        else:
            conn.execute(
                "UPDATE sessions SET "
                "cwd = COALESCE(?, cwd), "
                "branch = COALESCE(?, branch), "
                "first_prompt = COALESCE(first_prompt, ?), "
                "last_prompt = COALESCE(?, last_prompt), "
                "last_assistant = COALESCE(?, last_assistant), "
                "updated_at = ?, "
                "last_event = ?, "
                "last_event_at = ?, "
                "waiting_reason = ?, "
                "pid = COALESCE(?, pid), "
                "iterm_session_id = COALESCE(?, iterm_session_id), "
                "term_program = COALESCE(?, term_program), "
                "transcript_path = COALESCE(?, transcript_path), "
                "origin = 'hook', "
                "ended_at = COALESCE(?, ended_at), "
                "tty = COALESCE(?, tty), "
                "end_reason = COALESCE(?, end_reason), "
                "todos_open = COALESCE(?, todos_open) "
                "WHERE tool=? AND session_id=?",
                (
                    cwd,
                    branch,
                    _truncate(first_prompt),
                    last_prompt,
                    last_assistant,
                    now,
                    new_last_event,
                    new_last_event_at,
                    new_waiting_reason,
                    pid,
                    iterm_session_id,
                    term_program,
                    transcript_path,
                    ended_at,
                    tty,
                    end_reason,
                    todos_open,
                    tool,
                    session_id,
                ),
            )

        if background is not None:
            # Every parent Stop rewrites all four, so a missing array (older
            # Claude Code) reads as unknown rather than a stale count.
            count, types, cron_count, cron_prompts = background
            conn.execute(
                "UPDATE sessions SET background_tasks=?, background_types=?, "
                "session_crons=?, cron_prompts=? WHERE tool=? AND session_id=?",
                (
                    count,
                    json.dumps(types) if types is not None else None,
                    cron_count,
                    json.dumps(cron_prompts) if cron_prompts is not None else None,
                    tool,
                    session_id,
                ),
            )
        elif marker_after_turn and event == "SubagentStop":
            # A background subagent finished: one fewer in flight. A
            # SubagentStop of a workflow/teammate task changes neither
            # until the next parent Stop rewrites both.
            _drop_background_subagent(conn, tool, session_id, row)

        if (
            event == "SessionEnd" and not is_subagent_tagged
            and (row is None or row["first_prompt"] is None)
        ):
            # A session that ends without ever being prompted is a helper
            # (an SDK or tool-spawned run), not something to show.
            conn.execute(
                "UPDATE sessions SET interactive=0 WHERE tool=? AND session_id=? "
                "AND first_prompt IS NULL AND NOT EXISTS ("
                "  SELECT 1 FROM events WHERE tool=? AND session_id=? "
                "  AND agent_id IS NULL AND event='UserPromptSubmit')",
                (tool, session_id, tool, session_id),
            )

        if is_daemon:
            # COALESCE above only avoids writing a *new* bad value; this
            # clears out any daemon pid/terminal fields a previous event
            # (or a pre-fix hook run) already stored.
            conn.execute(
                "UPDATE sessions SET pid=NULL, iterm_session_id=NULL, "
                "term_program=NULL, tty=NULL WHERE tool=? AND session_id=?",
                (tool, session_id),
            )

        if resumed:
            # New activity after a SessionEnd means the session is live
            # again -- clear the ended marker so auto-done (which requires
            # ended_at) stops applying until it genuinely ends once more.
            conn.execute(
                "UPDATE sessions SET ended_at=NULL, end_reason=NULL "
                "WHERE tool=? AND session_id=?",
                (tool, session_id),
            )

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    # Routine, including the downgrades (subagent-tagged events keep
    # last_event, daemon-origin events drop pid/terminal, irrelevant
    # notifications are ignored): debug only, so a normal session adds
    # nothing to the log at the default info level.
    _diag(
        "debug", "event", tool=tool, event=event, session=session_id, pid=pid,
        tty=tty, daemon=is_daemon, subagent=is_subagent_tagged,
        ignored=(
            "subagent" if is_subagent_tagged
            else "irrelevant_notification" if irrelevant_notification
            else "after_turn" if marker_after_turn
            else f"session_start_{source}" if continued_start else None
        ),
        term=term_program,
    )

    if event in REPARSE_TRIGGER_EVENTS and not is_subagent_tagged:
        try:
            spawn_todos_reparse(tool, session_id, transcript_path)
        except Exception:
            pass

    if (
        tool == "codex"
        and event in CODEX_MAP_TRIGGER_EVENTS
        and not is_subagent_tagged
        and is_daemon
    ):
        try:
            spawn_codex_map(session_id, cwd, now)
        except Exception:
            pass


# Set by AgentBoard around commands it runs itself (the retitle command's
# `claude -p`), so their sessions are not recorded. Matches
# titles.HOOK_SKIP_ENV (not imported: this module stays light).
HOOK_SKIP_ENV = "AGENTBOARD_HOOK_SKIP"


def run(tool: str, stdin_stream=None) -> None:
    """Entry point used by the CLI. Never raises, never prints."""
    if os.environ.get(HOOK_SKIP_ENV):
        return
    if stdin_stream is None:
        stdin_stream = sys.stdin
    try:
        raw = stdin_stream.read()
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            _diag("info", "drop", tool=tool, reason="unparseable_payload", bytes=len(raw))
            raise
        conn = db.connect()
        try:
            process_payload(conn, tool, payload)
        finally:
            conn.close()
    except Exception as exc:
        _log_error(f"[{tool}] {type(exc).__name__}: {exc}")
