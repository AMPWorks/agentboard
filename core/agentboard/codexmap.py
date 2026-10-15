"""Map a Codex session to its real TUI process.

Codex 0.160+ runs hooks behind the shared app-server-daemon (see
hook.is_untrusted_daemon), so a session's hook can no longer resolve its own
pid/tty/terminal by walking its own process ancestry -- every session's hook
descends from the same long-lived daemon. There is also no exact join
available in Codex's own state: ~/.codex/logs_2.sqlite's process_uuid rows
carry no thread_id, and the daemon's own connection rows carry no client
pid. So this is a heuristic match against the live process list instead:
find real (non-daemon) codex TUI processes via `ps`, narrow to the one(s)
whose cwd (via `lsof`) matches the session's, and when more than one
matches, prefer a candidate not already claimed by a different live
session, then break remaining ties by which pid's tty was touched most
recently (the user just typed there).
"""

import os
import re
import subprocess
import time

from .reconcile import is_pid_alive, parse_ts

PS_TIMEOUT_SECS = 2
LSOF_TIMEOUT_SECS = 2

# How recent a candidate's tty activity must be, relative to the hook event
# that triggered this mapping, to count as "the user just typed there"
# rather than a coincidentally-matching but unrelated idle TUI.
TTY_RECENCY_WINDOW_SECONDS = 30

# Only these three env vars are ever read off a candidate TUI process (via
# `ps eww`) or stored -- never any other variable from its environment.
_ENV_VAR_RE = re.compile(r"(?:^|\s)(ITERM_SESSION_ID|TERM_SESSION_ID|TERM_PROGRAM)=(\S*)")


def _clean_tty(tty):
    if not tty or tty == "??":
        return None
    return tty


def _list_codex_tui_processes(ps_run=subprocess.run):
    """[{"pid", "tty"}, ...] for running codex TUI processes (not the daemon)."""
    try:
        out = ps_run(
            ["ps", "-axo", "pid=,tty=,comm="],
            capture_output=True, text=True, timeout=PS_TIMEOUT_SECS,
        )
    except Exception:
        return []
    procs = []
    for line in (out.stdout or "").splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid_str, tty, comm = parts
        if os.path.basename(comm) != "codex":
            continue
        if "app-server-daemon" in comm:
            continue
        tty = _clean_tty(tty)
        if tty is None:
            continue
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        procs.append({"pid": pid, "tty": tty})
    return procs


def _process_cwd(pid, lsof_run=subprocess.run):
    try:
        out = lsof_run(
            ["lsof", "-a", "-d", "cwd", "-p", str(pid), "-Fn"],
            capture_output=True, text=True, timeout=LSOF_TIMEOUT_SECS,
        )
    except Exception:
        return None
    for line in (out.stdout or "").splitlines():
        if line.startswith("n"):
            return line[1:]
    return None


def find_candidates(cwd, ps_run=subprocess.run, lsof_run=subprocess.run):
    """Codex TUI processes (never the daemon) whose cwd matches `cwd`."""
    if not cwd:
        return []
    target = os.path.normpath(cwd)
    matches = []
    for proc in _list_codex_tui_processes(ps_run=ps_run):
        proc_cwd = _process_cwd(proc["pid"], lsof_run=lsof_run)
        if proc_cwd is not None and os.path.normpath(proc_cwd) == target:
            matches.append(proc)
    return matches


def read_tui_env(pid, ps_run=subprocess.run):
    """ITERM_SESSION_ID/TERM_SESSION_ID/TERM_PROGRAM only, from pid's env."""
    try:
        out = ps_run(
            ["ps", "eww", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=PS_TIMEOUT_SECS,
        )
    except Exception:
        return {}
    values = {}
    for match in _ENV_VAR_RE.finditer(out.stdout or ""):
        values.setdefault(match.group(1), match.group(2))
    return values


def _tty_activity_time(tty, stat_fn=os.stat):
    path = tty if tty.startswith("/dev/") else f"/dev/{tty}"
    try:
        st = stat_fn(path)
    except OSError:
        return None
    return max(st.st_atime, st.st_mtime)


def _already_mapped_elsewhere(conn, session_id, pid, is_pid_alive_fn=is_pid_alive):
    row = conn.execute(
        "SELECT session_id FROM sessions WHERE tool='codex' AND pid=? AND session_id!=?",
        (pid, session_id),
    ).fetchone()
    if row is None:
        return False
    return is_pid_alive_fn(pid) is True


def choose_candidate(
    conn, session_id, candidates, now_epoch,
    stat_fn=os.stat, is_pid_alive_fn=is_pid_alive,
):
    """Narrow `candidates` ([{"pid", "tty"}]) down to exactly one, or None."""
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    preferred = [
        c for c in candidates
        if not _already_mapped_elsewhere(conn, session_id, c["pid"], is_pid_alive_fn=is_pid_alive_fn)
    ]
    pool = preferred or candidates
    if len(pool) == 1:
        return pool[0]

    now = now_epoch if now_epoch is not None else time.time()
    scored = []
    for c in pool:
        activity = _tty_activity_time(c["tty"], stat_fn=stat_fn)
        if activity is None:
            continue
        age = now - activity
        if 0 <= age <= TTY_RECENCY_WINDOW_SECONDS:
            scored.append((activity, c))
    if not scored:
        return None
    scored.sort(key=lambda pair: pair[0], reverse=True)
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None
    return scored[0][1]


def _at_epoch(at):
    if not at:
        return None
    dt = parse_ts(at)
    return dt.timestamp() if dt is not None else None


def map_codex_session(
    conn, session_id, cwd, at=None,
    ps_run=subprocess.run, lsof_run=subprocess.run,
    stat_fn=os.stat, is_pid_alive_fn=is_pid_alive,
):
    """Attempt to map a codex session row to its real TUI process.

    Returns True if a mapping was written, False if there were no
    candidates or the choice was still ambiguous.
    """
    candidates = find_candidates(cwd, ps_run=ps_run, lsof_run=lsof_run)
    chosen = choose_candidate(
        conn, session_id, candidates, _at_epoch(at),
        stat_fn=stat_fn, is_pid_alive_fn=is_pid_alive_fn,
    )
    if chosen is None:
        return False

    pid, tty = chosen["pid"], chosen["tty"]
    env = read_tui_env(pid, ps_run=ps_run)
    iterm_session_id = env.get("ITERM_SESSION_ID") or env.get("TERM_SESSION_ID")
    term_program = env.get("TERM_PROGRAM")

    conn.execute("BEGIN IMMEDIATE")
    try:
        # codex resume inside an existing TUI switches which session this
        # pid/tty now belongs to -- steal it from whatever row last held it.
        conn.execute(
            "UPDATE sessions SET pid=NULL, iterm_session_id=NULL, term_program=NULL, "
            "tty=NULL WHERE tool='codex' AND pid=? AND session_id!=?",
            (pid, session_id),
        )
        conn.execute(
            "UPDATE sessions SET pid=?, tty=?, iterm_session_id=?, term_program=? "
            "WHERE tool='codex' AND session_id=?",
            (pid, tty, iterm_session_id, term_program, session_id),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return True
