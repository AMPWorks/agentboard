"""State derivation: turns stored session/triage/link rows into a `state`."""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .hook import ACTIVITY_EVENTS, NEEDS_YOU_NOTIFICATION_TYPES, NEEDS_YOU_TOOL_SENTINEL
from .links import has_open_pr, links_done_reason

# Claude SessionEnd reasons that mean the user actually finished and quit,
# as opposed to e.g. /clear or /resume. Codex's SessionEnd reason is
# always "other" and gets no such filter -- any Codex SessionEnd counts.
CLEAN_EXIT_REASONS = {"prompt_input_exit", "logout"}

# Non-subagent events that mean "the agent is blocked on the user", used
# to confirm nothing needs_you-shaped was left pending right before a
# clean exit.
NEEDS_YOU_EVENT_NAMES = {"PermissionRequest", "StopFailure", NEEDS_YOU_TOOL_SENTINEL}

REVIEW_EVENTS = {"Stop", "Interrupt", "SessionEnd"}
NEEDS_YOU_EVENTS = {"PermissionRequest", "StopFailure", NEEDS_YOU_TOOL_SENTINEL}

RUNNING_FRESH_MINUTES = 10
STALE_MINUTES = 30
REVIEW_WINDOW_HOURS = 24
INTERRUPTED_WINDOW_HOURS = 24

STATE_PRIORITY = [
    "needs_you",
    "interrupted",
    "review",
    "running",
    "unconcluded",
    "snoozed",
    "done",
]


def parse_ts(value):
    if not value:
        return None
    try:
        value = value.replace("Z", "+00:00")
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def is_pid_alive(pid):
    if pid is None:
        return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OverflowError, TypeError, ValueError):
        return None


def load_claude_session_registry(home: Path) -> dict:
    """Map sessionId -> status payload from ~/.claude/sessions/<pid>.json."""
    registry = {}
    if not home.is_dir():
        return registry
    for entry in home.iterdir():
        if not entry.name.endswith(".json"):
            continue
        try:
            pid = int(entry.stem)
        except ValueError:
            continue
        try:
            data = json.loads(entry.read_text())
        except (OSError, ValueError):
            continue
        session_id = data.get("sessionId")
        if not session_id:
            continue
        data = dict(data)
        data["pid"] = pid
        registry[session_id] = data
    return registry


ROLLOUT_FRESH_MINUTES = 10


def _rollout_mtime_alive(transcript_path, now):
    if not transcript_path:
        return None
    try:
        mtime = os.path.getmtime(transcript_path)
    except OSError:
        return None
    mtime_dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
    return (now - mtime_dt) <= timedelta(minutes=ROLLOUT_FRESH_MINUTES)


def effective_liveness(tool, pid, session_id, claude_registry, transcript_path=None, now=None):
    """Return (alive, resolved_pid) honoring the Claude sessions registry.

    Codex 0.160+ runs hooks behind a shared app-server-daemon, so a real
    per-session pid often can't be resolved at all (hook.py already
    refuses to store the daemon's own pid -- see is_untrusted_daemon).
    For a pid-less Codex session, fall back to the rollout file's mtime;
    there's no reliable alternative client-process mapping (investigated:
    no pid/tty column anywhere in ~/.codex's own sqlite state, and the
    session index carries no process info either), so unlike Claude's
    unknown-pid case (returns None, "ambiguous"), a pid-less Codex session
    resolves definitively to alive or not -- stale means closed.
    """
    if pid is not None:
        alive = is_pid_alive(pid)
        if alive is not False:
            return alive, pid
        # Confirmed dead by signal 0; the registry cannot override that.
        return False, pid
    if tool == "claude" and claude_registry:
        entry = claude_registry.get(session_id)
        if entry:
            resolved_pid = entry.get("pid")
            return is_pid_alive(resolved_pid), resolved_pid
    if tool == "codex":
        now = now or datetime.now(timezone.utc)
        return bool(_rollout_mtime_alive(transcript_path, now)), None
    return None, None


def get_last_notification_type(conn, tool, session_id):
    row = conn.execute(
        "SELECT notification_type FROM events "
        "WHERE tool=? AND session_id=? AND event='Notification' AND agent_id IS NULL "
        "ORDER BY ts DESC, id DESC LIMIT 1",
        (tool, session_id),
    ).fetchone()
    return row["notification_type"] if row else None


def _is_clean_exit(tool, end_reason) -> bool:
    if tool == "claude":
        return end_reason in CLEAN_EXIT_REASONS
    if tool == "codex":
        return end_reason is not None
    return False


def last_turn_finished(conn, tool, session_id) -> bool:
    """True if, among this session's own (non-subagent) events, a Stop
    came after the last UserPromptSubmit and before SessionEnd, with
    nothing needs_you-shaped left pending after that Stop. Interrupt (or
    anything else in that gap, e.g. StopFailure) means it did not finish.
    """
    rows = conn.execute(
        "SELECT event FROM events WHERE tool=? AND session_id=? AND agent_id IS NULL "
        "ORDER BY ts ASC, id ASC",
        (tool, session_id),
    ).fetchall()
    events = [row["event"] for row in rows]

    session_end_idx = None
    last_prompt_idx = None
    for i, event in enumerate(events):
        if event == "UserPromptSubmit":
            last_prompt_idx = i
        if event == "SessionEnd":
            session_end_idx = i
    if session_end_idx is None:
        return False

    start = (last_prompt_idx + 1) if last_prompt_idx is not None else 0
    between = events[start:session_end_idx]
    if "Stop" not in between:
        return False
    if "Interrupt" in between:
        return False

    last_stop_pos = max(i for i, event in enumerate(between) if event == "Stop")
    after_stop = between[last_stop_pos + 1:]
    return not any(event in NEEDS_YOU_EVENT_NAMES for event in after_stop)


def compute_state(
    session,
    triage,
    links,
    now=None,
    alive=None,
    registry_entry=None,
    notification_type=None,
    turn_finished=None,
):
    """Compute (state, waiting_reason, auto_done, done_reason) for one session.

    `session` is a mapping of the sessions table columns. `triage` is a
    mapping of the triage row or None. `links` is a list of link row
    mappings. `alive` is the result of effective_liveness (True/False/None).
    `registry_entry` is the matching ~/.claude/sessions/<pid>.json payload,
    used only as a fallback when hook-driven state gives no signal.
    `notification_type` is the notification_type of the most recent
    Notification event, looked up from the events table by the caller when
    `last_event == "Notification"`. `turn_finished` is the caller's
    precomputed `last_turn_finished(conn, tool, session_id)`, only needed
    (and only looked at) once the session has actually ended.

    `done_reason` is one of "manual", "pr merged", "link done", "clean
    exit", or None; `auto_done` is True whenever done_reason is set and
    isn't "manual" -- i.e. the system decided it, not the user.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    last_event = session.get("last_event")
    waiting_reason = session.get("waiting_reason")
    ended_at = parse_ts(session.get("ended_at"))

    # Manual triage overrides win outright. A new UserPromptSubmit already
    # reopens a done session (hook.py), so this is never stale by surprise.
    if triage and triage.get("status") == "done":
        return "done", None, False, "manual"

    # An explicit `mark open` issued after the session already ended
    # rejects auto-done even though ended_at is still set (a genuine
    # resume instead clears ended_at itself -- see hook.py).
    auto_done_suppressed = False
    if triage and triage.get("status") == "open":
        triage_updated = parse_ts(triage.get("updated_at"))
        if triage_updated and ended_at and triage_updated > ended_at:
            auto_done_suppressed = True

    if triage and triage.get("status") == "snoozed":
        until = parse_ts(triage.get("until"))
        if until is not None and until > now:
            return "snoozed", None, False, None

    needs_you = last_event in NEEDS_YOU_EVENTS or (
        last_event == "Notification"
        and notification_type in NEEDS_YOU_NOTIFICATION_TYPES
    )
    if not needs_you and not last_event and registry_entry:
        if registry_entry.get("status") == "waiting":
            return "needs_you", registry_entry.get("waitingFor"), False, None
    if needs_you:
        return "needs_you", waiting_reason, False, None

    is_activity = last_event in ACTIVITY_EVENTS
    last_event_at = parse_ts(session.get("last_event_at"))
    age = (now - last_event_at) if last_event_at else None

    if is_activity:
        if alive is True:
            return "running", None, False, None
        would_be_interrupted = alive is False or (
            age is not None and age > timedelta(minutes=STALE_MINUTES)
        )
        if would_be_interrupted:
            # Only recent kills crowd the needs_you/interrupted group;
            # anything older than a day is just old, unconcluded work.
            if age is not None and age <= timedelta(hours=INTERRUPTED_WINDOW_HOURS):
                return "interrupted", None, False, None
            return "unconcluded", "interrupted", False, None
        if age is not None and age < timedelta(minutes=RUNNING_FRESH_MINUTES):
            return "running", None, False, None
        # Ambiguous: unknown liveness, age between the two thresholds.
        # Falls through below.

    # Only once needs_you/running/interrupted are ruled out does a
    # merged/closed/done link (or a clean-exit auto-done) retire the
    # session -- a live session with a merged PR that just got a new
    # prompt must still read as active.
    link_reason = links_done_reason(links)
    if link_reason:
        return "done", None, True, link_reason

    if ended_at and not auto_done_suppressed:
        tool = session.get("tool")
        clean_exit = _is_clean_exit(tool, session.get("end_reason"))
        todos_open = session.get("todos_open")
        no_open_todos = todos_open is None or todos_open <= 0
        no_open_pr = not has_open_pr(links)
        if clean_exit and bool(turn_finished) and no_open_todos and no_open_pr:
            return "done", None, True, "clean exit"

    is_review_event = last_event in REVIEW_EVENTS or (
        last_event == "Notification" and notification_type == "idle_prompt"
    )
    updated_at = parse_ts(session.get("updated_at"))
    review_age = (now - updated_at) if updated_at else None

    if (
        is_review_event
        and review_age is not None
        and review_age <= timedelta(hours=REVIEW_WINDOW_HOURS)
    ):
        return "review", None, False, None

    return "unconcluded", None, False, None
