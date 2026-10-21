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


def fetch_generic_pr(branch: str, cwd: str, run_gh=_run_gh):
    if not branch or branch in NON_FEATURE_BRANCHES:
        return None
    started = time.monotonic()

    def trace(level, outcome, **fields):
        diag.log(level, "links", "gh", branch=branch, cwd=cwd, outcome=outcome,
                 ms=diag.ms_since(started), **fields)

    try:
        result = run_gh(
            [
                "pr", "list", "--head", branch, "--state", "all", "--limit", "1",
                "--json", "number,state,url,title",
            ],
            cwd,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        trace("warn", "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "oserror",
              error=str(exc))
        return None
    if result.returncode != 0:
        trace("warn", "nonzero", rc=result.returncode,
              stderr=diag.tail_text(getattr(result, "stderr", None), 300))
        return None
    try:
        rows = json.loads(result.stdout)
    except ValueError:
        trace("warn", "bad_json", stdout=diag.tail_text(result.stdout, 200))
        return None
    if not rows:
        trace("debug", "none")
        return None
    row = rows[0]
    trace("debug", "found", ref=f"#{row.get('number')}", state=row.get("state"))
    return {
        "kind": "pr",
        "ref": f"#{row['number']}",
        "url": row.get("url"),
        "state": row.get("state"),
        "title": row.get("title"),
    }


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


def upsert_link(conn, tool, session_id, link, now_iso):
    """Insert or merge a link. A field left unset (None) keeps its old value."""
    conn.execute(
        "INSERT INTO links(tool, session_id, kind, ref, url, state, title, checked_at) "
        "VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(tool, session_id, kind, ref) DO UPDATE SET "
        "url=COALESCE(excluded.url, links.url), "
        "state=COALESCE(excluded.state, links.state), "
        "title=COALESCE(excluded.title, links.title), "
        "checked_at=excluded.checked_at",
        (tool, session_id, link["kind"], link["ref"], link.get("url"),
         link.get("state"), link.get("title"), now_iso),
    )


def links_done_reason(links):
    """Which done-by-link rule applies, if any: "pr merged", "link done", or None."""
    pr_links = [link for link in links if link["kind"] == "pr"]
    if pr_links and all(link["state"] in ("MERGED", "CLOSED") for link in pr_links):
        return "pr merged"
    if any(link.get("state") == "DONE" for link in links):
        return "link done"
    return None


def links_say_done(links) -> bool:
    return links_done_reason(links) is not None


def has_open_pr(links) -> bool:
    return any(link["kind"] == "pr" and link.get("state") == "OPEN" for link in links)


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
                for link in raw_links if isinstance(raw_links, list) else []:
                    if _is_valid_link(link):
                        upsert_link(conn, tool, session_id, link, now_iso)
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
            for link in raw_links if isinstance(raw_links, list) else []:
                if _is_valid_link(link):
                    upsert_link(conn, tool, session_id, link, now_iso)
            checked += 1
    return checked


def refresh_links(conn, run_gh=_run_gh, home: Path = None) -> int:
    """Update the links table. Network via `gh` and provider commands."""
    now = datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    config = load_config(home) if home else {}
    checked = 0

    if home is not None:
        checked += ingest_inbox(conn, home, now_iso)
        checked += run_providers(conn, config, now_iso)

    sessions = conn.execute("SELECT * FROM sessions").fetchall()
    has_gh = gh_available()
    gh_checked = 0
    for session in sessions:
        session_dict = dict(session)
        for link in collect_offline_links(session_dict, config):
            upsert_link(conn, session["tool"], session["session_id"], link, now_iso)

        if not has_gh:
            continue
        branch = session_dict.get("branch")
        if not branch or branch in NON_FEATURE_BRANCHES:
            continue
        existing = conn.execute(
            "SELECT * FROM links WHERE tool=? AND session_id=? AND kind='pr' "
            "ORDER BY checked_at DESC LIMIT 1",
            (session["tool"], session["session_id"]),
        ).fetchone()
        existing_dict = dict(existing) if existing else None
        if not should_check_generic_pr(existing_dict, now):
            continue
        pr_link = fetch_generic_pr(branch, session_dict.get("cwd"), run_gh)
        checked += 1
        gh_checked += 1
        if pr_link:
            upsert_link(conn, session["tool"], session["session_id"], pr_link, now_iso)
    diag.info(
        "links", "refresh", sessions=len(sessions), checked=checked,
        gh="available" if has_gh else "missing", gh_checked=gh_checked,
        providers=len(config.get("providers", []) or []),
    )
    return checked
