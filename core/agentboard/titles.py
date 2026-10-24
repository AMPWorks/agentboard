"""Session titles that follow the work.

Claude Code writes a session's `ai-title` once and then repeats it, so a
long-lived session keeps its opening summary forever. `refresh-links`
(network allowed; `snapshot` stays offline) therefore retitles sessions
whose work has moved on: once a session has `retitle_min_prompts` new
parent prompts since its last retitle, or has changed branch, a
configurable `retitle_command` reads a short context on stdin and prints a
new title. `agentboard title` lets a session or skill name itself.

Precedence (top wins): a user's own /rename (`custom`), never replaced; the
newest of a generated retitle (`generated`) or `agentboard title` (`cli`);
Claude Code's `ai`/`summary` title; the first prompt (`fallback`).
"""

import os
import shlex
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import diag
from . import links as links_mod

DEFAULT_MIN_PROMPTS = 5
DEFAULT_MAX_PER_RUN = 5
RETITLE_TIMEOUT_SECS = 60
# At most one attempt per session per this long: a broken command costs one
# timeout per session per hour rather than one per refresh, and a loop
# session's stream of tick prompts can't retitle it every refresh.
MIN_RETITLE_INTERVAL_MINUTES = 60
TITLE_MAX_CHARS = 60
CONTEXT_MAX_CHARS = 4000
CONTEXT_TURNS = 6
CONTEXT_TEXT_CHARS = 400

# The hook skips every event while this is set, so the retitle command's own
# `claude -p` run never lands on the board as a session of its own.
HOOK_SKIP_ENV = "AGENTBOARD_HOOK_SKIP"

RETITLE_INSTRUCTION = (
    "You name coding-agent sessions for a dashboard. The session's recent context is on "
    "stdin. Reply with one short title, at most 8 words, saying what the session is working "
    "on now. No quotes, no markdown, no trailing punctuation, nothing else."
)

# Where Claude Code's installer and package managers put the `claude` CLI,
# for when refresh-links runs with a minimal PATH (e.g. from the app).
CLAUDE_CLI_CANDIDATES = (
    "~/.local/bin/claude",
    "~/.claude/local/claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
)


def _find_claude():
    found = shutil.which("claude")
    if found:
        return found
    for candidate in CLAUDE_CLI_CANDIDATES:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def default_command():
    """The local `claude` CLI in print mode with Haiku, or None without it.

    The context on stdin is untrusted text copied from other sessions, so
    the run gets no tools at all: `--tools ""` drops the built-in set and
    `--strict-mcp-config` (with no `--mcp-config`) drops MCP servers.
    Naming a session never needs a tool. `--tools` takes a list, so it is
    followed by another flag, never by the positional instruction."""
    claude = _find_claude()
    if not claude:
        return None
    return [claude, "-p", "--model", "haiku", "--tools", "", "--strict-mcp-config",
            "--no-session-persistence", RETITLE_INSTRUCTION]


def retitle_command(config: dict):
    """`retitle_command` from config.json as an argv list: a list of
    strings, or a string split like a shell would. Absent means the
    default; null (or an empty value) turns retitling off."""
    if "retitle_command" not in config:
        return default_command()
    value = config.get("retitle_command")
    if isinstance(value, str) and value.strip():
        return shlex.split(value)
    if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
        return list(value)
    return None


def _int_setting(config, key, default, minimum):
    value = config.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        return default
    return value


def min_prompts(config: dict) -> int:
    return _int_setting(config, "retitle_min_prompts", DEFAULT_MIN_PROMPTS, 1)


def max_per_run(config: dict) -> int:
    return _int_setting(config, "retitle_max_per_run", DEFAULT_MAX_PER_RUN, 0)


def _now_iso(now):
    return now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parent_prompt_counts(conn) -> dict:
    return {
        (row[0], row[1]): row[2]
        for row in conn.execute(
            "SELECT tool, session_id, COUNT(*) FROM events "
            "WHERE event='UserPromptSubmit' AND agent_id IS NULL GROUP BY tool, session_id"
        )
    }


def parent_prompt_count(conn, tool, session_id) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM events WHERE tool=? AND session_id=? "
        "AND event='UserPromptSubmit' AND agent_id IS NULL",
        (tool, session_id),
    ).fetchone()[0]


def is_due(session, prompt_count, minimum) -> bool:
    """Enough new parent prompts since the last retitle, or a branch
    change since then (only once a retitle recorded a branch)."""
    baseline = session.get("retitle_prompt_count") or 0
    if prompt_count - baseline >= minimum:
        return True
    last_branch = session.get("retitle_branch")
    branch = session.get("branch")
    return bool(last_branch and branch and branch != last_branch)


def candidates(conn, config: dict, now=None) -> list:
    """Sessions due a retitle, most recently active first, capped at
    `retitle_max_per_run`. Never a session the user named (custom), one
    that is done (manually, by a clean exit or by its links) or has ended,
    a non-interactive one, one idle past the snapshot retention window, or
    one retitled (or attempted) under MIN_RETITLE_INTERVAL_MINUTES ago."""
    now = now or datetime.now(timezone.utc)
    cap = max_per_run(config)
    if cap <= 0:
        return []
    minimum = min_prompts(config)
    counts = parent_prompt_counts(conn)
    manual_done = {
        (row[0], row[1])
        for row in conn.execute("SELECT tool, session_id FROM triage WHERE status='done'")
    }
    retention = timedelta(days=links_mod.SNAPSHOT_RETENTION_DAYS)
    due = []
    for row in conn.execute("SELECT * FROM sessions").fetchall():
        session = dict(row)
        key = (session["tool"], session["session_id"])
        if key in manual_done or session.get("ended_at") or session.get("interactive") == 0:
            continue
        if session.get("title_source") == "custom":
            continue
        updated = links_mod._ts(session.get("updated_at"))
        if updated is None or now - updated > retention:
            continue
        count = counts.get(key, 0)
        if not is_due(session, count, minimum):
            continue
        attempted = links_mod._ts(session.get("retitled_at"))
        if attempted is not None and now - attempted < timedelta(
            minutes=MIN_RETITLE_INTERVAL_MINUTES
        ):
            continue
        session_links = [dict(link) for link in conn.execute(
            "SELECT * FROM links WHERE tool=? AND session_id=?", key
        ).fetchall()]
        if links_mod.links_done_reason(
            session_links, session.get("branch"), links_mod.last_parent_prompt_at(conn, *key)
        ):
            continue
        due.append((updated, session, count))
    due.sort(key=lambda item: item[0], reverse=True)
    return [(session, count) for _updated, session, count in due[:cap]]


def build_context(conn, session) -> str:
    """The retitle command's stdin: the current title, project, branch and
    the last few parent prompts and final assistant messages, oldest first,
    capped at CONTEXT_MAX_CHARS."""
    head = [
        f"Current title: {session.get('title') or '(none)'}",
        f"Project: {os.path.basename(session.get('cwd') or '') or '(unknown)'}",
        f"Branch: {session.get('branch') or '(none)'}",
    ]
    rows = conn.execute(
        "SELECT event, text FROM events WHERE tool=? AND session_id=? AND agent_id IS NULL "
        "AND event IN ('UserPromptSubmit', 'Stop') AND text IS NOT NULL AND text <> '' "
        "ORDER BY id DESC LIMIT ?",
        (session["tool"], session["session_id"], CONTEXT_TURNS * 2),
    ).fetchall()
    turns = []
    for event, text in reversed([tuple(row) for row in rows]):
        who = "User" if event == "UserPromptSubmit" else "Assistant"
        body = " ".join(text.split())[:CONTEXT_TEXT_CHARS]
        turns.append(f"{who}: {body}")
    if not turns:
        for label, key in (("User", "last_prompt"), ("Assistant", "last_assistant")):
            if session.get(key):
                turns.append(f"{label}: {' '.join(session[key].split())[:CONTEXT_TEXT_CHARS]}")
    text = "\n".join(head) + "\n\nRecent activity (oldest first):\n"
    budget = CONTEXT_MAX_CHARS - len(text)
    kept = []
    for turn in reversed(turns):
        if len(turn) + 1 > budget:
            break
        kept.append(turn)
        budget -= len(turn) + 1
    return text + "\n".join(reversed(kept)) + "\n"


def clean_title(output) -> str | None:
    """The first non-empty output line as a title: markdown, quotes and a
    "Title:" label stripped, trimmed to TITLE_MAX_CHARS."""
    for line in (output or "").splitlines():
        line = line.strip().strip("#*_`").strip()
        if line.lower().startswith("title:"):
            line = line[len("title:"):].strip()
        line = line.strip("\"'“”‘’").strip().rstrip(".").strip()
        if line:
            return line[:TITLE_MAX_CHARS].rstrip()
    return None


def run_command(command, context, timeout=RETITLE_TIMEOUT_SECS, cwd=None):
    """Default runner: the command with `context` on stdin, the hook
    disabled for anything it starts, bounded by `timeout`. Returns stdout,
    or raises (OSError, TimeoutExpired, CalledProcessError)."""
    env = dict(os.environ)
    env[HOOK_SKIP_ENV] = "1"
    result = subprocess.run(
        command, input=context, capture_output=True, text=True, timeout=timeout,
        cwd=cwd, env=env,
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, command, result.stdout,
                                            result.stderr)
    return result.stdout


def _record_attempt(conn, session, now_iso, *, title=None, count=None):
    if title is not None:
        conn.execute(
            "UPDATE sessions SET title=?, title_source='generated', retitled_at=?, "
            "retitle_prompt_count=?, retitle_branch=? "
            "WHERE tool=? AND session_id=? AND COALESCE(title_source, '') <> 'custom'",
            (title, now_iso, count, session.get("branch"), session["tool"], session["session_id"]),
        )
    else:
        conn.execute(
            "UPDATE sessions SET retitled_at=? WHERE tool=? AND session_id=?",
            (now_iso, session["tool"], session["session_id"]),
        )


def retitle_sessions(conn, config: dict, *, runner=None, now=None, cwd=None) -> int:
    """Retitle the sessions that are due (see candidates). Each failure is
    logged and skipped, leaving that title as it was. Returns the number
    retitled. Network only through the command itself."""
    command = retitle_command(config)
    if not command:
        return 0
    now = now or datetime.now(timezone.utc)
    runner = runner or run_command
    retitled = 0
    for session, count in candidates(conn, config, now):
        key = f"{session['tool']}:{session['session_id']}"
        started = time.monotonic()
        context = build_context(conn, session)
        try:
            output = runner(command, context, timeout=RETITLE_TIMEOUT_SECS, cwd=cwd)
        except subprocess.TimeoutExpired:
            diag.warn("titles", "retitle", session=key, outcome="timeout",
                      ms=diag.ms_since(started))
            _record_attempt(conn, session, _now_iso(now))
            continue
        except subprocess.CalledProcessError as exc:
            diag.warn("titles", "retitle", session=key, outcome="nonzero", rc=exc.returncode,
                      stderr=diag.tail_text(exc.stderr, 300), ms=diag.ms_since(started))
            _record_attempt(conn, session, _now_iso(now))
            continue
        except Exception as exc:
            diag.warn("titles", "retitle", session=key, outcome="error",
                      exc=type(exc).__name__, msg=str(exc), ms=diag.ms_since(started))
            _record_attempt(conn, session, _now_iso(now))
            continue
        title = clean_title(output)
        if not title:
            diag.warn("titles", "retitle", session=key, outcome="empty",
                      ms=diag.ms_since(started))
            _record_attempt(conn, session, _now_iso(now))
            continue
        _record_attempt(conn, session, _now_iso(now), title=title, count=count)
        retitled += 1
        diag.info("titles", "retitle", session=key, outcome="ok", prompts=count,
                  ms=diag.ms_since(started))
    return retitled


def set_cli_title(conn, tool, session_id, title, now=None) -> bool:
    """`agentboard title`: name a session (title_source='cli'). Resets the
    retitle baseline so a generated title is due only after the work moves
    on again. A user's custom title is never replaced: returns False."""
    now = now or datetime.now(timezone.utc)
    row = conn.execute(
        "SELECT title_source, branch FROM sessions WHERE tool=? AND session_id=?",
        (tool, session_id),
    ).fetchone()
    if row is None or row["title_source"] == "custom":
        return False
    conn.execute(
        "UPDATE sessions SET title=?, title_source='cli', retitled_at=?, "
        "retitle_prompt_count=?, retitle_branch=? WHERE tool=? AND session_id=?",
        (title[:TITLE_MAX_CHARS * 2], _now_iso(now), parent_prompt_count(conn, tool, session_id),
         row["branch"], tool, session_id),
    )
    return True

