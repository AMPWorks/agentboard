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

# Event names whose own occurrence always signals "running", even when
# tagged with an agent_id (i.e. fired by a subagent).
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


def _git_branch(cwd: str):
    if not cwd:
        return None
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


def _last_parent_prompt_text(conn, tool, session_id):
    """Text of the session's newest parent (not subagent-tagged) prompt.

    Read from events rather than sessions.last_prompt, which a subagent
    UserPromptSubmit also overwrites, so the hook decides "unattended" from
    the same prompt `notes --rebuild` tracks for the same Stop. Walks the
    partial parent-events index backwards and stops at the first match.
    """
    found = conn.execute(
        "SELECT text FROM events WHERE tool=? AND session_id=? AND agent_id IS NULL "
        "AND event='UserPromptSubmit' AND text IS NOT NULL AND text <> '' "
        "ORDER BY ts DESC, id DESC LIMIT 1",
        (tool, session_id),
    ).fetchone()
    return found[0] if found else None


def _record_stop_note(conn, tool, session_id, text, now, event_id) -> None:
    """Classify a parent Stop's final message and keep it as a note.

    Runs inside process_payload's transaction, under a savepoint, so a
    classifier or note-write failure is logged and rolled back on its own
    without losing the event. `notes` is imported here, only for parent
    Stop events, so every other event adds no import or config read.
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
        unattended = notes.is_unattended(
            _last_parent_prompt_text(conn, tool, session_id),
            notes.unattended_prefixes(config),
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

    conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = conn.execute(
            "INSERT INTO events(ts, tool, session_id, event, agent_id, "
            "notification_type, tool_name, text, extra) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
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
            ),
        )

        row = conn.execute(
            "SELECT * FROM sessions WHERE tool=? AND session_id=?",
            (tool, session_id),
        ).fetchone()

        is_subagent_tagged = bool(agent_id) and event not in SUBAGENT_MARKER_EVENTS

        if event == "Stop" and not is_subagent_tagged and text:
            _record_stop_note(conn, tool, session_id, text, now, cursor.lastrowid)

        if event == "UserPromptSubmit" and not is_subagent_tagged:
            conn.execute(
                "UPDATE triage SET status='open', updated_at=? "
                "WHERE tool=? AND session_id=? AND status='done'",
                (now, tool, session_id),
            )

        branch = None
        if event in ("SessionStart", "UserPromptSubmit") or (row and not row["branch"]):
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

        if is_subagent_tagged or irrelevant_notification:
            new_last_event = row["last_event"] if row else None
            new_waiting_reason = row["waiting_reason"] if row else None
        elif needs_you_tool_reason is not None:
            new_last_event = NEEDS_YOU_TOOL_SENTINEL
            new_waiting_reason = needs_you_tool_reason
        else:
            new_last_event = event
            new_waiting_reason = _extract_waiting_reason(event, payload)

        first_prompt = prompt_text if event == "UserPromptSubmit" else None
        last_prompt = _truncate(prompt_text) if event == "UserPromptSubmit" else None
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
                    now,
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
            else "irrelevant_notification" if irrelevant_notification else None
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


def run(tool: str, stdin_stream=None) -> None:
    """Entry point used by the CLI. Never raises, never prints."""
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
