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
# Prompts Claude Code injects on its own (a background task finishing, a
# message from another agent or session): they start a turn, but nobody
# typed them.
DEFAULT_INJECTED_PREFIXES = ("<task-notification>", "<agent-message", "<cross-session-message")

# events.prompt_kind values. NULL (a row recorded before migration 7 that
# `classify_prompts` has not reached) reads as human.
PROMPT_KINDS = ("human", "loop", "cron", "injected")

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
    r"(\*\*)?(Quiet|Idle|Nothing (to do|actionable|to act on|has changed|(else )?changed"
    r"|new|outstanding)"
    r"|Unchanged|Still\b|Holding|No-op|Waiting on|Standing by|Awaiting"
    r"|Loop (stays )?stopped|No changes?|Second|Third|Fourth)\b",
    re.IGNORECASE,
)

# A status line whose lead sentence only restates what is waiting on the
# human ("Two things are still waiting on you:", "PR #5 still needs your
# approval.") is as quiet as an idle-worded one, unless that sentence also
# reports an event (EVENT_RE) or carries a link.
WAITING_RE = re.compile(
    r"\b(still (waiting|pending|blocked|needs? (you|your)\b|awaits? (you|your)\b)"
    r"|waiting (on|for) (you|your)\b|blocked on (you|your)\b)",
    re.IGNORECASE,
)
SENTENCE_END_RE = re.compile(r"(?<=[.!?:;])\s")

# ...unless the message reports a hard event anywhere in it ("Nothing changed
# on the plan, but CI went red on PR #5"), unless negated or pending ("has
# not merged", "no tests failed", "None of the checks failed", "needs to be
# merged") or background in a since/until/once/if/unless clause ("the queue
# is empty since the logging work merged"). An adjectival "merged" ("the
# merged PR", "PR #5 (merged yesterday)", "merged-upstream") is no event;
# "I merged PR #7" is (see ADJECTIVAL_RE).
# Softer events ("deployed", "approved", "pushed") behind a quiet or waiting
# lead are restatements of a known state and stay quiet.
STRONG_EVENT_RE = re.compile(
    r"\b((?<!\()merged(?!-)"
    r"|opened|filed|failed|CI (is |went )?red)\b",
    re.IGNORECASE,
)
# A negator directly before the event, or "no"/"none"/"nothing" and a short
# subject of the same clause ("no tests", "none of the checks", "nothing
# has") with no conjunction or punctuation in between.
NEGATED_RE = re.compile(
    r"((\b(not|never|no|nothing)|n't)\s+(yet\s+)?((been|be|get)\s+)?"
    r"|\bto\s+(be|get)\s+"
    r"|\b(no|none|nothing)"
    r"([^\S\n]+(?!(but|and|or|so|then|though|although|while|however)\b)[\w'#-]+){1,4}[^\S\n]+)$",
    re.IGNORECASE,
)
BACKGROUND_RE = re.compile(r"\b(since|until|once|if|unless)\b", re.IGNORECASE)
# A determiner directly before "merged" makes it adjectival ("the merged
# PR", "its merged branch").
ADJECTIVAL_RE = re.compile(
    r"\b(the|a|an|this|that|these|those|its|his|her|their|our|your|my)\s+$",
    re.IGNORECASE,
)

# ...or, however short, an idle-worded answer nonetheless reports news.
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

# Something happened that the human may want to know about. Events count
# every time (becoming "blocked" is one; "still blocked" or "blocked on you"
# restates a wait, see WAITING_RE); a PR/issue reference counts only while
# the message names a reference the session's earlier notes have not (see
# classify's seen_refs), and a status ("ready for review", "in Pending")
# only while its own clause names no reference or a new one, so a loop
# restating "PR #5 is ready for your review" every tick is kept once.
EVENT_RE = re.compile(
    r"\b(merged|approved|LGTM|CI (is |went )?(red|green)|heads-up|failed|repair|pushed"
    r"|opened|deployed|filed|Completed|(?<!still )blocked(?! on (you|your)\b))\b",
    re.IGNORECASE,
)
STATUS_RE = re.compile(r"\b(ready for (your )?review|in Pending)\b", re.IGNORECASE)
CLAUSE_END_RE = re.compile(r"[.;:!?](?=\s|$)|\n")
MILESTONE_REF_RE = re.compile(r"\b\w+#\d+\b|\bPRs? #\d+", re.IGNORECASE)
UPDATE_MIN_CHARS = 300

# References a note mentions, for the seen-ref rule: "repo#12" keys as
# "repo#12", and "PR #12", "PRs #12, #13" or a bare "#12" as "#12". An
# ordinal ("step #2", "item #3") is not a reference.
NAMED_REF_RE = re.compile(r"\b([A-Za-z_][\w.-]*)#(\d+)\b")
BARE_REF_RE = re.compile(
    r"(?<![\w#])(?<!step )(?<!item )(?<!part )(?<!phase )(?<!stage )(?<!option )#(\d+)\b",
    re.IGNORECASE,
)

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


def injected_prefixes(config: dict) -> tuple:
    """`prompt_injected_prefixes` from config.json, default
    DEFAULT_INJECTED_PREFIXES: a prompt starting with one of these was
    injected by Claude Code, not typed by a person or a loop."""
    return _string_list(config.get("prompt_injected_prefixes"), DEFAULT_INJECTED_PREFIXES)


def load_markers(home: Path) -> tuple:
    return markers(load_config(home))


CRON_PROMPT_CHARS = 200


def cron_key(text) -> str:
    """How a prompt is compared with a stored cron prompt: whitespace
    collapsed, first CRON_PROMPT_CHARS characters (the hook stores each
    session cron's prompt the same way, see hook._background_info)."""
    return " ".join(str(text).split())[:CRON_PROMPT_CHARS]


def is_cron_prompt(prompt, cron_prompts) -> bool:
    if not prompt or not isinstance(prompt, str) or not cron_prompts:
        return False
    key = cron_key(prompt)
    return bool(key) and key in cron_prompts


def is_unattended(prompt, prefixes, cron_prompts=()) -> bool:
    """True for a loop's prompt: it starts with one of `prefixes`
    (`note_unattended_prefixes`), or it is one of the session's scheduled
    crons (`cron_prompts`, as stored from its last Stop)."""
    if not prompt or not isinstance(prompt, str):
        return False
    text = prompt.lstrip()
    if any(text.startswith(prefix) for prefix in prefixes):
        return True
    return is_cron_prompt(prompt, cron_prompts)


def prompt_kind(text, *, injected=DEFAULT_INJECTED_PREFIXES,
                unattended=DEFAULT_UNATTENDED_PREFIXES, cron_prompts=()) -> str:
    """"injected" (it starts with an `injected` prefix, after leading
    whitespace), "loop" (a `note_unattended_prefixes` prefix), "cron" (its
    whitespace-collapsed first CRON_PROMPT_CHARS characters equal one of the
    session's stored `cron_prompts`), else "human"."""
    if not text or not isinstance(text, str):
        return "human"
    stripped = text.lstrip()
    if any(stripped.startswith(prefix) for prefix in injected):
        return "injected"
    if any(stripped.startswith(prefix) for prefix in unattended):
        return "loop"
    if is_cron_prompt(text, cron_prompts):
        return "cron"
    return "human"


def kind_is_unattended(kind, text, prefixes) -> bool:
    """The unattended flag for a turn whose (non-injected) prompt has this
    kind: a loop or cron prompt is; an unclassified (legacy NULL) prompt is
    when it starts with one of `prefixes`."""
    if kind in ("loop", "cron"):
        return True
    return kind is None and is_unattended(text, prefixes)


def _session_cron_prompts(conn) -> dict:
    found = {}
    for tool, session_id, value in conn.execute(
        "SELECT tool, session_id, cron_prompts FROM sessions WHERE cron_prompts IS NOT NULL"
    ):
        try:
            prompts = json.loads(value)
        except (TypeError, ValueError):
            continue
        if isinstance(prompts, list):
            found[(tool, session_id)] = tuple(p for p in prompts if isinstance(p, str))
    return found


def classify_prompts(conn, config: dict, *, reset=False) -> int:
    """Fill events.prompt_kind for parent UserPromptSubmit rows where it is
    NULL (recorded before the hook classified prompts). With reset, every
    parent prompt is re-classified so a changed prefix list applies to the
    recorded history, except that a row the hook recorded as "cron" keeps
    it unless it now matches an injected prefix: sessions.cron_prompts only
    reflects the latest Stop, so a deleted cron's ticks could not be
    re-derived. Returns the number of rows changed. Must run inside the
    caller's transaction."""
    injected = injected_prefixes(config)
    unattended = unattended_prefixes(config)
    crons = _session_cron_prompts(conn)
    query = (
        "SELECT id, tool, session_id, text, prompt_kind FROM events "
        "WHERE event='UserPromptSubmit' AND agent_id IS NULL"
    )
    if not reset:
        query += " AND prompt_kind IS NULL"
    updates = []
    for event_id, tool, session_id, text, current in conn.execute(query).fetchall():
        kind = prompt_kind(text, injected=injected, unattended=unattended,
                           cron_prompts=crons.get((tool, session_id), ()))
        if current == "cron" and kind != "injected":
            kind = "cron"
        if kind != current:
            updates.append((kind, event_id))
    for start in range(0, len(updates), 500):
        conn.executemany("UPDATE events SET prompt_kind=? WHERE id=?", updates[start:start + 500])
    return len(updates)


# -- classification --------------------------------------------------------------

def has_marker_line(text: str, marker_list) -> bool:
    if not marker_list:
        return False
    for line in text.splitlines():
        stripped = line.lstrip(MARKER_LEAD_CHARS)
        if any(stripped.startswith(marker) for marker in marker_list):
            return True
    return False


def refs(text) -> set:
    """PR/issue references in text, normalized (see NAMED_REF_RE)."""
    if not text or not isinstance(text, str):
        return set()
    found = {f"{name.lower()}#{num}" for name, num in NAMED_REF_RE.findall(text)}
    found.update(f"#{num}" for num in BARE_REF_RE.findall(text))
    return found


def session_refs(conn, tool, session_id) -> set:
    """Every reference the session's notes (open or dismissed) mention: the
    seen_refs the hook passes to classify for that session's next Stop."""
    seen = set()
    for row in conn.execute(
        "SELECT text FROM notes WHERE tool=? AND session_id=?", (tool, session_id)
    ):
        seen |= refs(row[0])
    return seen


def _lead_sentence(text: str) -> str:
    return SENTENCE_END_RE.split(first_line(text), maxsplit=1)[0]


def is_waiting_restatement(text: str) -> bool:
    """True when the message's lead sentence only says what is still
    waiting on the human, with no event or link in it."""
    lead = _lead_sentence(text)
    return bool(WAITING_RE.search(lead)) and not EVENT_RE.search(lead) and not URL_RE.search(lead)


def has_strong_event(text: str) -> bool:
    """A hard event (STRONG_EVENT_RE) anywhere in the text that is neither
    negated nor background (see BACKGROUND_RE) in its own clause."""
    for match in STRONG_EVENT_RE.finditer(text):
        before = text[: match.start()]
        clause = CLAUSE_END_RE.split(before)[-1]
        if match.group().lower() == "merged" and ADJECTIVAL_RE.search(before):
            continue
        if not NEGATED_RE.search(before) and not BACKGROUND_RE.search(clause):
            return True
    return False


def is_milestone(text: str, seen_refs=None) -> bool:
    if EVENT_RE.search(text) or URL_RE.search(text):
        return True
    seen = set(seen_refs or ())
    if MILESTONE_REF_RE.search(text) and refs(text) - seen:
        return True
    # A status is about its own clause: "PR #4 is in hand; the plan is ready
    # for your review" reports a new thing to review even with #4 seen.
    for clause in CLAUSE_END_RE.split(text):
        if STATUS_RE.search(clause):
            clause_refs = refs(clause)
            if not clause_refs or clause_refs - seen:
                return True
    return False


def classify(text, *, markers=DEFAULT_MARKERS, unattended=True, seen_refs=None):
    """Return "needs_you", "update", "error" or None for one final message.

    A marker line always counts (a request for the human is worth keeping
    in an attended turn too); "update" and "error" only count when the turn
    was unattended, since a human who is watching already read them.
    `seen_refs` is the set of references (`refs`) the same session's
    earlier notes already mention: a reference in it no longer makes a
    short message a milestone on its own, while an event word still does.
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
    if QUIET_RE.match(text) or is_waiting_restatement(text):
        if has_strong_event(text):
            return "update"
        if NEWS_RE.search(text) and len(text) >= NEWS_MIN_CHARS:
            return "update"
        return None
    if (
        PROGRESS_RE.search(text)
        and len(text) < PROGRESS_MAX_CHARS
        and not URL_RE.search(text)
    ):
        return None
    if is_milestone(text, seen_refs) or len(text) >= UPDATE_MIN_CHARS:
        return "update"
    return None


def failure_note_text(error, text) -> str:
    """The `error` note a parent StopFailure records: "<error>: <text>"
    (the documented error type, then the API error message), or whichever
    of the two exists; "" when neither does. Its first line is the
    session's waiting_reason (hook.failure_reason), and the hook and
    `notes --rebuild` both build the note with this, so they agree."""
    error = error.strip() if isinstance(error, str) else ""
    body = text.strip() if isinstance(text, str) else ""
    if error and body and body != error:
        return f"{error}: {body}"
    return error or body


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

def dismiss(conn, now_iso, ids=None, session=None, kinds=None) -> int:
    """Set dismissed_at on open notes: the given ids, or every open note
    when ids is None, narrowed to one (tool, session_id)'s with `session`
    and to the given kinds with `kinds`. Returns the number dismissed. Must
    run inside the caller's transaction."""
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
    query = "UPDATE notes SET dismissed_at=? WHERE dismissed_at IS NULL"
    params = [now_iso]
    if session is not None:
        query += " AND tool=? AND session_id=?"
        params.extend(session)
    if kinds is not None:
        kinds = [kind for kind in kinds if kind in KINDS]
        if not kinds:
            return 0
        query += f" AND kind IN ({','.join('?' for _ in kinds)})"
        params.extend(kinds)
    return conn.execute(query, params).rowcount


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


def _extra_field(extra, key):
    try:
        value = json.loads(extra) if extra else {}
    except (TypeError, ValueError):
        return None
    return value.get(key) if isinstance(value, dict) else None


def _replay_note(conn, stats, tool, session_id, kind, text, ts, event_id) -> bool:
    """Record one replayed note unless a dismissed note with the same
    fingerprint already covers its event. True when it was recorded."""
    covered = conn.execute(
        "SELECT 1 FROM notes WHERE tool=? AND session_id=? AND fingerprint=? "
        "AND dismissed_at IS NOT NULL AND last_event_id >= ? LIMIT 1",
        (tool, session_id, fingerprint(kind, _truncate(text).strip()), event_id),
    ).fetchone()
    if covered:
        stats["skipped_dismissed"] += 1
        return False
    record_note(conn, tool, session_id, kind, text, "backfill", ts, event_id=event_id)
    stats["recorded"] += 1
    return True


def rebuild(conn, config: dict, *, days=None, reset=False, now=None) -> dict:
    """Replay recorded parent Stop events through the classifier, and
    parent StopFailure events as `error` notes (failure_note_text).

    Without reset: replays events with rebuilt_through < id <= hook_from
    (the boundary the migration recorded; the hook owns every event above
    it), then moves rebuilt_through up to hook_from, so a second rebuild is
    a no-op whatever notes the hook wrote meanwhile. With reset: deletes
    open stop/backfill notes (never dismissed ones, never cli/inbox/provider
    ones) and replays every event up to MAX(events.id). Either way an event
    is skipped when a dismissed note with the same fingerprint already
    covers it (last_event_id >= the event's id), so a dismissed note is not
    resurrected. `days` narrows the replay to Stop events newer than that.
    First, `classify_prompts` fills every unclassified prompt's kind (and,
    with reset, re-applies the prefix lists). Each event's unattended flag
    comes from the nearest preceding non-injected parent UserPromptSubmit of
    the same session, the same prompt the hook reads from events
    (hook._last_parent_prompt), and its kind. Each event's seen_refs are
    the references of the session's notes that predate it: kept notes first
    seen at an earlier event (or, without an event, an earlier ts) plus the
    notes this replay recorded so far, so a replay agrees with what the hook
    would have decided at the time. Runs in one IMMEDIATE transaction.
    """
    marker_list = markers(config)
    prefixes = unattended_prefixes(config)
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days) if days is not None else None
    stats = {"scanned": 0, "recorded": 0, "skipped_dismissed": 0, "deleted": 0}

    conn.execute("BEGIN IMMEDIATE")
    try:
        stats["classified"] = classify_prompts(conn, config, reset=reset)
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
            kept_refs = {}
            for note_row in conn.execute(
                "SELECT tool, session_id, event_id, ts, text FROM notes"
            ).fetchall():
                note_refs = refs(note_row[4])
                if note_refs:
                    kept_refs.setdefault((note_row[0], note_row[1]), []).append(
                        (note_row[2], note_row[3], note_refs)
                    )
            replay_refs = {}
            rows = conn.execute(
                "SELECT id, ts, tool, session_id, event, text, prompt_kind, extra FROM events "
                "WHERE agent_id IS NULL AND id <= ? "
                "AND (event = 'UserPromptSubmit' "
                "     OR (event IN ('Stop', 'StopFailure') AND id > ?)) "
                "ORDER BY id",
                (upper, lower),
            )
            for row in rows.fetchall():
                event_id, ts, tool, session_id, event, text, kind_of_prompt, extra = tuple(row)
                key = (tool, session_id)
                if event == "UserPromptSubmit":
                    if text and kind_of_prompt != "injected":
                        last_prompt[key] = (text, kind_of_prompt)
                    continue
                if event == "StopFailure":
                    # The error note the hook recorded (_record_failure_note).
                    text = failure_note_text(_extra_field(extra, "error"), text)
                if not text or not text.strip():
                    continue
                if cutoff is not None:
                    when = _parse_ts(ts)
                    if when is None or when < cutoff:
                        continue
                stats["scanned"] += 1
                if event == "StopFailure":
                    if _replay_note(conn, stats, tool, session_id, "error", text, ts, event_id):
                        replay_refs.setdefault(key, set()).update(refs(text))
                    continue
                seen = set(replay_refs.get(key, ()))
                for note_event, note_ts, note_refs in kept_refs.get(key, ()):
                    earlier = (
                        note_event < event_id if note_event is not None
                        else bool(note_ts and ts and note_ts < ts)
                    )
                    if earlier:
                        seen |= note_refs
                prompt_text, prompt_kind_ = last_prompt.get(key, (None, None))
                kind = classify(
                    text, markers=marker_list,
                    unattended=kind_is_unattended(prompt_kind_, prompt_text, prefixes),
                    seen_refs=seen,
                )
                if not kind:
                    continue
                if _replay_note(conn, stats, tool, session_id, kind, text, ts, event_id):
                    replay_refs.setdefault(key, set()).update(refs(text))

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
