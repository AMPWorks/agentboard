"""Retained notes: the few final messages worth keeping across sessions.

Stdlib only, and no import of `links` or `diag`: `hook.py` imports this
module on the Stop fast path, so it must stay cheap to load. Every regex
table is a module constant compiled at import, so the rules can be tuned
(and `core/tests/fixtures/stop-corpus.jsonl` replayed) without touching
callers.

The classification rules are ported from the todo-pane terminal mod
(v0.3), which was drawn from real unattended-session transcripts: most
final messages are quiet-tick one-liners, progress notes, or repeated API
errors; what is worth keeping is an explicit request for the human (a
configured marker line, `USER_ACTION:` by default), a milestone (a PR
opened or merged, CI red, a plan ready), a repair or heads-up, or an
outage.
"""

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

KINDS = ("needs_you", "update", "error")
SOURCES = ("stop", "cli", "inbox", "provider", "backfill")

MAX_TEXT_CHARS = 4000
FINGERPRINT_CHARS = 200

DEFAULT_MARKERS = ("USER_ACTION:",)
DEFAULT_UNATTENDED_PREFIXES = ("# Autonomous loop",)

# Characters a marker line may be decorated with before the marker itself
# (list bullets, emphasis, block quotes, inline code), e.g.
# "- **USER_ACTION:** ...".
MARKER_LEAD_CHARS = " \t*_`>-"

# A transient failure the tool printed instead of an answer (optionally in
# bold, as QUIET_RE allows).
ERROR_RE = re.compile(
    r"(\*\*)?(API Error|Request timed out|Still down|You've hit your session limit)\b",
    re.IGNORECASE,
)

# Idle-worded answers restate what is already waiting, however long.
QUIET_RE = re.compile(
    r"(\*\*)?(Quiet|Idle|Nothing (to do|actionable|to act on|has changed|new|outstanding)"
    r"|Unchanged|Still\b|Holding|No-op|Waiting on|Standing by|Awaiting"
    r"|Loop (stays )?stopped|No change|Second|Third|Fourth)\b",
    re.IGNORECASE,
)

# ...unless an idle-worded answer nonetheless reports something new.
NEWS_RE = re.compile(
    r"\b(repair(ed)?|heads-up|found|bug|wrong|correct(ion|ed)?|fixed|broke|regression)\b",
    re.IGNORECASE,
)
NEWS_MIN_CHARS = 160

# Short progress notes on work another agent is still doing.
PROGRESS_RE = re.compile(
    r"\b(still (building|working|running)|takes no action|holds? the (project )?lock"
    r"|nothing new (to do )?(this|on this) tick|no new work|no autonomous work)\b",
    re.IGNORECASE,
)
PROGRESS_MAX_CHARS = 400

URL_RE = re.compile(r"https?://", re.IGNORECASE)

# Something happened that the human may want to know about.
MILESTONE_RE = re.compile(
    r"\b\w+#\d+\b|\bPR #\d+|\b(merged|approved|LGTM|ready for (your )?review"
    r"|CI (is |went )?(red|green)|heads-up|blocked|failed|repair|pushed|opened"
    r"|deployed|filed|in Pending|Completed)\b|https?://",
    re.IGNORECASE,
)
UPDATE_MIN_CHARS = 300

# Fingerprint normalization: ticks restate the same request with a fresh
# timestamp, clock time, wait or count, so those are dropped before the
# first FINGERPRINT_CHARS are compared. Other digits (PR and issue
# numbers) are kept, so PR #12 and PR #13 stay separate notes.
ISO_TIMESTAMP_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(z|[+-]\d{2}:?\d{2})?(\s*utc)?\b"
)
CLOCK_RE = re.compile(r"\b\d{1,2}:\d{2}(:\d{2})?(\s*(am|pm|utc))?\b")
DURATION_RE = re.compile(
    r"\b\d+\s?(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|ticks?)\b"
)
ATTEMPT_RE = re.compile(r"\battempt \d+(/\d+)?")
WHITESPACE_RE = re.compile(r"\s+")


# -- configuration ---------------------------------------------------------------

def load_config(home: Path) -> dict:
    """Read `$AGENTBOARD_HOME/config.json` with stdlib json only; {} when
    absent or unreadable (the same leniency as links.load_config)."""
    try:
        data = json.loads((Path(home) / "config.json").read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _string_list(value, default):
    if not isinstance(value, list):
        return tuple(default)
    return tuple(item for item in value if isinstance(item, str) and item)


def markers(config: dict) -> tuple:
    """`note_markers` from config.json, default ("USER_ACTION:",). An empty
    list turns marker detection off."""
    return _string_list(config.get("note_markers"), DEFAULT_MARKERS)


def unattended_prefixes(config: dict) -> tuple:
    """`note_unattended_prefixes` from config.json, default
    ("# Autonomous loop",): a turn whose prompt starts with one of these was
    not driven by a human watching the terminal."""
    return _string_list(config.get("note_unattended_prefixes"), DEFAULT_UNATTENDED_PREFIXES)


def load_markers(home: Path) -> tuple:
    return markers(load_config(home))


def is_unattended(prompt, prefixes) -> bool:
    if not prompt or not isinstance(prompt, str):
        return False
    text = prompt.lstrip()
    return any(text.startswith(prefix) for prefix in prefixes)


# -- classification --------------------------------------------------------------

def has_marker_line(text: str, marker_list) -> bool:
    if not marker_list:
        return False
    for line in text.splitlines():
        stripped = line.lstrip(MARKER_LEAD_CHARS)
        if any(stripped.startswith(marker) for marker in marker_list):
            return True
    return False


def classify(text, *, markers=DEFAULT_MARKERS, unattended=True):
    """Return "needs_you", "update", "error" or None for one final message.

    A marker line always counts (a request for the human is worth keeping
    in an attended turn too); "update" and "error" only count when the turn
    was unattended, since a human who is watching already read them.
    """
    if not text or not isinstance(text, str):
        return None
    text = text.strip()
    if not text:
        return None
    if has_marker_line(text, markers):
        return "needs_you"
    if not unattended:
        return None
    if ERROR_RE.match(text):
        return "error"
    if QUIET_RE.match(text):
        if NEWS_RE.search(text) and len(text) >= NEWS_MIN_CHARS:
            return "update"
        return None
    if (
        PROGRESS_RE.search(text)
        and len(text) < PROGRESS_MAX_CHARS
        and not URL_RE.search(text)
    ):
        return None
    if MILESTONE_RE.search(text) or len(text) >= UPDATE_MIN_CHARS:
        return "update"
    return None


def first_line(text) -> str:
    """The first non-empty line, with surrounding markdown emphasis removed."""
    for line in (text or "").strip().splitlines():
        line = line.strip().strip("*").strip()
        if line:
            return line
    return ""


def headline(text, marker_list=DEFAULT_MARKERS) -> str:
    """One line that stands for the note: its first marker line (so a
    request buried under a summary still leads), else its first line."""
    for line in (text or "").splitlines():
        stripped = line.lstrip(MARKER_LEAD_CHARS)
        if marker_list and any(stripped.startswith(marker) for marker in marker_list):
            return stripped.replace("**", "").strip()
    return first_line(text)


def normalize(text: str) -> str:
    value = (text or "").lower()
    value = ISO_TIMESTAMP_RE.sub(" ", value)
    value = CLOCK_RE.sub(" ", value)
    value = DURATION_RE.sub("#", value)
    value = ATTEMPT_RE.sub("attempt #", value)
    value = WHITESPACE_RE.sub(" ", value).strip()
    return value[:FINGERPRINT_CHARS]


def fingerprint(kind: str, text: str) -> str:
    """Dedupe key: kind plus the normalized first 200 chars. Errors key on
    their first line only, so one outage is one note with a count."""
    body = first_line(text) if kind == "error" else text
    return f"{kind}:{normalize(body)}"


# -- storage ----------------------------------------------------------------------

def _truncate(text: str) -> str:
    text = str(text)
    return text[:MAX_TEXT_CHARS] if len(text) > MAX_TEXT_CHARS else text


def record_note(conn, tool, session_id, kind, text, source, now_iso, event_id=None):
    """Insert a note, or bump the open note with the same fingerprint.

    Only open notes dedupe (the unique index is partial on dismissed_at IS
    NULL), so a repeat of a dismissed note starts a new row. A replayed
    older occurrence never moves an open note's last_ts/last_event_id
    backwards; it can only move its first-seen ts/event_id earlier. The
    newest occurrence's wording replaces `text` (an error's body changes
    while its first-line fingerprint stays put); an older replay keeps it. All
    writers stamp ts in the hook's `%Y-%m-%dT%H:%M:%S.%fZ` form, so the
    MIN/MAX string comparisons below are chronological. Returns the open
    note's id. Must run inside the caller's transaction.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown note kind: {kind!r}")
    text = _truncate(text).strip()
    if not text:
        raise ValueError("note text is empty")
    fp = fingerprint(kind, text)
    conn.execute(
        "INSERT INTO notes(tool, session_id, ts, last_ts, kind, text, fingerprint, "
        "count, source, event_id, last_event_id) "
        "VALUES (?,?,?,?,?,?,?,1,?,?,?) "
        "ON CONFLICT(tool, session_id, fingerprint) WHERE dismissed_at IS NULL "
        "DO UPDATE SET "
        "count = count + 1, "
        "text = CASE WHEN excluded.last_ts >= last_ts THEN excluded.text ELSE text END, "
        "ts = MIN(ts, excluded.ts), "
        "last_ts = MAX(last_ts, excluded.last_ts), "
        "event_id = CASE WHEN excluded.event_id IS NULL THEN event_id "
        "  WHEN event_id IS NULL THEN excluded.event_id "
        "  ELSE MIN(event_id, excluded.event_id) END, "
        "last_event_id = CASE WHEN excluded.last_event_id IS NULL THEN last_event_id "
        "  ELSE MAX(COALESCE(last_event_id, 0), excluded.last_event_id) END",
        (tool, session_id, now_iso, now_iso, kind, text, fp, source, event_id, event_id),
    )
    row = conn.execute(
        "SELECT id FROM notes WHERE tool=? AND session_id=? AND fingerprint=? "
        "AND dismissed_at IS NULL",
        (tool, session_id, fp),
    ).fetchone()
    return row[0] if row else None


def is_valid_note(note) -> bool:
    """Shape check for a note reported from outside (an inbox drop file or a
    provider row): kind one of KINDS, text a non-empty string. Anything
    malformed is skipped by the caller, never raised."""
    if not isinstance(note, dict):
        return False
    text = note.get("text")
    return note.get("kind") in KINDS and isinstance(text, str) and bool(text.strip())


# -- dismiss / rebuild ----------------------------------------------------------------

def dismiss(conn, now_iso, ids=None, session=None) -> int:
    """Set dismissed_at on open notes: the given ids, or every open note
    (optionally only one (tool, session_id)'s) when ids is None. Returns the
    number dismissed. Must run inside the caller's transaction."""
    if ids is not None:
        ids = [int(i) for i in ids]
        if not ids:
            return 0
        marks = ",".join("?" for _ in ids)
        cur = conn.execute(
            f"UPDATE notes SET dismissed_at=? WHERE dismissed_at IS NULL AND id IN ({marks})",
            (now_iso, *ids),
        )
        return cur.rowcount
    if session is not None:
        tool, session_id = session
        cur = conn.execute(
            "UPDATE notes SET dismissed_at=? WHERE dismissed_at IS NULL "
            "AND tool=? AND session_id=?",
            (now_iso, tool, session_id),
        )
        return cur.rowcount
    cur = conn.execute("UPDATE notes SET dismissed_at=? WHERE dismissed_at IS NULL", (now_iso,))
    return cur.rowcount


def _meta(conn) -> dict:
    return {row[0]: row[1] or 0 for row in conn.execute("SELECT key, value FROM notes_meta")}


def _parse_ts(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def rebuild(conn, config: dict, *, days=None, reset=False, now=None) -> dict:
    """Replay recorded parent Stop events through the classifier.

    Without reset: replays events with rebuilt_through < id <= hook_from
    (the boundary the migration recorded; the hook owns every event above
    it), then moves rebuilt_through up to hook_from, so a second rebuild is
    a no-op whatever notes the hook wrote meanwhile. With reset: deletes
    open stop/backfill notes (never dismissed ones, never cli/inbox/provider
    ones) and replays every event up to MAX(events.id). Either way an event
    is skipped when a dismissed note with the same fingerprint already
    covers it (last_event_id >= the event's id), so a dismissed note is not
    resurrected. `days` narrows the replay to Stop events newer than that.
    Each event's unattended flag comes from the nearest preceding parent
    UserPromptSubmit of the same session, the same prompt the hook reads
    from events (hook._last_parent_prompt_text). Runs in one IMMEDIATE
    transaction.
    """
    marker_list = markers(config)
    prefixes = unattended_prefixes(config)
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days) if days is not None else None
    stats = {"scanned": 0, "recorded": 0, "skipped_dismissed": 0, "deleted": 0}

    conn.execute("BEGIN IMMEDIATE")
    try:
        meta = _meta(conn)
        hook_from = int(meta.get("hook_from", 0))
        if reset:
            stats["deleted"] = conn.execute(
                "DELETE FROM notes WHERE dismissed_at IS NULL AND source IN ('stop', 'backfill')"
            ).rowcount
            lower = 0
            max_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
            upper = max(int(max_id), hook_from)
        else:
            lower = int(meta.get("rebuilt_through", 0))
            upper = hook_from

        if upper > lower:
            last_prompt = {}
            rows = conn.execute(
                "SELECT id, ts, tool, session_id, event, text FROM events "
                "WHERE agent_id IS NULL AND id <= ? "
                "AND (event = 'UserPromptSubmit' OR (event = 'Stop' AND id > ?)) "
                "ORDER BY id",
                (upper, lower),
            )
            for row in rows.fetchall():
                event_id, ts, tool, session_id, event, text = tuple(row)
                key = (tool, session_id)
                if event == "UserPromptSubmit":
                    if text:
                        last_prompt[key] = text
                    continue
                if not text or not text.strip():
                    continue
                if cutoff is not None:
                    when = _parse_ts(ts)
                    if when is None or when < cutoff:
                        continue
                stats["scanned"] += 1
                kind = classify(
                    text, markers=marker_list,
                    unattended=is_unattended(last_prompt.get(key), prefixes),
                )
                if not kind:
                    continue
                covered = conn.execute(
                    "SELECT 1 FROM notes WHERE tool=? AND session_id=? AND fingerprint=? "
                    "AND dismissed_at IS NOT NULL AND last_event_id >= ? LIMIT 1",
                    (tool, session_id, fingerprint(kind, _truncate(text).strip()), event_id),
                ).fetchone()
                if covered:
                    stats["skipped_dismissed"] += 1
                    continue
                record_note(conn, tool, session_id, kind, text, "backfill", ts, event_id=event_id)
                stats["recorded"] += 1

        new_mark = max(upper, int(meta.get("rebuilt_through", 0))) if not reset else upper
        conn.execute(
            "INSERT INTO notes_meta(key, value) VALUES ('rebuilt_through', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (new_mark,),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    stats["lower"] = lower
    stats["upper"] = upper
    return stats
