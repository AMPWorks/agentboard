"""Generic link/status integrations: no built-in knowledge of any specific
ticket system, team or host. Everything environment-specific comes in
through `agentboard link`/`status`, the inbox drop directory, provider
commands, or the optional `gh`-based PR detector.
"""

import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import diag
from . import notes as notes_mod

NON_FEATURE_BRANCHES = {"main", "master", "develop"}
GH_CACHE_MINUTES = 15
GH_TIMEOUT_SECS = 15
PROVIDER_TIMEOUT_SECS = 30

SESSION_ENV_CANDIDATES = [
    ("claude", "CLAUDE_CODE_SESSION_ID"),
    ("codex", "CODEX_SESSION_ID"),
    ("codex", "CODEX_THREAD_ID"),
]

DONE_LINK_STATES = {"MERGED", "CLOSED", "DONE"}
CLOSED_PR_STATES = ("MERGED", "CLOSED")

# Who reported a link. A link the session reported itself (cli, inbox,
# provider) is that session's own work; `gh` (the branch detector or the
# per-link refresh) and `ticket` (config patterns) only found it.
REPORTED_SOURCES = ("cli", "inbox", "provider")

# The snapshot drops done sessions older than this; refresh-links skips the
# per-link refresh for sessions idle longer, unless one of their PRs still
# awaits the user.
SNAPSHOT_RETENTION_DAYS = 7

# Fields every gh PR lookup asks for (`gh pr list` and `gh pr view`).
GH_PR_FIELDS = "number,state,url,title,createdAt,mergedAt,closedAt,headRefName,author"


def inbox_dir(home: Path) -> Path:
    return home / "inbox"


def config_path(home: Path) -> Path:
    return home / "config.json"


def load_config(home: Path) -> dict:
    path = config_path(home)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def compile_ticket_patterns(config: dict):
    patterns = []
    for entry in config.get("ticket_patterns", []):
        regex = entry.get("regex")
        url_template = entry.get("url")
        if not regex or not url_template:
            continue
        try:
            compiled = re.compile(regex)
        except re.error:
            continue
        patterns.append((compiled, url_template))
    return patterns


def extract_ticket_links(session: dict, patterns) -> list:
    if not patterns:
        return []
    texts = [session.get("branch"), session.get("first_prompt")]
    links = []
    seen = set()
    for text in texts:
        if not text:
            continue
        for compiled, url_template in patterns:
            for match in compiled.findall(text):
                ref = match if isinstance(match, str) else match[0]
                if ref in seen:
                    continue
                seen.add(ref)
                links.append(
                    {
                        "kind": "ticket",
                        "ref": ref,
                        "url": url_template.replace("{ref}", ref),
                        "state": None,
                        "title": None,
                    }
                )
    return links


def collect_offline_links(session: dict, config: dict) -> list:
    """Links derivable without network access: config-driven ticket refs."""
    return extract_ticket_links(session, compile_ticket_patterns(config))


# -- gh PR detector ------------------------------------------------------------

def gh_available() -> bool:
    return shutil.which("gh") is not None


def _run_gh(args, cwd, timeout=GH_TIMEOUT_SECS):
    return subprocess.run(
        ["gh"] + args, cwd=cwd, capture_output=True, text=True, timeout=timeout
    )


def should_check_generic_pr(existing_link, now):
    if existing_link is None:
        return True
    state = existing_link.get("state")
    if state in ("MERGED", "CLOSED"):
        return False
    checked_at = existing_link.get("checked_at")
    if not checked_at:
        return True
    try:
        checked = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    return now - checked > timedelta(minutes=GH_CACHE_MINUTES)


def _pr_from_gh_row(row) -> dict:
    """A gh PR JSON object -> a link dict (plus the PR author's login)."""
    state = row.get("state")
    closed_at = None
    if state == "MERGED":
        closed_at = row.get("mergedAt") or row.get("closedAt")
    elif state == "CLOSED":
        closed_at = row.get("closedAt")
    author = row.get("author")
    return {
        "kind": "pr",
        "ref": f"#{row['number']}",
        "url": row.get("url"),
        "state": state,
        "title": row.get("title"),
        "branch": row.get("headRefName") or None,
        "created_at": row.get("createdAt") or None,
        "closed_at": closed_at or None,
        "author": author.get("login") if isinstance(author, dict) else None,
    }


def _gh_json(args, cwd, run_gh, trace):
    """Run gh and parse its JSON stdout; None (traced) on any failure."""
    try:
        result = run_gh(args, cwd)
    except (OSError, subprocess.TimeoutExpired) as exc:
        trace("warn", "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "oserror",
              error=str(exc))
        return None
    if result.returncode != 0:
        trace("warn", "nonzero", rc=result.returncode,
              stderr=diag.tail_text(getattr(result, "stderr", None), 300))
        return None
    try:
        return _parse_json_stream(result.stdout)
    except ValueError:
        trace("warn", "bad_json", stdout=diag.tail_text(result.stdout, 200))
        return None


def _parse_json_stream(text):
    """Parse gh output: one JSON value, or the concatenated arrays
    `gh api --paginate` prints (one per page), merged into one list."""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty output")
    decoder = json.JSONDecoder()
    values = []
    pos = 0
    while pos < len(text):
        value, pos = decoder.raw_decode(text, pos)
        values.append(value)
        while pos < len(text) and text[pos].isspace():
            pos += 1
    if len(values) == 1:
        return values[0]
    merged = []
    for value in values:
        if not isinstance(value, list):
            raise ValueError("mixed paginated output")
        merged.extend(value)
    return merged


def fetch_generic_pr(branch: str, cwd: str, run_gh=_run_gh):
    """The newest PR whose head is `branch`, as a link dict, or None."""
    if not branch or branch in NON_FEATURE_BRANCHES:
        return None
    started = time.monotonic()

    def trace(level, outcome, **fields):
        diag.log(level, "links", "gh", branch=branch, cwd=cwd, outcome=outcome,
                 ms=diag.ms_since(started), **fields)

    rows = _gh_json(
        ["pr", "list", "--head", branch, "--state", "all", "--limit", "1",
         "--json", GH_PR_FIELDS],
        cwd, run_gh, trace,
    )
    if rows is None:
        return None
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        trace("debug", "none")
        return None
    row = rows[0]
    trace("debug", "found", ref=f"#{row.get('number')}", state=row.get("state"))
    return _pr_from_gh_row(row)


def fetch_pr(target: str, cwd, run_gh=_run_gh):
    """`gh pr view <url-or-number>` as a link dict, or None. A URL needs no
    cwd (pass None: the session's worktree may be gone); a bare number is
    resolved against the repository in `cwd`."""
    started = time.monotonic()

    def trace(level, outcome, **fields):
        diag.log(level, "links", "gh_view", target=target, outcome=outcome,
                 ms=diag.ms_since(started), **fields)

    row = _gh_json(["pr", "view", str(target), "--json", GH_PR_FIELDS], cwd, run_gh, trace)
    if not isinstance(row, dict) or row.get("number") is None:
        if row is not None:
            trace("warn", "bad_shape")
        return None
    trace("debug", "found", state=row.get("state"))
    return _pr_from_gh_row(row)


_PR_URL_RE = re.compile(r"^https?://[^/]+/([^/]+)/([^/]+)/pull/(\d+)")


def parse_pr_url(url):
    """(owner, repo, number) from a GitHub PR URL, or None."""
    match = _PR_URL_RE.match(url or "")
    if not match:
        return None
    return match.group(1), match.group(2), match.group(3)


def fetch_pr_responses(url, run_gh=_run_gh):
    """Every review and conversation comment on the PR at `url`, as
    [(login, user_type, ts)], from the REST endpoints (they carry
    `user.type`, which tells a bot from a person). None when the URL isn't
    a GitHub PR URL or either call fails."""
    parsed = parse_pr_url(url)
    if parsed is None:
        return None
    owner, repo, number = parsed
    started = time.monotonic()

    def trace(level, outcome, **fields):
        diag.log(level, "links", "gh_responses", url=url, outcome=outcome,
                 ms=diag.ms_since(started), **fields)

    responses = []
    for path, ts_key in (
        (f"repos/{owner}/{repo}/pulls/{number}/reviews", "submitted_at"),
        (f"repos/{owner}/{repo}/issues/{number}/comments", "created_at"),
    ):
        items = _gh_json(["api", path, "--paginate"], None, run_gh, trace)
        if items is None:
            return None
        if not isinstance(items, list):
            trace("warn", "bad_shape", path=path)
            return None
        for item in items:
            if not isinstance(item, dict):
                continue
            user = item.get("user") if isinstance(item.get("user"), dict) else {}
            responses.append((user.get("login"), user.get("type"), item.get(ts_key)))
    trace("debug", "ok", count=len(responses))
    return responses


def fetch_active_login(run_gh=_run_gh):
    """The login `gh` acts as (`gh api user`), or None."""
    started = time.monotonic()

    def trace(level, outcome, **fields):
        diag.log(level, "links", "gh_user", outcome=outcome, ms=diag.ms_since(started), **fields)

    data = _gh_json(["api", "user"], None, run_gh, trace)
    login = data.get("login") if isinstance(data, dict) else None
    return login if isinstance(login, str) and login else None


def is_bot(login, user_type) -> bool:
    return user_type == "Bot" or (isinstance(login, str) and login.endswith("[bot]"))


def automation_logins(config: dict) -> set:
    """`automation_logins` from config.json: extra accounts (a reviewer
    bot, an author bot) whose reviews and comments are not a response from
    the user. No defaults."""
    value = config.get("automation_logins")
    if not isinstance(value, list):
        return set()
    return {item.lower() for item in value if isinstance(item, str) and item}


def responded_at_from(responses, *, author=None, active_login=None, automation=(),
                      is_mid_turn=None):
    """The earliest review/comment time that counts as the user's response.

    A response counts unless its author is a bot, listed in `automation`,
    or the PR's author. One-account setups (the PR's author IS the active
    gh login): that login's responses count only when made while the
    owning session was not mid-turn (`is_mid_turn(ts)` False), since the
    user and the session then share one account. An active login that is
    not the PR's author is treated like any other human: with the user as
    the default gh login and sessions authoring through a separate token,
    the user's own reviews must clear `awaits_you`.
    """
    author_l = author.lower() if isinstance(author, str) else None
    me = active_login.lower() if isinstance(active_login, str) else None
    one_account = author_l is not None and author_l == me
    best = None
    best_ts = None
    for login, user_type, ts in responses or ():
        when = _ts(ts)
        if when is None or not isinstance(login, str) or not login:
            continue
        lowered = login.lower()
        if is_bot(login, user_type) or lowered in automation:
            continue
        if one_account and lowered == me:
            if is_mid_turn is None or is_mid_turn(ts):
                continue
        elif lowered == author_l:
            continue
        if best_ts is None or when < best_ts:
            best, best_ts = ts, when
    return best


# -- storage --------------------------------------------------------------------

def _is_valid_link(link) -> bool:
    """Shape check for a link reported from outside (an inbox drop file or
    a provider's stdout) before it reaches upsert_link, whose INSERT needs
    a non-empty kind and ref. Anything malformed is skipped, never raised:
    one bad entry from an external source must not take down `snapshot`
    or `refresh-links` for every other session."""
    if not isinstance(link, dict):
        return False
    return bool(link.get("kind")) and bool(link.get("ref"))


_EXTERNAL_LINK_FIELDS = ("kind", "ref", "url", "state", "title", "branch")


def _external_link(link) -> dict:
    """The fields AgentBoard takes from a reported link (an inbox drop or a
    provider row): the documented ones, as strings. Anything else (close or
    response times) is AgentBoard's own to work out."""
    return {
        key: link.get(key) for key in _EXTERNAL_LINK_FIELDS
        if isinstance(link.get(key), str) and link.get(key)
    }


def session_branch(conn, tool, session_id):
    row = conn.execute(
        "SELECT branch FROM sessions WHERE tool=? AND session_id=?", (tool, session_id)
    ).fetchone()
    return row["branch"] if row else None


def upsert_link(conn, tool, session_id, link, now_iso, branch=None, source=None):
    """Insert or merge a link. A field left unset (None) keeps its old value.

    `link["branch"]`, when set, is authoritative (gh's headRefName or an
    explicit `agentboard link --branch`) and replaces the stored branch;
    the `branch` argument is the session's current branch, used only to
    fill a link that has none, never to move one (a session that moved on
    must not drag its old PR along). `source` never downgrades a link the
    session reported itself to one gh merely found. `closed_at` is stamped
    once: gh's merge/close time when known, else the first time the link
    is seen MERGED/CLOSED; a reopened (OPEN) link clears it.
    `responded_at` keeps the first value it gets.
    """
    state = link.get("state")
    explicit_branch = link.get("branch") or None
    explicit_closed = link.get("closed_at") or None
    self_authored = link.get("self_authored")
    if self_authored is not None:
        self_authored = 1 if self_authored else 0
    conn.execute(
        "INSERT INTO links(tool, session_id, kind, ref, url, state, title, checked_at, "
        "branch, closed_at, source, created_at, responded_at, self_authored) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(tool, session_id, kind, ref) DO UPDATE SET "
        "url=COALESCE(excluded.url, links.url), "
        "state=COALESCE(excluded.state, links.state), "
        "title=COALESCE(excluded.title, links.title), "
        "checked_at=excluded.checked_at, "
        "branch=CASE WHEN ? IS NOT NULL THEN ? "
        "  ELSE COALESCE(links.branch, excluded.branch) END, "
        "source=CASE WHEN links.source IN ('cli','inbox','provider') THEN links.source "
        "  ELSE COALESCE(excluded.source, links.source) END, "
        "created_at=COALESCE(excluded.created_at, links.created_at), "
        "responded_at=COALESCE(links.responded_at, excluded.responded_at), "
        "self_authored=COALESCE(excluded.self_authored, links.self_authored), "
        "closed_at=CASE WHEN excluded.state = 'OPEN' THEN NULL "
        "  ELSE COALESCE(?, links.closed_at, "
        "    CASE WHEN excluded.state IN ('MERGED','CLOSED') THEN excluded.checked_at END) END",
        (tool, session_id, link["kind"], link["ref"], link.get("url"),
         state, link.get("title"), now_iso,
         explicit_branch or branch,
         explicit_closed or (now_iso if state in CLOSED_PR_STATES else None),
         source, link.get("created_at") or None, link.get("responded_at") or None,
         self_authored,
         explicit_branch, explicit_branch, explicit_closed),
    )


def _ts(value):
    """A stored time as an aware datetime (naive means UTC), or None. Uses
    reconcile.parse_ts (imported lazily: reconcile imports this module)."""
    from .reconcile import parse_ts

    parsed = parse_ts(value) if isinstance(value, str) else None
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def is_feature_branch(branch) -> bool:
    return bool(branch) and branch not in NON_FEATURE_BRANCHES


def links_done_reason(links, branch=None, last_prompt_at=None):
    """Which done-by-link rule applies, if any: "pr merged", "link done", or None.

    "pr merged": the session is on a feature branch, at least one PR link
    records that same branch, every such link is MERGED/CLOSED, and no
    parent prompt is newer than the newest of their close times (legacy
    NULL closed_at falls back to checked_at). A link with no recorded
    branch never counts, and on main/master/develop (or with no branch)
    no PR link retires a session. "link done": any link reports DONE.
    """
    if is_feature_branch(branch):
        on_branch = [
            link for link in links
            if link.get("kind") == "pr" and link.get("branch") == branch
        ]
        if on_branch and all(link.get("state") in CLOSED_PR_STATES for link in on_branch):
            closes = [_ts(link.get("closed_at") or link.get("checked_at")) for link in on_branch]
            closes = [c for c in closes if c is not None]
            prompt = _ts(last_prompt_at)
            if prompt is None or (closes and not prompt > max(closes)):
                return "pr merged"
    if any(link.get("state") == "DONE" for link in links):
        return "link done"
    return None


def links_say_done(links, branch=None, last_prompt_at=None) -> bool:
    return links_done_reason(links, branch, last_prompt_at) is not None


def pr_is_owned(link, session, now=None) -> bool:
    """A PR link is the session's own work when the session reported it
    itself (cli/inbox/provider), or when it was found by gh and created
    inside the session's lifetime (started_at to ended_at, or now). A PR
    that merely shares the session's branch from before is inherited."""
    if link.get("kind") != "pr":
        return False
    if link.get("source") in REPORTED_SOURCES:
        return True
    created = _ts(link.get("created_at"))
    started = _ts(session.get("started_at"))
    if created is None or started is None:
        return False
    end = _ts(session.get("ended_at")) or now or datetime.now(timezone.utc)
    return started <= created <= end


def pr_awaits_you(link, session, now=None) -> bool:
    """Owned, OPEN, no response from the user yet, and refreshable (has a
    URL: a link that can never be looked up could never clear)."""
    return (
        link.get("state") == "OPEN"
        and bool(link.get("url"))
        and not link.get("responded_at")
        and pr_is_owned(link, session, now)
    )


def awaiting_reason(awaiting) -> str | None:
    """The Review reason for owned PRs that await the user."""
    if not awaiting:
        return None
    refs = ", ".join(link["ref"] for link in awaiting)
    one_account = all(link.get("self_authored") for link in awaiting)
    if len(awaiting) == 1:
        return f"PR {refs} open, no response yet" if one_account else f"PR {refs} awaits your review"
    return f"PRs {refs} open, no response yet" if one_account else f"PRs {refs} await your review"


def record_external_notes(conn, tool, session_id, raw_notes, source, now_iso) -> int:
    """Record the optional `notes` list of an inbox drop file or provider
    row ([{"kind": "needs_you|update|error", "text": "..."}]). Malformed
    entries are skipped and logged, never raised, like malformed links."""
    if raw_notes is None:
        return 0
    if not isinstance(raw_notes, list):
        diag.warn("links", "note_skipped", source=source, session=f"{tool}:{session_id}",
                  reason="not_a_list")
        return 0
    recorded = 0
    for note in raw_notes:
        if not notes_mod.is_valid_note(note):
            diag.warn("links", "note_skipped", source=source, session=f"{tool}:{session_id}",
                      reason="invalid")
            continue
        notes_mod.record_note(conn, tool, session_id, note["kind"], note["text"], source, now_iso)
        recorded += 1
    return recorded


# -- session resolution -----------------------------------------------------------

def resolve_session_from_env(conn):
    for tool, var in SESSION_ENV_CANDIDATES:
        value = os.environ.get(var)
        if not value:
            continue
        row = conn.execute(
            "SELECT tool, session_id FROM sessions WHERE tool=? AND session_id=?",
            (tool, value),
        ).fetchone()
        if row:
            return (row["tool"], row["session_id"])
    return None


def _ancestor_pids(start_pid, max_levels=10):
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,ppid="], capture_output=True, text=True, timeout=2
        )
    except Exception:
        return []
    table = {}
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            table[int(parts[0])] = int(parts[1])
        except ValueError:
            continue
    pids = []
    pid = start_pid
    for _ in range(max_levels):
        if pid is None or pid in pids:
            break
        pids.append(pid)
        pid = table.get(pid)
        if pid is None or pid <= 1:
            break
    return pids


def resolve_session_from_ancestors(conn, start_pid):
    pids = set(_ancestor_pids(start_pid))
    if not pids:
        return None
    rows = conn.execute(
        "SELECT tool, session_id, pid FROM sessions WHERE pid IS NOT NULL"
    ).fetchall()
    for row in rows:
        if row["pid"] in pids:
            return (row["tool"], row["session_id"])
    return None


def resolve_session_from_cwd(conn, cwd):
    if not cwd:
        return None
    rows = conn.execute(
        "SELECT tool, session_id, cwd FROM sessions "
        "WHERE cwd IS NOT NULL ORDER BY updated_at DESC"
    ).fetchall()
    cwd = cwd.rstrip("/")
    for row in rows:
        session_cwd = (row["cwd"] or "").rstrip("/")
        if not session_cwd:
            continue
        if cwd == session_cwd or cwd.startswith(session_cwd + "/") or session_cwd.startswith(cwd + "/"):
            return (row["tool"], row["session_id"])
    return None


def resolve_session(conn, explicit=None, cwd=None, pid=None):
    if explicit:
        tool, _, session_id = explicit.partition(":")
        if tool and session_id:
            return tool, session_id
        return None
    found = resolve_session_from_env(conn)
    if found:
        return found
    found = resolve_session_from_ancestors(conn, pid if pid is not None else os.getppid())
    if found:
        return found
    found = resolve_session_from_cwd(conn, cwd if cwd is not None else os.getcwd())
    if found:
        return found
    return None


# -- inbox ------------------------------------------------------------------------

def ingest_inbox(conn, home: Path, now_iso: str) -> int:
    """Consume $AGENTBOARD_HOME/inbox/*.json, deleting each after processing."""
    directory = inbox_dir(home)
    if not directory.is_dir():
        return 0
    count = 0
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            diag.warn("links", "inbox_drop", file=path.name, reason="unreadable", error=str(exc))
            path.unlink(missing_ok=True)
            continue
        if not isinstance(data, dict):
            diag.warn("links", "inbox_drop", file=path.name, reason="not_an_object")
            path.unlink(missing_ok=True)
            continue
        try:
            session = data.get("session")
            cwd = data.get("cwd")
            resolved = resolve_session(
                conn,
                explicit=session if isinstance(session, str) else None,
                cwd=cwd if isinstance(cwd, str) else None,
            )
            if resolved:
                tool, session_id = resolved
                raw_links = data.get("links")
                current_branch = session_branch(conn, tool, session_id)
                for link in raw_links if isinstance(raw_links, list) else []:
                    if _is_valid_link(link):
                        upsert_link(conn, tool, session_id, _external_link(link), now_iso,
                                    branch=current_branch, source="inbox")
                record_external_notes(conn, tool, session_id, data.get("notes"), "inbox", now_iso)
                status_text = data.get("status")
                if status_text and isinstance(status_text, str):
                    conn.execute(
                        "UPDATE sessions SET status_text=?, status_until=NULL "
                        "WHERE tool=? AND session_id=?",
                        (status_text, tool, session_id),
                    )
                count += 1
                diag.debug("links", "inbox", file=path.name, session=f"{tool}:{session_id}")
            else:
                diag.info("links", "inbox_drop", file=path.name, reason="no_session",
                          session=session, cwd=cwd)
        finally:
            path.unlink(missing_ok=True)
    return count


# -- provider commands --------------------------------------------------------------

def open_sessions_payload(conn) -> list:
    rows = conn.execute(
        "SELECT s.tool, s.session_id, s.cwd, s.branch, s.title, s.first_prompt "
        "FROM sessions s LEFT JOIN triage t "
        "ON t.tool=s.tool AND t.session_id=s.session_id "
        "WHERE t.status IS NULL OR t.status != 'done'"
    ).fetchall()
    return [
        {
            "session": f"{row['tool']}:{row['session_id']}",
            "cwd": row["cwd"],
            "branch": row["branch"],
            "title": row["title"],
            "first_prompt": row["first_prompt"],
        }
        for row in rows
    ]


def run_providers(conn, config: dict, now_iso: str) -> int:
    checked = 0
    payload = json.dumps(open_sessions_payload(conn)).encode("utf-8")
    for provider in config.get("providers", []):
        command = provider.get("command")
        if not command:
            continue
        name = provider.get("name") or (
            command[0] if isinstance(command, list) and command else str(command)
        )
        started = time.monotonic()

        def trace(level, outcome, **fields):
            diag.log(level, "links", "provider", name=name, outcome=outcome,
                     ms=diag.ms_since(started), **fields)

        try:
            result = subprocess.run(
                command, input=payload, capture_output=True,
                timeout=PROVIDER_TIMEOUT_SECS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            trace("warn", "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "oserror",
                  error=str(exc))
            continue
        if result.returncode != 0:
            trace("warn", "nonzero", rc=result.returncode,
                  stderr=diag.tail_text(result.stderr, 300))
            continue
        try:
            rows = json.loads(result.stdout.decode("utf-8", "replace"))
        except ValueError:
            trace("warn", "bad_json", rc=0, stdout=diag.tail_text(result.stdout, 200))
            continue
        if not isinstance(rows, list):
            trace("warn", "not_a_list", rc=0)
            continue
        trace("info", "ok", rc=0, rows=len(rows))
        for row in rows:
            if not isinstance(row, dict):
                continue
            session = row.get("session")
            if not isinstance(session, str) or ":" not in session:
                continue
            tool, session_id = session.split(":", 1)
            if not tool or not session_id:
                continue
            raw_links = row.get("links")
            current_branch = session_branch(conn, tool, session_id)
            for link in raw_links if isinstance(raw_links, list) else []:
                if _is_valid_link(link):
                    upsert_link(conn, tool, session_id, _external_link(link), now_iso,
                                branch=current_branch, source="provider")
            record_external_notes(conn, tool, session_id, row.get("notes"), "provider", now_iso)
            checked += 1
    return checked


def should_check_link(link, now) -> bool:
    """Per-link refresh throttle: an OPEN PR link is re-fetched once its
    last check is older than GH_CACHE_MINUTES. MERGED/CLOSED links are
    final and never re-fetched."""
    return link.get("state") == "OPEN" and should_check_generic_pr(link, now)


class GhContext:
    """Per-refresh state shared by the gh lookups: the runner, the active
    login (fetched once, lazily), the automation logins from config, and
    per-URL caches so one PR linked to several sessions costs one fetch."""

    def __init__(self, run_gh=_run_gh, config=None):
        self.run_gh = run_gh
        self.automation = automation_logins(config or {})
        self._login = None
        self._login_fetched = False
        self.pr_by_url = {}
        self.responses_by_url = {}
        self.mid_turn_by_session = {}
        self.fetches = 0

    def active_login(self):
        if not self._login_fetched:
            self._login_fetched = True
            self._login = fetch_active_login(self.run_gh)
        return self._login

    def pr(self, url):
        if url not in self.pr_by_url:
            self.fetches += 1
            self.pr_by_url[url] = fetch_pr(url, None, self.run_gh)
        return self.pr_by_url[url]

    def responses(self, url):
        if url not in self.responses_by_url:
            self.responses_by_url[url] = fetch_pr_responses(url, self.run_gh)
        return self.responses_by_url[url]

    def mid_turn(self, conn, tool, session_id):
        """mid_turn_checker for one session, built once per refresh."""
        key = (tool, session_id)
        if key not in self.mid_turn_by_session:
            self.mid_turn_by_session[key] = mid_turn_checker(conn, tool, session_id)
        return self.mid_turn_by_session[key]


def last_parent_prompt_at(conn, tool, session_id):
    """The ts of a session's newest parent UserPromptSubmit, or None."""
    row = conn.execute(
        "SELECT ts FROM events WHERE tool=? AND session_id=? AND event='UserPromptSubmit' "
        "AND agent_id IS NULL ORDER BY id DESC LIMIT 1",
        (tool, session_id),
    ).fetchone()
    return row[0] if row else None


def last_parent_prompt_times(conn) -> dict:
    """last_parent_prompt_at for every session in one pass:
    {(tool, session_id): ts}. A session with no parent prompt is absent."""
    return {
        (row[0], row[1]): row[2]
        for row in conn.execute(
            "SELECT e.tool, e.session_id, e.ts FROM events e JOIN ("
            "  SELECT MAX(id) AS id FROM events WHERE event='UserPromptSubmit' "
            "  AND agent_id IS NULL GROUP BY tool, session_id"
            ") newest ON e.id = newest.id"
        )
    }


TURN_START_EVENTS = ("UserPromptSubmit",)
TURN_END_EVENTS = ("Stop", "StopFailure", "Interrupt", "SessionEnd")


def mid_turn_checker(conn, tool, session_id):
    """A function ts -> True while the session was mid-turn at ts: its
    newest parent turn event at or before ts is a UserPromptSubmit.

    With no parent turn events at all (a backfill-origin row, or hooks
    installed late) the turn history is unknown, so every ts counts as
    mid-turn until the session's `ended_at`: an unverifiable history
    never lets the session's own comments clear its PR. The same holds
    for any ts before the first recorded turn event (a session whose
    only mark is its SessionEnd)."""
    ended = conn.execute(
        "SELECT ended_at FROM sessions WHERE tool=? AND session_id=?", (tool, session_id)
    ).fetchone()
    ended_at = _ts(ended["ended_at"]) if ended else None
    marks = []
    for row in conn.execute(
        "SELECT ts, event FROM events WHERE tool=? AND session_id=? AND agent_id IS NULL "
        "AND event IN ('UserPromptSubmit','Stop','StopFailure','Interrupt','SessionEnd') "
        "ORDER BY id",
        (tool, session_id),
    ):
        when = _ts(row["ts"])
        if when is not None:
            marks.append((when, row["event"]))
    marks.sort(key=lambda mark: mark[0])

    def is_mid_turn(ts):
        when = _ts(ts)
        if when is None:
            return True
        if not marks or when < marks[0][0]:
            return ended_at is None or when < ended_at
        last = None
        for mark_ts, event in marks:
            if mark_ts > when:
                break
            last = event
        return last in TURN_START_EVENTS

    return is_mid_turn


def _store_pr(conn, pr, rows, ctx, now_iso) -> None:
    """Upsert one fetched PR into every given link row (each keeps its own
    ref and session), with `responded_at` worked out per owning session."""
    responses = ctx.responses(pr["url"]) if pr.get("state") == "OPEN" and pr.get("url") else None
    login = ctx.active_login() if pr.get("author") else None
    self_authored = None
    if login and pr.get("author"):
        self_authored = pr["author"].lower() == login.lower()
    for row in rows:
        responded = None
        if responses:
            responded = responded_at_from(
                responses, author=pr.get("author"), active_login=login,
                automation=ctx.automation,
                is_mid_turn=ctx.mid_turn(conn, row["tool"], row["session_id"]),
            )
        update = {key: pr.get(key) for key in (
            "url", "state", "title", "branch", "created_at", "closed_at",
        )}
        update.update(kind="pr", ref=row["ref"], responded_at=responded,
                      self_authored=self_authored)
        upsert_link(conn, row["tool"], row["session_id"], update, now_iso, source="gh")


def refresh_pr_link(conn, link, run_gh=_run_gh, *, ctx=None, now_iso=None, cwd=None) -> bool:
    """Re-check one OPEN PR link by URL (`gh pr view <url>`, cwd=None) and
    store its state, times and first user response into every link row
    with that URL. A link with no URL is first looked up by number in the
    session's `cwd`, when that directory still exists. Returns True when a
    PR was fetched."""
    ctx = ctx or GhContext(run_gh)
    now_iso = now_iso or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    url = link.get("url")
    if not url:
        number = (link.get("ref") or "").lstrip("#")
        if not number.isdigit() or not cwd or not os.path.isdir(cwd):
            return False
        ctx.fetches += 1
        pr = fetch_pr(number, cwd, ctx.run_gh)
        if not pr or not pr.get("url"):
            return False
        url = pr["url"]
        ctx.pr_by_url[url] = pr
        upsert_link(conn, link["tool"], link["session_id"],
                    {"kind": "pr", "ref": link["ref"], "url": url}, now_iso, source="gh")
    else:
        pr = ctx.pr(url)
        if not pr:
            return False
    rows = [dict(row) for row in conn.execute(
        "SELECT tool, session_id, ref FROM links WHERE kind='pr' AND url=?", (url,)
    ).fetchall()]
    _store_pr(conn, pr, rows, ctx, now_iso)
    return True


def _skip_for_link_refresh(session, links_for_session, manual_done, now) -> bool:
    """Per-link refresh skips manual-done sessions and sessions idle past
    the snapshot retention window, unless one of their PRs awaits the
    user (refreshing it is how that clears)."""
    if manual_done:
        return True
    updated = _ts(session.get("updated_at"))
    if updated is None or now - updated <= timedelta(days=SNAPSHOT_RETENTION_DAYS):
        return False
    return not any(pr_awaits_you(link, session, now) for link in links_for_session)


def refresh_pr_links(conn, ctx, sessions, now, now_iso) -> int:
    """Refresh every OPEN PR link (any source, any branch) of the eligible
    sessions, each URL at most once. Returns the number of PRs fetched."""
    manual_done = {
        (row["tool"], row["session_id"])
        for row in conn.execute("SELECT tool, session_id FROM triage WHERE status='done'")
    }
    by_session = {}
    for row in conn.execute("SELECT * FROM links WHERE kind='pr'").fetchall():
        by_session.setdefault((row["tool"], row["session_id"]), []).append(dict(row))
    groups = {}
    for session in sessions:
        key = (session["tool"], session["session_id"])
        session_links = by_session.get(key, [])
        if not session_links:
            continue
        if _skip_for_link_refresh(session, session_links, key in manual_done, now):
            continue
        for link in session_links:
            if link.get("state") != "OPEN":
                continue
            group_key = link["url"] or ("no-url", key, link["ref"])
            groups.setdefault(group_key, []).append((session, link))
    fetched = 0
    for members in groups.values():
        if not any(should_check_link(link, now) for _session, link in members):
            continue
        session, link = members[0]
        if refresh_pr_link(conn, link, ctx=ctx, now_iso=now_iso, cwd=session.get("cwd")):
            fetched += 1
    return fetched


def refresh_links(conn, run_gh=_run_gh, home: Path = None, now=None, retitle_runner=None) -> int:
    """Update the links table. Network via `gh` and provider commands.
    With a `home` (the CLI's refresh-links), also retitles the sessions
    whose work has moved on (titles.retitle_sessions; `retitle_runner`
    replaces the command runner in tests)."""
    now = now or datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    config = load_config(home) if home else {}
    checked = 0

    if home is not None:
        checked += ingest_inbox(conn, home, now_iso)
        checked += run_providers(conn, config, now_iso)

    sessions = [dict(row) for row in conn.execute("SELECT * FROM sessions").fetchall()]
    has_gh = run_gh is not _run_gh or gh_available()
    ctx = GhContext(run_gh, config)
    for session in sessions:
        for link in collect_offline_links(session, config):
            upsert_link(conn, session["tool"], session["session_id"], link, now_iso,
                        branch=session.get("branch"), source="ticket")

    pr_fetched = 0
    gh_checked = 0
    if has_gh:
        # Every OPEN PR link first, by URL: this is what keeps PRs a
        # session reported from main (or any PR after its first) current.
        pr_fetched = refresh_pr_links(conn, ctx, sessions, now, now_iso)
        checked += pr_fetched
        # Then the branch detector: the newest PR link recorded for the
        # session's *current* branch decides whether to look; none means
        # look, so a branch change (or a legacy row with no branch)
        # re-checks.
        for session in sessions:
            branch = session.get("branch")
            if not is_feature_branch(branch):
                continue
            existing = conn.execute(
                "SELECT * FROM links WHERE tool=? AND session_id=? AND kind='pr' AND branch=? "
                "ORDER BY checked_at DESC LIMIT 1",
                (session["tool"], session["session_id"], branch),
            ).fetchone()
            if not should_check_generic_pr(dict(existing) if existing else None, now):
                continue
            pr_link = fetch_generic_pr(branch, session.get("cwd"), run_gh)
            checked += 1
            gh_checked += 1
            if pr_link:
                if pr_link.get("url"):
                    ctx.pr_by_url.setdefault(pr_link["url"], pr_link)
                _store_pr(conn, pr_link, [{
                    "tool": session["tool"], "session_id": session["session_id"],
                    "ref": pr_link["ref"],
                }], ctx, now_iso)
    retitled = 0
    if home is not None:
        from . import titles

        try:
            retitled = titles.retitle_sessions(conn, config, runner=retitle_runner, now=now,
                                               cwd=str(home))
        except Exception as exc:
            diag.warn("links", "retitle", outcome="error", exc=type(exc).__name__, msg=str(exc))
    diag.info(
        "links", "refresh", sessions=len(sessions), checked=checked,
        gh="available" if has_gh else "missing", gh_checked=gh_checked, pr_fetched=pr_fetched,
        providers=len(config.get("providers", []) or []), retitled=retitled,
    )
    return checked
