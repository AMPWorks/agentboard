"""agentboard command-line entry point."""

import argparse
import functools
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import backfill as backfill_mod
from . import codexmap
from . import db
from . import diag
from . import hook as hook_mod
from . import links
from . import notes as notes_mod
from . import reconcile
from . import titles

STATES = ["needs_you", "interrupted", "running", "review", "unconcluded", "snoozed", "done"]

CLAUDE_EVENTS = [
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
    "PostToolUseFailure", "PermissionRequest", "Notification", "Stop",
    "StopFailure", "SubagentStart", "SubagentStop", "SessionEnd",
]
CODEX_EVENTS = [
    "SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse",
    "PostToolUse", "PermissionRequest", "Stop", "Interrupt",
    "SubagentStart", "SubagentStop", "PreCompact", "PostCompact",
]

DEFAULT_DONE_RETENTION_DAYS = links.SNAPSHOT_RETENTION_DAYS

# TERM_PROGRAM values AgentBoard knows how to turn into a real app name for
# `open -a`. Anything else: focus/resume can't do anything but fail.
TERM_PROGRAM_APP_NAMES = {
    "iTerm.app": "iTerm",
    "Apple_Terminal": "Terminal",
    "vscode": "Visual Studio Code",
    "ghostty": "Ghostty",
    "WezTerm": "WezTerm",
    "WarpTerminal": "Warp",
}


def _bin_path() -> Path:
    # Deliberately not .resolve()'d: --bin-path (install.sh's --dry-run
    # preview) is used as-is, with no symlink resolution. If this did
    # resolve, a HOME/AGENTBOARD_CORE_DIR that goes through a symlink
    # (e.g. macOS's /tmp -> /private/tmp) would make a real install
    # register a different command string than a dry-run preview of that
    # same install showed, and a dry-run run afterwards would show a
    # spurious diff rewriting every hook command.
    return Path(__file__).parent.parent / "bin" / "agentboard"


def _report(level, event, message, **fields):
    """Print `message` to stderr and log the same text, so stderr and
    agentboard.log always agree about what went wrong."""
    print(message, file=sys.stderr)
    diag.log(level, "cli", event, msg=message, **fields)


def _now_iso(now=None):
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# -- snapshot -----------------------------------------------------------------

def _first_line(text, limit=120):
    if not text:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    return stripped.splitlines()[0][:limit]


def _resume_command(tool, cwd, session_id):
    if not cwd:
        return None
    quoted_cwd = shlex.quote(cwd)
    if tool == "claude":
        return f"cd {quoted_cwd} && claude --resume {session_id}"
    if tool == "codex":
        return f"cd {quoted_cwd} && codex resume {session_id}"
    return None


def _session_links(conn, session, config):
    by_key = {}
    for link in links.collect_offline_links(session, config):
        by_key[(link["kind"], link["ref"])] = link
    rows = conn.execute(
        "SELECT kind, ref, url, state, title, checked_at, branch, closed_at, source, "
        "created_at, responded_at, self_authored FROM links WHERE tool=? AND session_id=?",
        (session["tool"], session["session_id"]),
    ).fetchall()
    for row in rows:
        by_key[(row["kind"], row["ref"])] = dict(row)
    return list(by_key.values())


def _can_focus(session):
    """True iff cmd_focus has some way to act on this session's terminal."""
    return session.get("term_program") in TERM_PROGRAM_APP_NAMES


def _session_status_text(session, now):
    text = session.get("status_text")
    if not text:
        return None
    until = reconcile.parse_ts(session.get("status_until"))
    if until is not None and until <= now:
        return None
    return text


SNAPSHOT_NOTES_PER_SESSION = 20


def _listed_notes(session_notes):
    """The notes a snapshot lists for one session: the newest
    SNAPSHOT_NOTES_PER_SESSION, plus every older open needs_you note, so a
    request for the user is never capped out by a loop's updates (the app
    relies on this to count a session's unlisted notes as update/error
    ones). Keeps the newest-first order."""
    listed = session_notes[:SNAPSHOT_NOTES_PER_SESSION]
    listed += [n for n in session_notes[SNAPSHOT_NOTES_PER_SESSION:] if n["kind"] == "needs_you"]
    return listed


def _open_notes_by_session(conn) -> dict:
    """{(tool, session_id): [open note rows, newest last_ts first]}."""
    by_key = {}
    for row in conn.execute("SELECT * FROM notes WHERE dismissed_at IS NULL").fetchall():
        by_key.setdefault((row["tool"], row["session_id"]), []).append(dict(row))
    oldest = datetime.min.replace(tzinfo=timezone.utc)

    def newest_first(note):
        ts = reconcile.parse_ts(note["last_ts"])
        if ts is not None and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (ts or oldest, note["id"])

    for rows in by_key.values():
        rows.sort(key=newest_first, reverse=True)
    return by_key


def _build_session_entry(conn, session, triage_by_key, claude_registry, config, now,
                         notes_by_key=None, prompt_times=None):
    session = dict(session)
    tool = session["tool"]
    session_id = session["session_id"]
    session_notes = (notes_by_key or {}).get((tool, session_id), [])
    triage = triage_by_key.get((tool, session_id))
    session_link_rows = _session_links(conn, session, config)

    notification_type = None
    if session.get("last_event") == "Notification":
        notification_type = reconcile.get_last_notification_type(conn, tool, session_id)

    registry_entry = None
    if tool == "claude" and session.get("pid") is None:
        registry_entry = claude_registry.get(session_id)

    alive, resolved_pid = reconcile.effective_liveness(
        tool, session.get("pid"), session_id, claude_registry,
        transcript_path=session.get("transcript_path"), now=now,
    )

    # Only a Codex clean exit asks whether the last turn finished (a clean
    # Claude exit is /exit at the prompt), so skip the full event scan for
    # everything else; this runs for every ended session on every poll.
    turn_finished = None
    if session.get("ended_at") and tool != "claude" and session.get("end_reason"):
        turn_finished = reconcile.last_turn_finished(conn, tool, session_id)

    marker_list = notes_mod.markers(config)

    def with_headline(note):
        if note is None:
            return None
        return dict(note, headline=notes_mod.headline(note["text"], marker_list))

    open_needs_you_note = with_headline(
        next((n for n in session_notes if n["kind"] == "needs_you"), None)
    )
    open_update_note = with_headline(
        next((n for n in session_notes if n["kind"] == "update"), None)
    )
    # One value for every rule that asks "has the user (or the next tick)
    # prompted since?": note release and the done-by-link new-work check.
    if prompt_times is None:
        last_prompt_at = links.last_parent_prompt_at(conn, tool, session_id)
    else:
        last_prompt_at = prompt_times.get((tool, session_id))
    unattended = notes_mod.is_unattended(
        session.get("last_prompt"), notes_mod.unattended_prefixes(config)
    )

    state, waiting_reason, auto_done, done_reason = reconcile.compute_state(
        session, triage, session_link_rows, now=now, alive=alive,
        registry_entry=registry_entry, notification_type=notification_type,
        turn_finished=turn_finished, open_needs_you_note=open_needs_you_note,
        last_prompt_at=last_prompt_at, open_update_note=open_update_note,
        unattended=unattended,
    )

    cwd = session.get("cwd")
    title = session.get("title") or _first_line(session.get("first_prompt")) or session_id

    return {
        "key": f"{tool}:{session_id}",
        "tool": tool,
        "session_id": session_id,
        "state": state,
        "waiting_reason": waiting_reason,
        "auto_done": auto_done,
        "done_reason": done_reason,
        "title": title,
        "title_source": session.get("title_source") if session.get("title") else (
            "fallback" if session.get("first_prompt") else None
        ),
        "cwd": cwd,
        "project": os.path.basename(cwd) if cwd else None,
        "branch": session.get("branch"),
        "first_prompt": session.get("first_prompt"),
        "last_prompt": session.get("last_prompt"),
        "last_assistant": session.get("last_assistant"),
        "started_at": session.get("started_at"),
        "updated_at": session.get("updated_at"),
        "ended_at": session.get("ended_at"),
        "alive": bool(alive),
        "interactive": True if session.get("interactive") is None else bool(session.get("interactive")),
        "can_focus": _can_focus(session),
        "resume_command": _resume_command(tool, cwd, session_id),
        "status_text": _session_status_text(session, now),
        "links": [
            {
                "kind": link["kind"],
                "ref": link["ref"],
                "url": link.get("url"),
                "state": link.get("state"),
                "title": link.get("title"),
                "branch": link.get("branch"),
                "owned": links.pr_is_owned(link, session, now),
                "awaits_you": links.pr_awaits_you(link, session, now),
            }
            for link in session_link_rows
        ],
        "triage": {
            "status": (triage["status"] if triage else "open"),
            "until": (triage["until"] if triage else None),
            "note": (triage["note"] if triage else None),
        },
        "notes": [
            {
                "id": note["id"],
                "kind": note["kind"],
                "text": note["text"],
                "ts": note["ts"],
                "last_ts": note["last_ts"],
                "count": note["count"],
                "source": note["source"],
            }
            for note in _listed_notes(session_notes)
        ],
        "notes_count": len(session_notes),
    }


def build_snapshot(conn, include_all=False, now=None):
    now = now or datetime.now(timezone.utc)
    home = db.home_dir()
    now_iso = _now_iso(now)
    links.ingest_inbox(conn, home, now_iso)

    sessions = conn.execute("SELECT * FROM sessions").fetchall()
    triage_rows = conn.execute("SELECT * FROM triage").fetchall()
    triage_by_key = {(row["tool"], row["session_id"]): dict(row) for row in triage_rows}
    claude_registry = reconcile.load_claude_session_registry(
        Path.home() / ".claude" / "sessions"
    )
    config = links.load_config(home)
    notes_by_key = _open_notes_by_session(conn)
    # One grouped query, not one per session on every 3 s poll.
    prompt_times = links.last_parent_prompt_times(conn)

    entries = [
        _build_session_entry(conn, session, triage_by_key, claude_registry, config, now,
                             notes_by_key, prompt_times)
        for session in sessions
    ]

    if not include_all:
        cutoff = now - timedelta(days=DEFAULT_DONE_RETENTION_DAYS)
        filtered = []
        for entry in entries:
            if not entry["interactive"]:
                continue
            if (
                entry["first_prompt"] is None
                and entry["last_prompt"] is None
                and entry["state"] != "needs_you"
            ):
                continue
            if entry["state"] == "done":
                updated = reconcile.parse_ts(entry["updated_at"])
                if updated is not None and updated < cutoff:
                    continue
            filtered.append(entry)
        entries = filtered

    state_rank = {name: i for i, name in enumerate(STATES)}

    def sort_key(entry):
        rank = state_rank.get(entry["state"], len(STATES))
        updated = reconcile.parse_ts(entry["updated_at"]) or datetime.min.replace(
            tzinfo=timezone.utc
        )
        return (rank, -updated.timestamp())

    entries.sort(key=sort_key)

    counts = {name: 0 for name in STATES}
    for entry in entries:
        if entry["state"] in counts:
            counts[entry["state"]] += 1
    # Only the notes of sessions this snapshot emits, so the total matches
    # what the app can show.
    counts["notes"] = sum(entry["notes_count"] for entry in entries)

    return {
        "generated_at": _now_iso(now),
        "counts": counts,
        "sessions": entries,
    }


def cmd_snapshot(args):
    conn = db.connect()
    try:
        snapshot = build_snapshot(conn, include_all=args.all)
    finally:
        conn.close()
    if args.json:
        print(json.dumps(snapshot))
    else:
        _print_snapshot_table(snapshot)
    return 0


def _print_snapshot_table(snapshot):
    header = f"{'STATE':<12} {'TOOL':<7} {'PROJECT':<24} {'BRANCH':<24} {'TITLE'}"
    print(header)
    for entry in snapshot["sessions"]:
        print(
            f"{entry['state']:<12} {entry['tool']:<7} "
            f"{(entry['project'] or ''):<24} {(entry['branch'] or ''):<24} "
            f"{entry['title']}"
        )


# -- mark ---------------------------------------------------------------------

def cmd_mark(args):
    conn = db.connect()
    try:
        now = _now_iso()
        if args.action == "done":
            status, until = "done", None
        elif args.action == "open":
            status, until = "open", None
        elif args.action == "snooze":
            hours = args.hours if args.hours is not None else 24
            until_dt = datetime.now(timezone.utc) + timedelta(hours=hours)
            status, until = "snoozed", _now_iso(until_dt)
        else:
            raise ValueError(f"unknown action {args.action}")

        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO triage(tool, session_id, status, until, note, updated_at) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(tool, session_id) DO UPDATE SET "
            "status=excluded.status, until=excluded.until, "
            "note=COALESCE(excluded.note, triage.note), updated_at=excluded.updated_at",
            (args.tool, args.session_id, status, until, args.note, now),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    diag.info(
        "cli", "mark", key=f"{args.tool}:{args.session_id}", action=args.action,
        status=status, until=until,
        note=None if args.note is None else f"<redacted:{len(args.note)} chars>",
    )
    return 0


# -- backfill / refresh-links --------------------------------------------------

def cmd_backfill(args):
    conn = db.connect()
    try:
        if args.session:
            tool, _, session_id = args.session.partition(":")
            if not tool or not session_id:
                _report("error", "backfill", f"error: --session must be TOOL:ID, got {args.session!r}")
                return 1
            result = backfill_mod.backfill_single_session(
                conn, tool, session_id, transcript_path=args.transcript
            )
            diag.log(
                "debug" if result == "updated" else "info", "backfill", "session",
                key=args.session, transcript=args.transcript, result=result,
            )
            print(f"session={args.session} result={result}")
            return 0
        stats = backfill_mod.run_backfill(conn, days=args.days)
    finally:
        conn.close()
    print(
        f"claude={stats['claude']} codex={stats['codex']} errors={stats['errors']} "
        f"daemon_pid_corrected={stats['daemon_pid_corrected']}"
    )
    return 0


def cmd_refresh_links(args):
    conn = db.connect()
    try:
        checked = links.refresh_links(conn, home=db.home_dir())
    finally:
        conn.close()
    print(f"refreshed {checked} link(s)")
    return 0


def cmd_link(args):
    conn = db.connect()
    try:
        resolved = links.resolve_session(conn, explicit=args.session)
        if not resolved:
            _report("warn", "link", "agentboard link: could not resolve a session",
                    explicit=args.session)
            return 0
        tool, session_id = resolved
        link = {
            "kind": args.kind,
            "ref": args.ref,
            "url": args.url,
            "state": args.state,
            "title": args.title,
            "branch": args.branch,
        }
        now_iso = _now_iso()
        conn.execute("BEGIN IMMEDIATE")
        links.upsert_link(conn, tool, session_id, link, now_iso,
                          branch=links.session_branch(conn, tool, session_id), source="cli")
        conn.execute("COMMIT")
    finally:
        conn.close()
    return 0


def cmd_status(args):
    conn = db.connect()
    try:
        resolved = links.resolve_session(conn, explicit=args.session)
        if not resolved:
            _report("warn", "status", "agentboard status: could not resolve a session",
                    explicit=args.session)
            return 0
        tool, session_id = resolved
        until = None
        if args.ttl is not None:
            until = _now_iso(datetime.now(timezone.utc) + timedelta(minutes=args.ttl))
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE sessions SET status_text=?, status_until=? "
            "WHERE tool=? AND session_id=?",
            (args.text, until, tool, session_id),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return 0


def cmd_title(args):
    text = " ".join((args.text or "").split())
    if not text:
        _report("error", "title", "agentboard title: --text is empty")
        return 1
    conn = db.connect()
    try:
        resolved = links.resolve_session(conn, explicit=args.session)
        if not resolved:
            _report("warn", "title", "agentboard title: could not resolve a session",
                    explicit=args.session)
            return 0
        tool, session_id = resolved
        conn.execute("BEGIN IMMEDIATE")
        try:
            applied = titles.set_cli_title(conn, tool, session_id, text)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    if not applied:
        _report("info", "title",
                "agentboard title: kept the session's own title (set by the user, or no such session)",
                key=f"{tool}:{session_id}")
        return 0
    diag.info("cli", "title", key=f"{tool}:{session_id}", text=f"<redacted:{len(text)} chars>")
    return 0


# -- notes ---------------------------------------------------------------------

def _note_dict(row) -> dict:
    return {
        "id": row["id"],
        "key": f"{row['tool']}:{row['session_id']}",
        "tool": row["tool"],
        "session_id": row["session_id"],
        "kind": row["kind"],
        "text": row["text"],
        "ts": row["ts"],
        "last_ts": row["last_ts"],
        "count": row["count"],
        "source": row["source"],
        "dismissed_at": row["dismissed_at"],
    }


def _explicit_session(value):
    """TOOL:ID -> (tool, session_id), or None when malformed."""
    tool, _, session_id = (value or "").partition(":")
    if not tool or not session_id:
        return None
    return tool, session_id


def cmd_note(args):
    text = (args.text or "").strip()
    if not text:
        _report("error", "note", "agentboard note: --text is empty")
        return 1
    conn = db.connect()
    try:
        resolved = links.resolve_session(conn, explicit=args.session)
        if not resolved:
            _report("warn", "note", "agentboard note: could not resolve a session",
                    explicit=args.session)
            return 0
        tool, session_id = resolved
        conn.execute("BEGIN IMMEDIATE")
        try:
            note_id = notes_mod.record_note(
                conn, tool, session_id, args.kind, text, "cli", _now_iso()
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    diag.info("cli", "note", key=f"{tool}:{session_id}", kind=args.kind, id=note_id,
              text=f"<redacted:{len(text)} chars>")
    return 0


def cmd_notes(args):
    if (args.days is not None or args.reset) and not args.rebuild:
        _report("error", "notes", "agentboard notes: --days and --reset need --rebuild")
        return 2
    if args.kind is not None and (args.dismiss is not None or args.rebuild):
        _report("error", "notes",
                "agentboard notes: --kind works with --dismiss-all or a listing only")
        return 2
    session = None
    if args.session:
        session = _explicit_session(args.session)
        if session is None:
            _report("error", "notes", f"agentboard notes: --session must be TOOL:ID, got {args.session!r}")
            return 2

    conn = db.connect()
    try:
        if args.rebuild:
            stats = notes_mod.rebuild(
                conn, notes_mod.load_config(db.home_dir()), days=args.days, reset=args.reset,
            )
            diag.info("cli", "notes_rebuild", days=args.days, reset=args.reset, **stats)
            if args.json:
                print(json.dumps(stats))
            else:
                print(
                    f"rebuilt notes: scanned={stats['scanned']} recorded={stats['recorded']} "
                    f"skipped_dismissed={stats['skipped_dismissed']} deleted={stats['deleted']}"
                )
            return 0

        if args.dismiss is not None or args.dismiss_all:
            now_iso = _now_iso()
            conn.execute("BEGIN IMMEDIATE")
            try:
                if args.dismiss is not None:
                    count = notes_mod.dismiss(conn, now_iso, ids=args.dismiss)
                else:
                    count = notes_mod.dismiss(conn, now_iso, session=session, kinds=args.kind)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            diag.info("cli", "notes_dismiss", ids=args.dismiss, all=args.dismiss_all,
                      session=args.session, kinds=args.kind, dismissed=count)
            if args.json:
                print(json.dumps({"dismissed": count}))
            else:
                print(f"dismissed {count} note(s)")
            return 0

        query = "SELECT * FROM notes"
        clauses, params = [], []
        if not args.all:
            clauses.append("dismissed_at IS NULL")
        if session is not None:
            clauses.append("tool=? AND session_id=?")
            params.extend(session)
        if args.kind is not None:
            clauses.append(f"kind IN ({','.join('?' for _ in args.kind)})")
            params.extend(args.kind)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY last_ts DESC, id DESC"
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()

    items = [_note_dict(row) for row in rows]
    if args.json:
        print(json.dumps({"notes": items}))
        return 0
    for item in items:
        times = f" x{item['count']}" if item["count"] > 1 else ""
        dismissed = " (dismissed)" if item["dismissed_at"] else ""
        print(
            f"{item['id']:>5} {item['kind']:<9} {item['key']:<44} {item['last_ts']}"
            f"{times}{dismissed}  {notes_mod.first_line(item['text'])[:120]}"
        )
    return 0


# -- install / uninstall hooks -------------------------------------------------

def _agentboard_hook_suffix(command: str) -> str:
    """The path- and quoting-independent tail of an agentboard hook command.

    Matching on this instead of the full command lets install-hooks
    recognize (and migrate) an entry registered from a different install
    location -- e.g. an old repo-checkout path, a shell-quoted path with
    spaces, or (the bug this guards against) an unquoted path that happens
    to contain a space -- as the same hook.

    Finding "bin/agentboard" directly in the raw string (rather than
    shlex.split-ing the whole command first) is what makes this safe even
    for an unquoted path containing a space: that space is always before
    the marker, inside the path, so it never lands in `rest` below.
    """
    marker = "bin/agentboard"
    idx = command.rfind(marker)
    if idx == -1:
        return command
    rest = command[idx + len(marker):]
    if rest[:1] in ("'", '"'):
        rest = rest[1:]
    try:
        args = shlex.split(rest)
    except ValueError:
        args = rest.split()
    return " ".join([marker] + args)


def _is_agentboard_hook_entry(entry, command) -> bool:
    if entry.get("type") != "command":
        return False
    entry_command = entry.get("command")
    if not isinstance(entry_command, str):
        return False
    return _agentboard_hook_suffix(entry_command) == _agentboard_hook_suffix(command)


def _has_agentboard_hook(groups, command):
    for group in groups:
        for entry in group.get("hooks", []):
            if _is_agentboard_hook_entry(entry, command):
                return True
    return False


def _merge_hooks(existing_hooks, events, command, timeout):
    """Add/update the agentboard hook for each event.

    An existing agentboard entry for this tool (any install path) is
    rewritten in place to the current command/timeout rather than
    duplicated -- this is how an old repo-checkout path gets migrated to
    the installed copy's path on a later `install-hooks` run.
    """
    hooks = dict(existing_hooks or {})
    changed = False
    for event in events:
        groups = list(hooks.get(event, []))
        new_groups = []
        found = False
        for group in groups:
            group_hooks = group.get("hooks", [])
            idx = next(
                (i for i, entry in enumerate(group_hooks) if _is_agentboard_hook_entry(entry, command)),
                None,
            )
            if idx is None:
                new_groups.append(group)
                continue
            found = True
            desired = {"type": "command", "command": command, "timeout": timeout}
            if group_hooks[idx] == desired:
                new_groups.append(group)
                continue
            new_group_hooks = list(group_hooks)
            new_group_hooks[idx] = desired
            new_groups.append({**group, "hooks": new_group_hooks})
            changed = True
        if not found:
            new_groups.append({"hooks": [{"type": "command", "command": command, "timeout": timeout}]})
            changed = True
        hooks[event] = new_groups
    return hooks, changed


def _strip_agentboard_hook(existing_hooks, command):
    """Remove only this agentboard entry from each hook group.

    Any other hook sharing a group (e.g. one a user added via Claude's
    /hooks UI) is kept. A group is dropped only once its own hooks list is
    empty, and an event key only once its groups list is empty -- this
    never leaves behind an empty "Event": [] placeholder.
    """
    hooks = dict(existing_hooks or {})
    changed = False
    result = {}
    for event, groups in hooks.items():
        new_groups = []
        for group in groups:
            group_hooks = group.get("hooks", [])
            kept = [h for h in group_hooks if not _is_agentboard_hook_entry(h, command)]
            if len(kept) != len(group_hooks):
                changed = True
            if kept:
                new_groups.append({**group, "hooks": kept})
        if new_groups:
            result[event] = new_groups
        elif groups:
            changed = True
    return result, changed


class MalformedConfigError(Exception):
    """A hooks config file could not be parsed as JSON."""

    def __init__(self, path: Path, original: Exception):
        self.path = path
        self.original = original
        super().__init__(f"{path} is not valid JSON ({original}); not modified")


def _load_json(path: Path):
    if not path.is_file():
        return {}
    text = path.read_text()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise MalformedConfigError(path, exc) from exc


def _unique_backup_path(path: Path) -> Path:
    """A `<path>.agentboard-bak-<...>` path guaranteed not to already
    exist, claimed atomically (O_CREAT|O_EXCL) so two backups made within
    the same microsecond -- or by two processes racing -- can never
    collide onto one filename. A UTC timestamp with microseconds makes a
    same-second collision rare in the first place; falling back to -1,
    -2, ... on top of that makes it impossible.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    suffix = 0
    while True:
        name = f"{path.name}.agentboard-bak-{ts}" + (f"-{suffix}" if suffix else "")
        candidate = path.with_name(name)
        try:
            fd = os.open(str(candidate), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            suffix += 1
            continue
        os.close(fd)
        return candidate


def _backup_file(path: Path) -> Path:
    """Copy `path` to a uniquely-named backup. Never overwrites an
    existing backup -- a backup is exactly the thing a later uninstall
    may need to restore from, and clobbering one silently would be a
    real data-loss bug (e.g. the one `original_backups` points at)."""
    backup_path = _unique_backup_path(path)
    shutil.copy2(path, backup_path)
    return backup_path


def _write_json_with_backup(path: Path, data, dry_run: bool, old_text: str):
    new_text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if dry_run:
        return new_text
    if path.is_file():
        backup_path = _backup_file(path)
        _record_original_backup(path, backup_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_text)
    return new_text


# -- install manifest -----------------------------------------------------------
#
# Tracks which config files/dirs install-hooks itself created (as opposed
# to ones that already existed with other content), and where the very
# first backup of a pre-existing file is, so uninstall-hooks can remove
# only what it added and restore the rest byte-for-byte.

_MANIFEST_LIST_KEYS = ("created_files", "created_dirs")
_MANIFEST_DICT_KEYS = ("original_backups",)


def _manifest_path() -> Path:
    return db.home_dir() / "install-manifest.json"


def _empty_manifest() -> dict:
    data = {key: [] for key in _MANIFEST_LIST_KEYS}
    data.update({key: {} for key in _MANIFEST_DICT_KEYS})
    return data


def _load_manifest() -> dict:
    path = _manifest_path()
    if not path.is_file():
        return _empty_manifest()
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return _empty_manifest()
    if not isinstance(data, dict):
        return _empty_manifest()
    for key in _MANIFEST_LIST_KEYS:
        if not isinstance(data.get(key), list):
            data[key] = []
    for key in _MANIFEST_DICT_KEYS:
        if not isinstance(data.get(key), dict):
            data[key] = {}
    return data


def _save_manifest(data: dict) -> None:
    path = _manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _record_created_file(path: Path) -> None:
    data = _load_manifest()
    key = str(path)
    if key not in data["created_files"]:
        data["created_files"].append(key)
        _save_manifest(data)


def _forget_created_file(path: Path) -> None:
    data = _load_manifest()
    key = str(path)
    if key in data["created_files"]:
        data["created_files"].remove(key)
        _save_manifest(data)


def _file_was_created_by_install(path: Path) -> bool:
    return str(path) in _load_manifest().get("created_files", [])


def _record_created_dir(path: Path) -> None:
    data = _load_manifest()
    key = str(path)
    if key not in data["created_dirs"]:
        data["created_dirs"].append(key)
        _save_manifest(data)


def _forget_created_dir(path: Path) -> None:
    data = _load_manifest()
    key = str(path)
    if key in data["created_dirs"]:
        data["created_dirs"].remove(key)
        _save_manifest(data)


def _dir_was_created_by_install(path: Path) -> bool:
    return str(path) in _load_manifest().get("created_dirs", [])


def _maybe_remove_created_dir(path: Path) -> None:
    """Remove a ~/.claude or ~/.codex dir install-hooks created, but only
    if it's still empty -- any other content (session files, other config
    the real tool wrote) means it predates-in-spirit the empty dir and
    must be left alone."""
    if not _dir_was_created_by_install(path):
        return
    try:
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
            _forget_created_dir(path)
    except OSError:
        pass


def _record_original_backup(path: Path, backup_path: Path) -> None:
    """Remember the *first* backup made of a pre-existing file, so a later
    uninstall can restore those exact original bytes instead of
    re-serializing. Never overwritten by a later backup."""
    data = _load_manifest()
    key = str(path)
    if key not in data["original_backups"]:
        data["original_backups"][key] = str(backup_path)
        _save_manifest(data)


def _forget_original_backup(path: Path) -> None:
    data = _load_manifest()
    key = str(path)
    if key in data["original_backups"]:
        del data["original_backups"][key]
        _save_manifest(data)


def _original_backup_path(path: Path):
    value = _load_manifest()["original_backups"].get(str(path))
    return Path(value) if value else None


def _original_had_hooks_key(path: Path) -> bool:
    """Whether the file's pre-install backup (install's first backup of
    it, if there is one) had a "hooks" key at all, even an empty one.
    Needed so stripping agentboard's own entries back out reproduces the
    exact pre-install shape: a file that started as {"hooks": {}} must
    stay {"hooks": {}}, not collapse to {} -- otherwise the byte-for-byte
    restore below never matches and a re-serialized copy gets written
    instead. False (not an error) for no backup, or one that's missing or
    unparseable -- the uninstall still proceeds, just without the
    byte-for-byte shortcut.
    """
    backup_path = _original_backup_path(path)
    if backup_path is None or not backup_path.is_file():
        return False
    try:
        data = json.loads(backup_path.read_bytes())
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and "hooks" in data


def _record_codex_config_action(path: Path, action: str) -> None:
    data = _load_manifest()
    data["codex_config_toml"] = {"path": str(path), "action": action}
    _save_manifest(data)


def _forget_codex_config_action() -> None:
    data = _load_manifest()
    if "codex_config_toml" in data:
        del data["codex_config_toml"]
        _save_manifest(data)


def _codex_config_action():
    return _load_manifest().get("codex_config_toml")


def _install_hooks_file(path: Path, old: dict, old_text: str, new_hooks, changed: bool, dry_run: bool):
    new = dict(old)
    new["hooks"] = new_hooks
    if not changed:
        return old_text, old_text, changed
    created = old_text == ""
    created_dir = created and not path.parent.is_dir()
    new_text = _write_json_with_backup(path, new, dry_run, old_text)
    if created and not dry_run:
        _record_created_file(path)
        if created_dir:
            _record_created_dir(path.parent)
    return old_text, new_text, changed


def install_claude_hooks(settings_path: Path, command: str, dry_run: bool):
    old = _load_json(settings_path)
    old_text = json.dumps(old, indent=2, ensure_ascii=False) + "\n" if settings_path.is_file() else ""
    new_hooks, changed = _merge_hooks(old.get("hooks", {}), CLAUDE_EVENTS, command, 5)
    return _install_hooks_file(settings_path, old, old_text, new_hooks, changed, dry_run)


def install_codex_hooks(hooks_path: Path, command: str, dry_run: bool):
    old = _load_json(hooks_path)
    old_text = json.dumps(old, indent=2, ensure_ascii=False) + "\n" if hooks_path.is_file() else ""
    new_hooks, changed = _merge_hooks(old.get("hooks", {}), CODEX_EVENTS, command, 5)
    return _install_hooks_file(hooks_path, old, old_text, new_hooks, changed, dry_run)


def _restore_original_bytes(path: Path, new: dict, dry_run: bool):
    """If install's first backup of this file, parsed, equals the result
    of stripping our hooks out, return (text, True) to signal that the
    exact original bytes should be written back verbatim instead of
    re-serializing (which would re-flow formatting, escape non-ASCII
    characters, add a trailing newline, etc. for no reason). Returns
    (None, False) when there's no usable backup or it doesn't match.
    """
    backup_path = _original_backup_path(path)
    if backup_path is None or not backup_path.is_file():
        return None, False
    try:
        original_bytes = backup_path.read_bytes()
        original_parsed = json.loads(original_bytes)
    except (OSError, json.JSONDecodeError):
        return None, False
    if original_parsed != new:
        return None, False
    try:
        original_text = original_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return None, False
    if not dry_run:
        _backup_file(path)
        path.write_bytes(original_bytes)
        _forget_original_backup(path)
    return original_text, True


def _uninstall_hooks(path: Path, command: str, dry_run: bool):
    """Shared uninstall path for both tools' config files.

    Restores the file to its pre-install state exactly when install-hooks
    is the one that created it and the result of stripping agentboard's
    own entries is now empty -- deleting it rather than writing back a
    bare "{}" that never existed before. When the file pre-existed and
    the stripped result matches install's first backup of it, the exact
    original bytes are written back instead of a re-serialized copy.
    """
    old = _load_json(path)
    old_text = json.dumps(old, indent=2, ensure_ascii=False) + "\n" if path.is_file() else ""
    new = dict(old)
    new_hooks, changed = _strip_agentboard_hook(old.get("hooks", {}), command)
    if new_hooks or _original_had_hooks_key(path):
        new["hooks"] = new_hooks
    else:
        new.pop("hooks", None)
    if not changed:
        return old_text, old_text, changed

    if not new and path.is_file() and _file_was_created_by_install(path):
        new_text = ""
        if not dry_run:
            path.unlink()
            _forget_created_file(path)
            _maybe_remove_created_dir(path.parent)
        return old_text, new_text, changed

    restored_text, restored = _restore_original_bytes(path, new, dry_run)
    if restored:
        return old_text, restored_text, changed

    new_text = _write_json_with_backup(path, new, dry_run, old_text)
    return old_text, new_text, changed


def uninstall_claude_hooks(settings_path: Path, command: str, dry_run: bool):
    return _uninstall_hooks(settings_path, command, dry_run)


def uninstall_codex_hooks(hooks_path: Path, command: str, dry_run: bool):
    return _uninstall_hooks(hooks_path, command, dry_run)


def _diff(old_text, new_text, label):
    import difflib

    lines = list(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"{label} (before)",
            tofile=f"{label} (after)",
        )
    )
    return "".join(lines)


# -- Codex [features] hooks flag -------------------------------------------------
#
# Codex silently ignores hooks.json unless `[features]\nhooks = true` is set
# in config.toml. install-hooks never edits that file by default (that's
# user config, same as settings.json/hooks.json themselves aren't touched
# beyond the hooks key) -- it only reads it to warn when the flag is
# missing. `enable-codex-hooks` below is the opt-in that actually sets it.

def _codex_config_path() -> Path:
    return Path.home() / ".codex" / "config.toml"


def _read_text_no_newline_translation(path: Path) -> str:
    """Like path.read_text(), but without universal-newline translation,
    so a CRLF file's \\r\\n survives into `text` -- Path.read_text() opens
    in text mode with newline=None, which collapses every \\r\\n (and
    bare \\r) to \\n, so a patcher that branches on "does this file use
    CRLF" never sees a CRLF to begin with."""
    with path.open("r", encoding="utf-8", newline="") as fh:
        return fh.read()


def _write_text_no_newline_translation(path: Path, text: str) -> None:
    """The write-side counterpart of _read_text_no_newline_translation:
    writes `text` verbatim, including any embedded \\r\\n, instead of
    letting text-mode writing reinterpret newlines."""
    with path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _codex_hooks_feature_enabled(config_path: Path):
    """True/False if config.toml says so, None if that can't be determined
    (missing file, no tomllib, the file itself is unparseable TOML, or it
    isn't valid UTF-8 -- tomllib.load() raises UnicodeDecodeError, not
    TOMLDecodeError, for that last one)."""
    if not config_path.is_file():
        return None
    try:
        import tomllib
    except ImportError:
        return None
    try:
        with config_path.open("rb") as fh:
            data = tomllib.load(fh)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, OSError):
        return None
    features = data.get("features")
    return isinstance(features, dict) and features.get("hooks") is True


def _any_agentboard_hook(hooks, command) -> bool:
    return any(_has_agentboard_hook(groups, command) for groups in (hooks or {}).values())


def _print_codex_hooks_reminder(hooks_path: Path, command: str) -> None:
    """Printed on every run where this tool's Codex hook entry is present,
    not just when it was just added -- a user who missed /hooks the first
    time should still see the reminder on a later re-run."""
    try:
        data = _load_json(hooks_path)
    except MalformedConfigError:
        return
    if not _any_agentboard_hook(data.get("hooks", {}), command):
        return
    print("Run /hooks in Codex to trust the new hooks.")
    config_path = _codex_config_path()
    enabled = _codex_hooks_feature_enabled(config_path)
    if enabled is not True:
        print(
            f"warning: Codex will ignore {hooks_path} until "
            f"hooks = true is set under [features] in {config_path}"
        )
        try:
            config_text = (
                _read_text_no_newline_translation(config_path) if config_path.is_file() else ""
            )
        except (OSError, UnicodeDecodeError):
            print(f"    could not read {config_path} as UTF-8; skipping feature check")
        else:
            print("    or run install.sh --enable-codex-hooks, which does this safely:")
            for line in _codex_features_manual_hint_lines(config_text):
                print(f"        {line}")


class CodexTomlPatchError(Exception):
    """config.toml's `features` can't be safely patched in place."""


# A bare or quoted TOML key, as used in a dotted key or an inline table.
_TOML_KEY_ALT = r'[A-Za-z0-9_-]+|"[^"\\]*"|\'[^\']*\''

_TOML_ARRAY_SECTION_RE = re.compile(r"^\s*\[\[(?P<name>[^\[\]]+)\]\]\s*(#.*)?$")
_TOML_TABLE_SECTION_RE = re.compile(r"^\s*\[(?P<name>[^\[\]]+)\]\s*(#.*)?$")
_TOML_HOOKS_KEY_RE = re.compile(
    r"^(?P<prefix>\s*(?:hooks|\"hooks\"|'hooks')\s*=\s*)(?P<value>\S+)(?P<suffix>.*)$"
)
_TOML_DOTTED_FEATURES_HOOKS_RE = re.compile(
    r"^(?P<prefix>\s*(?:features|\"features\"|'features')\s*\.\s*"
    r"(?:hooks|\"hooks\"|'hooks')\s*=\s*)(?P<value>\S+)(?P<suffix>.*)$"
)
_TOML_DOTTED_FEATURES_RE = re.compile(r"^\s*(?:features|\"features\"|'features')\s*\.")
_TOML_INLINE_FEATURES_RE = re.compile(
    r"^(?P<indent>\s*)(?:features|\"features\"|'features')\s*=\s*"
    r"\{(?P<body>[^{}]*)\}\s*(?P<comment>#.*)?$"
)
_TOML_INLINE_PIECE_RE = re.compile(rf"^(?P<key>{_TOML_KEY_ALT})\s*=\s*(?P<value>.+)$")


def _toml_bare_name(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        return raw[1:-1]
    return raw


def _iter_toml_section_headers(lines):
    """Yield (line_index, kind, bare_name) for every top-level [table] or
    [[array-of-tables]] header line. kind is "table" or "array"."""
    for i, line in enumerate(lines):
        if line.strip().startswith("#"):
            continue
        match = _TOML_ARRAY_SECTION_RE.match(line)
        if match:
            yield i, "array", _toml_bare_name(match.group("name").strip())
            continue
        match = _TOML_TABLE_SECTION_RE.match(line)
        if match:
            yield i, "table", _toml_bare_name(match.group("name").strip())


def _find_features_table_section(lines):
    """(start, end) line-index range of a bare `[features]` header's body
    (end is exclusive, the next header or EOF), or None if there isn't
    one. A `[[...]]` array-of-tables header always ends the range, even
    though it's never itself treated as the `[features]` table."""
    headers = list(_iter_toml_section_headers(lines))
    for i, (line_no, kind, name) in enumerate(headers):
        if kind == "table" and name == "features":
            end = headers[i + 1][0] if i + 1 < len(headers) else len(lines)
            return line_no, end
    return None


def _patch_features_section(lines, bounds):
    start, end = bounds
    out = list(lines[: start + 1])
    hooks_set = False
    for line in lines[start + 1 : end]:
        stripped = line.strip()
        key_match = None if stripped.startswith("#") else _TOML_HOOKS_KEY_RE.match(line)
        if key_match:
            hooks_set = True
            if key_match.group("value") != "true":
                out.append(f"{key_match.group('prefix')}true{key_match.group('suffix')}")
                continue
        out.append(line)
    if not hooks_set:
        out.append("hooks = true")
    out.extend(lines[end:])
    return out


def _toplevel_scope_limit(lines):
    """Line count before the first [table]/[[array]] header -- the region
    where a dotted or inline `features` assignment belongs to the root
    table, not some other section."""
    headers = list(_iter_toml_section_headers(lines))
    return headers[0][0] if headers else len(lines)


def _find_top_level_dotted_features(lines):
    """("exact", idx) for a `features.hooks = ...` line, or ("after", idx)
    for the last `features.<anything> = ...` line to insert after, or
    None if there's no top-level dotted `features.` key at all."""
    limit = _toplevel_scope_limit(lines)
    exact_idx = None
    last_idx = None
    for i in range(limit):
        line = lines[i]
        if line.strip().startswith("#"):
            continue
        if _TOML_DOTTED_FEATURES_HOOKS_RE.match(line):
            exact_idx = i
            last_idx = i
        elif _TOML_DOTTED_FEATURES_RE.match(line):
            last_idx = i
    if exact_idx is not None:
        return "exact", exact_idx
    if last_idx is not None:
        return "after", last_idx
    return None


def _patch_dotted_features(lines, found):
    kind, idx = found
    out = list(lines)
    if kind == "exact":
        key_match = _TOML_DOTTED_FEATURES_HOOKS_RE.match(out[idx])
        if key_match.group("value") != "true":
            out[idx] = f"{key_match.group('prefix')}true{key_match.group('suffix')}"
        return out
    out.insert(idx + 1, "features.hooks = true")
    return out


def _find_top_level_inline_features(lines):
    limit = _toplevel_scope_limit(lines)
    for i in range(limit):
        line = lines[i]
        if line.strip().startswith("#"):
            continue
        if _TOML_INLINE_FEATURES_RE.match(line):
            return i
    return None


def _patch_inline_features(lines, idx):
    match = _TOML_INLINE_FEATURES_RE.match(lines[idx])
    indent = match.group("indent")
    comment = match.group("comment")
    body = match.group("body").strip()
    raw_pieces = [p.strip() for p in body.split(",") if p.strip() != ""]

    pieces = []
    found_hooks = False
    for piece in raw_pieces:
        piece_match = _TOML_INLINE_PIECE_RE.match(piece)
        if piece_match is None:
            raise CodexTomlPatchError(
                "features is an inline table with a value AgentBoard can't safely "
                "rewrite (it contains '{', '[', a quote, or a comma)"
            )
        if _toml_bare_name(piece_match.group("key")) == "hooks":
            piece = f"{piece_match.group('key')} = true"
            found_hooks = True
        pieces.append(piece)
    if not found_hooks:
        pieces.append("hooks = true")

    new_line = f"{indent}features = {{ {', '.join(pieces)} }}"
    if comment:
        new_line += f"  {comment}"
    out = list(lines)
    out[idx] = new_line
    return out


def _append_features_section(lines):
    out = list(lines)
    if out and out[-1] != "":
        out.append("")
    out.append("[features]")
    out.append("hooks = true")
    return out


def _patch_toml_features_hooks(text: str, old_data: dict) -> str:
    features_val = old_data.get("features")
    if features_val is not None and not isinstance(features_val, dict):
        raise CodexTomlPatchError(
            "features is already defined as something other than a table "
            f"({type(features_val).__name__})"
        )

    had_trailing_newline = text.endswith("\n")
    lines = text.split("\n") if text else []
    if had_trailing_newline:
        lines = lines[:-1]

    section_bounds = _find_features_table_section(lines)
    if section_bounds is not None:
        new_lines = _patch_features_section(lines, section_bounds)
    else:
        dotted_found = _find_top_level_dotted_features(lines)
        if dotted_found is not None:
            new_lines = _patch_dotted_features(lines, dotted_found)
        else:
            inline_idx = _find_top_level_inline_features(lines)
            if inline_idx is not None:
                new_lines = _patch_inline_features(lines, inline_idx)
            elif features_val is None:
                new_lines = _append_features_section(lines)
            else:
                raise CodexTomlPatchError(
                    "features is defined in a form AgentBoard doesn't recognize "
                    "well enough to edit safely"
                )

    new_text = "\n".join(new_lines)
    if had_trailing_newline or len(new_lines) != len(lines):
        new_text += "\n"
    return new_text


def _codex_features_manual_hint_lines(text: str) -> list:
    """Advice for manually setting features.hooks = true in `text` (a
    Codex config.toml's current content).

    This is deliberately *not* its own independent detector. It runs the
    real patcher (_patch_codex_hooks_feature) and shows the lines that
    patcher's own tomllib round-trip verification already proved are
    safe -- the only forms this can ever produce a concrete snippet for
    are the ones the patcher itself can positively verify: features
    absent, a [features] table where hooks is absent or a bool, a dotted
    features.hooks bool, or a simple inline table. An independent regex
    scan of the raw text (the previous approach here) can be fooled by
    text that merely *looks* like "[features]" or "features.x = ..."
    inside an unrelated string value; delegating to the same
    tomllib-verified result the real edit would produce can't be.

    Returns [] if hooks is already true (nothing to suggest), or a
    single plain sentence with no snippet if the patcher can't safely
    determine where this goes at all.
    """
    try:
        new_text, changed = _patch_codex_hooks_feature(text)
    except CodexTomlPatchError:
        return [
            "Couldn't safely determine where to add it; set `hooks = true` "
            f"under `[features]` in {_codex_config_path()} by hand."
        ]
    if not changed:
        return []

    import difflib

    added_or_changed = [
        line[1:]
        for line in difflib.unified_diff(text.splitlines(), new_text.splitlines(), n=0, lineterm="")
        if line.startswith("+") and not line.startswith("+++")
    ]
    if not added_or_changed:
        return []
    return ["add this (or change the existing line to match):"] + [
        f"    {line}" for line in added_or_changed
    ]


def _patch_codex_hooks_feature(text: str):
    """Set features.hooks = true in a Codex config.toml, in place.

    Parses with tomllib first to see what `features` already is: a
    `[features]` table, a dotted key (`features.hooks = ...` /
    `features.x = ...`), or a one-line inline table (`features = {...}`).
    Edits that exact form -- never emits a second `[features]`. Raises
    CodexTomlPatchError, without changing `text` at all, when the
    existing form can't be edited safely, or when the result doesn't
    verify (re-parses to features.hooks = true with everything else
    unchanged). Returns (new_text, changed).
    """
    try:
        import tomllib
    except ImportError as exc:
        raise CodexTomlPatchError("tomllib is unavailable (Python >= 3.11 required)") from exc

    uses_crlf = "\r\n" in text and "\n" not in text.replace("\r\n", "")
    normalized = text.replace("\r\n", "\n") if uses_crlf else text

    try:
        old_data = tomllib.loads(normalized)
    except tomllib.TOMLDecodeError as exc:
        raise CodexTomlPatchError(f"existing file is not valid TOML ({exc})") from exc

    features_val = old_data.get("features")
    if isinstance(features_val, dict) and features_val.get("hooks") is True:
        return text, False

    new_normalized = _patch_toml_features_hooks(normalized, old_data)

    try:
        new_data = tomllib.loads(new_normalized)
    except tomllib.TOMLDecodeError as exc:
        raise CodexTomlPatchError(f"patch produced invalid TOML ({exc})") from exc

    new_features = new_data.get("features")
    if not isinstance(new_features, dict) or new_features.get("hooks") is not True:
        raise CodexTomlPatchError("patch did not result in features.hooks = true")

    expected = dict(old_data)
    expected_features = dict(expected.get("features") or {})
    expected_features["hooks"] = True
    expected["features"] = expected_features
    if new_data != expected:
        raise CodexTomlPatchError("patch changed values other than features.hooks")

    new_text = new_normalized.replace("\n", "\r\n") if uses_crlf else new_normalized
    return new_text, new_text != text


def _print_codex_manual_hint(old_text: str, file=None) -> None:
    # file=None, resolved to sys.stderr here rather than as the default
    # argument value, so this still honors a caller's
    # contextlib.redirect_stderr (a default is bound once, at def time).
    file = sys.stderr if file is None else file
    print("manual fix:", file=file)
    for line in _codex_features_manual_hint_lines(old_text):
        print(f"    {line}", file=file)


def cmd_enable_codex_hooks(args):
    config_path = _codex_config_path()
    # is_file() captured before any write below: a 0-byte config.toml
    # that already existed is "patched" (an empty file is still a file
    # the user created/owns), not "created" -- old_text == "" alone can't
    # tell the two apart.
    existed_before = config_path.is_file()
    try:
        old_text = _read_text_no_newline_translation(config_path) if existed_before else ""
    except (OSError, UnicodeDecodeError):
        _report(
            "error", "enable_codex_hooks",
            f"error: could not read {config_path} as UTF-8; skipping feature check",
        )
        return 1
    try:
        new_text, changed = _patch_codex_hooks_feature(old_text)
    except CodexTomlPatchError as exc:
        _report("error", "enable_codex_hooks", f"error: {config_path}: {exc}")
        _print_codex_manual_hint(old_text)
        return 1
    if not changed:
        diag.info("cli", "enable_codex_hooks", file=config_path, outcome="already_set")
        print(f"{config_path}: hooks = true already set under [features]")
        return 0
    if args.dry_run:
        diag.info("cli", "enable_codex_hooks", file=config_path, outcome="dry_run")
        print(_diff(old_text, new_text, str(config_path)))
        return 0
    if existed_before:
        _backup_file(config_path)
    created_dir = not config_path.parent.is_dir()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    _write_text_no_newline_translation(config_path, new_text)
    _record_codex_config_action(config_path, "created" if not existed_before else "patched")
    if created_dir:
        _record_created_dir(config_path.parent)
    diag.info(
        "cli", "enable_codex_hooks", file=config_path,
        outcome="patched" if existed_before else "created",
    )
    print(f"Set hooks = true under [features] in {config_path}")
    return 0


def cmd_install_hooks(args):
    do_claude = args.claude or (not args.claude and not args.codex)
    do_codex = args.codex or (not args.claude and not args.codex)
    # Quoted so a hook command still runs correctly from an install path
    # containing spaces (both tools run hook commands through a shell).
    # --bin-path overrides this with the path the command would be
    # installed at, for install.sh --dry-run previews run via the repo
    # checkout's own copy (whose __file__-derived path would otherwise be
    # the checkout, not the installed location).
    bin_path = shlex.quote(args.bin_path) if getattr(args, "bin_path", None) else shlex.quote(str(_bin_path()))
    failed_paths = []

    if do_claude:
        settings_path = Path.home() / ".claude" / "settings.json"
        command = f"{bin_path} hook --tool claude"
        try:
            old_text, new_text, changed = install_claude_hooks(settings_path, command, args.dry_run)
        except MalformedConfigError as exc:
            _report("error", "install_hooks", f"error: {exc}", file=exc.path)
            failed_paths.append(str(settings_path))
        else:
            diag.info(
                "cli", "install_hooks", tool="claude", file=settings_path,
                outcome="dry_run" if args.dry_run else ("changed" if changed else "unchanged"),
                would_change=changed if args.dry_run else None,
            )
            if args.dry_run:
                diff = _diff(old_text, new_text, str(settings_path))
                print(diff if diff else f"{settings_path}: no changes (already installed)")
            elif changed:
                print(f"Installed Claude hooks in {settings_path}")
            else:
                print(f"Claude hooks already installed in {settings_path}")

    if do_codex:
        hooks_path = Path.home() / ".codex" / "hooks.json"
        command = f"{bin_path} hook --tool codex"
        try:
            old_text, new_text, changed = install_codex_hooks(hooks_path, command, args.dry_run)
        except MalformedConfigError as exc:
            _report("error", "install_hooks", f"error: {exc}", file=exc.path)
            failed_paths.append(str(hooks_path))
        else:
            diag.info(
                "cli", "install_hooks", tool="codex", file=hooks_path,
                outcome="dry_run" if args.dry_run else ("changed" if changed else "unchanged"),
                would_change=changed if args.dry_run else None,
            )
            if args.dry_run:
                diff = _diff(old_text, new_text, str(hooks_path))
                print(diff if diff else f"{hooks_path}: no changes (already installed)")
            elif changed:
                print(f"Installed Codex hooks in {hooks_path}")
            else:
                print(f"Codex hooks already installed in {hooks_path}")
            _print_codex_hooks_reminder(hooks_path, command)

    if failed_paths:
        _report(
            "error", "install_hooks",
            f"error: {len(failed_paths)} file(s) skipped due to invalid JSON: "
            + ", ".join(failed_paths),
        )
        return 1
    return 0


def _uninstall_codex_config_feature(dry_run: bool) -> None:
    """Report on (and, for a file install created, possibly remove) the
    config.toml edit --enable-codex-hooks made, per the manifest entry
    cmd_enable_codex_hooks recorded. Never auto-reverts a user-modified
    file -- a `patched` file is left with hooks = true still enabled,
    and a `created` file is only deleted if it still parses to exactly
    {"features": {"hooks": True}}.
    """
    action_info = _codex_config_action()
    if not action_info:
        return
    config_path_str = action_info.get("path")
    action = action_info.get("action")
    if not config_path_str or action not in ("created", "patched"):
        return
    config_path = Path(config_path_str)

    if action == "patched":
        print(
            f"note: hooks = true was left enabled under [features] in {config_path} "
            "(set by --enable-codex-hooks; not reverted)"
        )
        if not dry_run:
            _forget_codex_config_action()
        return

    if not config_path.is_file():
        if not dry_run:
            _forget_codex_config_action()
        return

    try:
        import tomllib

        with config_path.open("rb") as fh:
            data = tomllib.load(fh)
    except Exception:
        data = None

    if data == {"features": {"hooks": True}}:
        if dry_run:
            print(f"would remove: {config_path} (created by --enable-codex-hooks)")
        else:
            config_path.unlink()
            _forget_codex_config_action()
            print(f"Removed {config_path} (created by --enable-codex-hooks)")
    else:
        print(f"note: leaving {config_path} in place (modified since --enable-codex-hooks created it)")
        if not dry_run:
            _forget_codex_config_action()


def cmd_uninstall_hooks(args):
    do_claude = args.claude or (not args.claude and not args.codex)
    do_codex = args.codex or (not args.claude and not args.codex)
    bin_path = shlex.quote(str(_bin_path()))
    dry_run = args.dry_run
    failed_paths = []

    if do_claude:
        settings_path = Path.home() / ".claude" / "settings.json"
        command = f"{bin_path} hook --tool claude"
        try:
            old_text, new_text, changed = uninstall_claude_hooks(settings_path, command, dry_run)
        except MalformedConfigError as exc:
            _report("error", "uninstall_hooks", f"error: {exc}", file=exc.path)
            failed_paths.append(str(settings_path))
        else:
            diag.info(
                "cli", "uninstall_hooks", tool="claude", file=settings_path,
                outcome="dry_run" if dry_run else ("changed" if changed else "unchanged"),
                would_change=changed if dry_run else None,
                removed_file=(not dry_run and changed and not settings_path.exists()) or None,
            )
            if dry_run:
                diff = _diff(old_text, new_text, str(settings_path))
                print(diff if diff else f"{settings_path}: no changes (no agentboard hooks installed)")
            else:
                print(f"Removed Claude hooks from {settings_path}" if changed else "No Claude hooks to remove")

    if do_codex:
        hooks_path = Path.home() / ".codex" / "hooks.json"
        command = f"{bin_path} hook --tool codex"
        try:
            old_text, new_text, changed = uninstall_codex_hooks(hooks_path, command, dry_run)
        except MalformedConfigError as exc:
            _report("error", "uninstall_hooks", f"error: {exc}", file=exc.path)
            failed_paths.append(str(hooks_path))
        else:
            diag.info(
                "cli", "uninstall_hooks", tool="codex", file=hooks_path,
                outcome="dry_run" if dry_run else ("changed" if changed else "unchanged"),
                would_change=changed if dry_run else None,
                removed_file=(not dry_run and changed and not hooks_path.exists()) or None,
            )
            if dry_run:
                diff = _diff(old_text, new_text, str(hooks_path))
                print(diff if diff else f"{hooks_path}: no changes (no agentboard hooks installed)")
            else:
                print(f"Removed Codex hooks from {hooks_path}" if changed else "No Codex hooks to remove")
        _uninstall_codex_config_feature(dry_run)
        # Re-check after config.toml is possibly gone too: hooks.json's own
        # uninstall (above) only tried this right after removing hooks.json
        # itself, when config.toml (if --enable-codex-hooks created ~/.codex
        # instead) could still be sitting in the dir.
        if not dry_run:
            _maybe_remove_created_dir(hooks_path.parent)

    if failed_paths:
        _report(
            "error", "uninstall_hooks",
            f"error: {len(failed_paths)} file(s) skipped due to invalid JSON: "
            + ", ".join(failed_paths),
        )
        return 1
    return 0


# -- hook -----------------------------------------------------------------------

def cmd_hook(args):
    hook_mod.run(args.tool)
    return 0


# -- map-codex ------------------------------------------------------------------

def cmd_map_codex(args):
    conn = db.connect()
    try:
        mapped = codexmap.map_codex_session(conn, args.session, args.cwd, at=args.at)
    finally:
        conn.close()
    diag.debug("cli", "map_codex", session=args.session, mapped=bool(mapped))
    print("mapped" if mapped else "unmapped")
    return 0


# -- focus / resume -------------------------------------------------------------

_ITERM_FOCUS_APPLESCRIPT = """
on run argv
  set targetId to item 1 of argv
  set targetTty to item 2 of argv
  tell application "iTerm2"
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          if targetId is not "" and (unique id of s) is equal to targetId then
            tell t to select
            tell s to select
            tell w to select
            activate
            delay 0.15
            tell t to select
            tell s to select
            return "found:uuid"
          end if
        end repeat
      end repeat
    end repeat
    if targetTty is not "" then
      repeat with w in windows
        repeat with t in tabs of w
          repeat with s in sessions of t
            try
              if (tty of s) is equal to targetTty then
                tell t to select
                tell s to select
                tell w to select
                activate
                delay 0.15
                tell t to select
                tell s to select
                return "found:tty"
              end if
            end try
          end repeat
        end repeat
      end repeat
    end if
  end tell
  return "not_found"
end run
"""

_ITERM_RESUME_APPLESCRIPT = """
on run argv
  set cmd to item 1 of argv
  tell application "iTerm2"
    activate
    if (count of windows) = 0 then
      set w to (create window with default profile)
    else
      set w to current window
      tell w to create tab with default profile
    end if
    tell current session of w
      write text cmd
    end tell
  end tell
end run
"""

_TERMINAL_FOCUS_APPLESCRIPT = """
on run argv
  set targetTty to item 1 of argv
  tell application "Terminal"
    if targetTty is not "" then
      repeat with w in windows
        repeat with t in tabs of w
          try
            if (tty of t) is equal to targetTty then
              set selected of t to true
              set frontmost of w to true
              activate
              set selected of t to true
              return "found"
            end if
          end try
        end repeat
      end repeat
    end if
    activate
    return "activated"
  end tell
end run
"""

_TERMINAL_RESUME_APPLESCRIPT = """
on run argv
  set cmd to item 1 of argv
  tell application "Terminal"
    activate
    do script cmd
  end tell
end run
"""


def _iterm_unique_id(iterm_session_id):
    if not iterm_session_id:
        return None
    if ":" in iterm_session_id:
        return iterm_session_id.split(":", 1)[1]
    return iterm_session_id


def _tty_path(tty):
    if not tty:
        return None
    return tty if tty.startswith("/dev/") else f"/dev/{tty}"


def _returncode(result):
    """A subprocess result's exit code, or None if it doesn't have a real one."""
    code = getattr(result, "returncode", None)
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def _stdout_text(result):
    out = getattr(result, "stdout", None)
    if isinstance(out, bytes):
        out = out.decode("utf-8", "replace")
    return out.strip() if isinstance(out, str) else ""


def _stderr_text(result):
    err = getattr(result, "stderr", None)
    if isinstance(err, bytes):
        err = err.decode("utf-8", "replace")
    return err.strip() if isinstance(err, str) else ""


# A focus/resume AppleScript can block indefinitely (a first-run Automation
# permission prompt, a hung terminal). Bounding it means the click still
# ends in a logged outcome instead of a process that never reports back.
OSASCRIPT_TIMEOUT_SECS = 10


class _TimedOut:
    """Stands in for the result of a command killed by its timeout."""

    returncode = None
    stdout = ""

    def __init__(self, seconds):
        self.stderr = f"timed out after {seconds}s"


def _run_bounded(run, argv):
    """`run(argv)` capturing text output, bounded by OSASCRIPT_TIMEOUT_SECS.
    A timeout comes back as a `_TimedOut` result instead of raising."""
    try:
        return run(argv, capture_output=True, text=True, timeout=OSASCRIPT_TIMEOUT_SECS)
    except subprocess.TimeoutExpired:
        return _TimedOut(OSASCRIPT_TIMEOUT_SECS)


def _activate_app(term_program, run_osascript):
    """Last resort: bring a mapped terminal app to front, no tab match.

    Returns the `open -a` result, or None if `term_program` has no app.
    """
    app_name = TERM_PROGRAM_APP_NAMES.get(term_program)
    if not app_name:
        return None
    return _run_bounded(run_osascript, ["open", "-a", app_name])


# What the focus AppleScripts print, mapped to the logged `outcome`. A bare
# "found" is what the iTerm script returned before it said which strategy
# matched; an older script can't misreport which one ran.
_ITERM_FOCUS_RESULTS = {
    "found:uuid": "matched_uuid",
    "found:tty": "matched_tty",
    "found": "matched_unknown",
}
_TERMINAL_FOCUS_RESULTS = {
    "found": "matched_tty",
    "activated": "activated_only",
}


class _FocusTrace:
    """Collects one focus attempt into a single `cli.focus` info line: the
    recorded identity, each strategy tried, the AppleScript's exit code and
    output, and the final outcome. Each step is also logged at debug."""

    def __init__(self, key):
        self.key = key
        self.started = time.monotonic()
        self.fields = {"key": key}
        self.strategies = []

    def identity(self, row):
        self.fields.update(
            term=row["term_program"] if row else None,
            tty=row["tty"] if row else None,
            iterm_id=row["iterm_session_id"] if row else None,
        )

    def tried(self, strategy, result, **extra):
        self.strategies.append(strategy)
        rc = _returncode(result)
        out = _stdout_text(result)
        err = _stderr_text(result)
        diag.debug(
            "cli", "focus.try", key=self.key, strategy=strategy, rc=rc,
            stdout=diag.tail_text(out), stderr=diag.tail_text(err), **extra,
        )
        if strategy != "open_a":
            self.fields.update(
                osa_rc=rc, osa_out=diag.tail_text(out, 200), osa_err=diag.tail_text(err, 300),
            )
        else:
            self.fields["open_rc"] = rc
        return rc, out

    def finish(self, outcome, level="info"):
        diag.log(
            level, "cli", "focus", **self.fields,
            strategy=",".join(self.strategies) or None, outcome=outcome,
            ms=diag.ms_since(self.started),
        )


def _open_a_outcome(result):
    if isinstance(result, _TimedOut):
        return "timeout"
    rc = _returncode(result)
    return "activate_failed" if rc not in (None, 0) else "activated_only"


def _focus_timed_out(trace, key):
    # The summary line (outcome=timeout, osa_err) is the log record; the
    # stderr text is for the app.
    trace.finish("timeout", level="warn")
    print(f"focusing the terminal for {key} timed out after {OSASCRIPT_TIMEOUT_SECS}s",
          file=sys.stderr)
    return 1


def cmd_focus(args, run_osascript=subprocess.run):
    key = f"{args.tool}:{args.session_id}"
    trace = _FocusTrace(key)
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT iterm_session_id, term_program, tty FROM sessions "
            "WHERE tool=? AND session_id=?",
            (args.tool, args.session_id),
        ).fetchone()
    finally:
        conn.close()
    trace.identity(row)
    if not row or row["term_program"] not in TERM_PROGRAM_APP_NAMES:
        outcome = "unsupported_terminal" if row and row["term_program"] else "no_terminal"
        trace.finish(outcome)
        print(f"no terminal recorded for {key}", file=sys.stderr)
        return 1

    term_program = row["term_program"]
    tty = _tty_path(row["tty"])
    # Logged before the first osascript/open call, so a click whose
    # AppleScript hangs still leaves a line at the default level.
    diag.info("cli", "focus.begin", **trace.fields)

    if term_program == "iTerm.app":
        unique_id = _iterm_unique_id(row["iterm_session_id"]) or ""
        result = _run_bounded(
            run_osascript, ["osascript", "-e", _ITERM_FOCUS_APPLESCRIPT, unique_id, tty or ""],
        )
        rc, out = trace.tried("iterm_script", result, unique_id=unique_id or None, tty=tty)
        if isinstance(result, _TimedOut):
            return _focus_timed_out(trace, key)
        outcome = _ITERM_FOCUS_RESULTS.get(out) if rc in (None, 0) else None
        if outcome:
            trace.finish(outcome)
            return 0
        fallback = _activate_app(term_program, run_osascript)
        if fallback is not None:
            trace.tried("open_a", fallback, app=TERM_PROGRAM_APP_NAMES[term_program])
            if isinstance(fallback, _TimedOut):
                return _focus_timed_out(trace, key)
            trace.finish(_open_a_outcome(fallback))
            return 0
        trace.finish("unsupported_terminal")
        print(f"iTerm session for {key} not found", file=sys.stderr)
        return 1

    if term_program == "Apple_Terminal":
        result = _run_bounded(
            run_osascript, ["osascript", "-e", _TERMINAL_FOCUS_APPLESCRIPT, tty or ""],
        )
        rc, out = trace.tried("terminal_script", result, tty=tty)
        if isinstance(result, _TimedOut):
            return _focus_timed_out(trace, key)
        outcome = _TERMINAL_FOCUS_RESULTS.get(out) if rc in (None, 0) else None
        if not outcome:
            # rc 0 with an answer the CLI does not recognise is not a failure.
            outcome = "not_found" if rc not in (None, 0) else "matched_unknown"
        trace.finish(outcome)
        return 0

    fallback = _activate_app(term_program, run_osascript)
    if fallback is not None:
        trace.tried("open_a", fallback, app=TERM_PROGRAM_APP_NAMES[term_program])
        if isinstance(fallback, _TimedOut):
            return _focus_timed_out(trace, key)
        trace.finish(_open_a_outcome(fallback))
        return 0
    trace.finish("unsupported_terminal")
    print(f"no way to focus terminal for {key}", file=sys.stderr)
    return 1


def cmd_resume(args, run_osascript=subprocess.run):
    key = f"{args.tool}:{args.session_id}"
    started = time.monotonic()
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT cwd, term_program FROM sessions WHERE tool=? AND session_id=?",
            (args.tool, args.session_id),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        _report("error", "resume", f"unknown session {key}", key=key)
        return 1
    command = _resume_command(args.tool, row["cwd"], args.session_id)
    if args.print:
        diag.debug("cli", "resume", key=key, strategy="print", command=command)
        print(command)
        return 0

    term_program = row["term_program"]
    if term_program == "Apple_Terminal":
        strategy = "terminal_script"
        result = _run_bounded(
            run_osascript, ["osascript", "-e", _TERMINAL_RESUME_APPLESCRIPT, command],
        )
    elif term_program == "iTerm.app" or not term_program:
        strategy = "iterm_script"
        result = _run_bounded(
            run_osascript, ["osascript", "-e", _ITERM_RESUME_APPLESCRIPT, command],
        )
    else:
        strategy = "open_a"
        result = _activate_app(term_program, run_osascript)
    timed_out = isinstance(result, _TimedOut)
    diag.log(
        "warn" if timed_out else "info", "cli", "resume", key=key, term=term_program, strategy=strategy, command=command,
        rc=_returncode(result), stderr=diag.tail_text(_stderr_text(result), 300) or None,
        outcome="timeout" if timed_out else None,
        ms=diag.ms_since(started),
    )
    if timed_out:
        print(f"resuming {key} timed out after {OSASCRIPT_TIMEOUT_SECS}s", file=sys.stderr)
        return 1
    return 0


# -- prune ----------------------------------------------------------------------

DEFAULT_PRUNE_DAYS = 90
DEFAULT_COMPACT_DAYS = 7
# `prune --auto` runs at most this often; the app calls it every 6 h.
PRUNE_AUTO_INTERVAL_SECS = 24 * 3600
# `prune --auto` vacuums at most this often, and only when the free pages
# exceed max(VACUUM_MIN_FREE_BYTES, VACUUM_MIN_FREE_FRACTION of the file):
# VACUUM holds the write lock for the whole copy.
VACUUM_AUTO_INTERVAL_SECS = 7 * 24 * 3600
VACUUM_MIN_FREE_BYTES = 8 * 1024 * 1024
VACUUM_MIN_FREE_FRACTION = 0.25
# Tool-use and subagent rows: nothing reads them once the turn is over (the
# session row carries `last_event`), and they are most of the table.
COMPACTED_EVENTS = (
    "PreToolUse", "PostToolUse", "PostToolUseFailure", "SubagentStart",
    "SubagentStop", "PreCompact", "PostCompact",
)
PRUNE_META_LAST_PRUNE = "last_prune_at"
PRUNE_META_LAST_VACUUM = "last_vacuum_at"


def _retention_days(config, key, default):
    """A non-negative integer config value, else `default`. 0 means never."""
    value = config.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return default
    return value


def _meta_value(conn, key):
    row = conn.execute("SELECT value FROM notes_meta WHERE key=?", (key,)).fetchone()
    if row is None or row["value"] is None:
        return None
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return None


def _set_meta_value(conn, key, value):
    conn.execute(
        "INSERT INTO notes_meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, int(value)),
    )


def _prune_keep_reason(conn, session, config, now):
    """Why an old session must survive the prune, or None."""
    tool, session_id = session["tool"], session["session_id"]
    if conn.execute(
        "SELECT 1 FROM notes WHERE tool=? AND session_id=? AND kind='needs_you' "
        "AND dismissed_at IS NULL LIMIT 1",
        (tool, session_id),
    ).fetchone():
        return "needs_you_note"
    for row in conn.execute(
        "SELECT * FROM links WHERE tool=? AND session_id=? AND kind='pr'", (tool, session_id)
    ):
        if links.pr_awaits_you(dict(row), session, now):
            return "awaiting_pr"
    triage = conn.execute(
        "SELECT status, until FROM triage WHERE tool=? AND session_id=?", (tool, session_id)
    ).fetchone()
    if triage and triage["status"] == "snoozed":
        until = reconcile.parse_ts(triage["until"])
        if until is not None and until > now:
            return "snoozed"
    if not session.get("ended_at") and reconcile.is_pid_alive(session.get("pid")) is True:
        return "alive"
    return None


def _vacuum_wanted(conn, db_file, now_epoch):
    """`--auto`'s own vacuum rule: enough free pages, and not done this week."""
    last = _meta_value(conn, PRUNE_META_LAST_VACUUM)
    if last is not None and now_epoch - last < VACUUM_AUTO_INTERVAL_SECS:
        return False
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    free_bytes = conn.execute("PRAGMA freelist_count").fetchone()[0] * page_size
    try:
        file_bytes = db_file.stat().st_size
    except OSError:
        file_bytes = 0
    return free_bytes > max(VACUUM_MIN_FREE_BYTES, VACUUM_MIN_FREE_FRACTION * file_bytes)


def cmd_prune(args):
    now = datetime.now(timezone.utc)
    now_epoch = int(now.timestamp())
    config = links.load_config(db.home_dir())
    days = args.days if args.days is not None else _retention_days(
        config, "prune_days", DEFAULT_PRUNE_DAYS)
    compact_days = args.compact_days if args.compact_days is not None else _retention_days(
        config, "compact_days", DEFAULT_COMPACT_DAYS)
    if days < 0 or compact_days < 0:
        _report("error", "prune_bad_args", "prune: --days and --compact-days must be 0 or more")
        return 2

    conn = db.connect()
    try:
        if args.auto:
            last = _meta_value(conn, PRUNE_META_LAST_PRUNE)
            if last is not None and 0 <= now_epoch - last < PRUNE_AUTO_INTERVAL_SECS:
                diag.debug("cli", "prune_skipped", reason="recent", last_prune_at=last)
                print("prune skipped: it ran less than 24 h ago")
                return 0

        # 0 means never, for sessions and dismissed notes alike.
        cutoff = _now_iso(now - timedelta(days=days)) if days else None
        compact_cutoff = _now_iso(now - timedelta(days=compact_days)) if compact_days else None

        doomed, skipped = [], {}
        if cutoff:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE updated_at IS NOT NULL AND updated_at < ?",
                (cutoff,),
            ).fetchall()
            for row in rows:
                session = dict(row)
                reason = _prune_keep_reason(conn, session, config, now)
                if reason:
                    skipped[reason] = skipped.get(reason, 0) + 1
                else:
                    doomed.append((session["tool"], session["session_id"]))
        skipped_total = sum(skipped.values())

        placeholders = ",".join("?" for _ in COMPACTED_EVENTS)
        compact_sql = f"FROM events WHERE event IN ({placeholders}) AND ts < ?"

        if args.dry_run:
            compacted = 0
            if compact_cutoff:
                compacted = conn.execute(
                    f"SELECT COUNT(*) {compact_sql}", (*COMPACTED_EVENTS, compact_cutoff)
                ).fetchone()[0]
            dismissed = 0
            if cutoff:
                dismissed = conn.execute(
                    "SELECT COUNT(*) FROM notes WHERE dismissed_at IS NOT NULL AND dismissed_at < ?",
                    (cutoff,),
                ).fetchone()[0]
            print(f"would prune {len(doomed)} session(s) ({skipped_total} skipped), "
                  f"compact {compacted} event(s), {dismissed} dismissed note(s)")
            return 0

        conn.execute("BEGIN IMMEDIATE")
        try:
            for tool, session_id in doomed:
                for table in ("events", "sessions", "triage", "links", "notes"):
                    conn.execute(
                        f"DELETE FROM {table} WHERE tool=? AND session_id=?", (tool, session_id),
                    )
            compacted = 0
            if compact_cutoff:
                compacted = conn.execute(
                    f"DELETE {compact_sql}", (*COMPACTED_EVENTS, compact_cutoff)
                ).rowcount
            # The only retention for dismissed notes: they are kept (so a
            # `notes --rebuild --reset` does not resurrect them) until they
            # are older than the prune window.
            dismissed = 0
            if cutoff:
                dismissed = conn.execute(
                    "DELETE FROM notes WHERE dismissed_at IS NOT NULL AND dismissed_at < ?",
                    (cutoff,),
                ).rowcount
            if args.auto:
                _set_meta_value(conn, PRUNE_META_LAST_PRUNE, now_epoch)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

        vacuumed = False
        if args.vacuum or (args.auto and _vacuum_wanted(conn, db.db_path(), now_epoch)):
            conn.execute("VACUUM")
            _set_meta_value(conn, PRUNE_META_LAST_VACUUM, now_epoch)
            vacuumed = True
    finally:
        conn.close()

    diag.info("cli", "prune", days=days, compact_days=compact_days, cutoff=cutoff,
              sessions=len(doomed), skipped=skipped_total,
              skipped_reasons=",".join(f"{k}:{v}" for k, v in sorted(skipped.items())) or None,
              compacted=compacted, dismissed_notes=dismissed, vacuumed=vacuumed,
              auto=bool(args.auto))
    print(f"pruned {len(doomed)} session(s) ({skipped_total} skipped), "
          f"compacted {compacted} event(s), {dismissed} dismissed note(s)"
          + (", vacuumed" if vacuumed else ""))
    return 0


# -- paths ----------------------------------------------------------------------
#
# The app (watching AGENTBOARD_HOME for changes) and anything else that
# needs to find AgentBoard's own files without duplicating this logic
# shells out to `agentboard paths --json` instead.

def _paths_dict() -> dict:
    return {
        "home": str(db.home_dir()),
        "db": str(db.db_path()),
        "core": str(_bin_path().parent.parent),
    }


def cmd_paths(args):
    paths = _paths_dict()
    if args.json:
        print(json.dumps(paths))
    else:
        for key in ("home", "db", "core"):
            print(f"{key}: {paths[key]}")
    return 0


# -- logs -------------------------------------------------------------------------
#
# Reads agentboard.log back (the rotated .1 first, so a --since window right
# after a rotation is complete) and, only with --app, merges the app's own
# OSLog lines from `log show`. Never called by snapshot or the app's polling.

DEFAULT_LOG_LINES = 200
APP_LOG_SUBSYSTEM = "io.github.agentboard"
APP_LOG_DEFAULT_WINDOW = timedelta(hours=1)
APP_LOG_TIMEOUT_SECS = 60
_SINCE_RELATIVE_RE = re.compile(r"^(\d+)([smhd])$")
_SINCE_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
_APP_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+(.*)$")


def _parse_since(value: str, now: datetime) -> datetime:
    """`<n>s|m|h|d` before `now`, or an ISO-8601 timestamp (naive = local
    time). Returns an aware UTC datetime; raises ValueError otherwise."""
    text = value.strip()
    match = _SINCE_RELATIVE_RE.match(text)
    if match:
        delta = timedelta(**{_SINCE_UNITS[match.group(2)]: int(match.group(1))})
        return now - delta
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(timezone.utc)


def _log_line_timestamp(line: str):
    head = line.split(" ", 1)[0]
    if len(head) < 20 or "T" not in head:
        return None
    try:
        parsed = datetime.fromisoformat(head)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _read_log_lines(home: Path) -> list:
    lines = []
    for name in (diag.ROTATED_FILENAME, diag.LOG_FILENAME):
        path = home / name
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                lines.extend(line.rstrip("\n") for line in fh)
        except FileNotFoundError:
            continue
        except OSError as exc:
            print(f"warning: could not read {path}: {exc}", file=sys.stderr)
    return lines


def _format_utc(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z"


def _app_log_show_argv(start: datetime) -> list:
    """`log show` for the app's OSLog lines since `start`. `--start` takes
    an absolute local time, which works for both a relative and an ISO
    --since (`--last` takes only m/h/d durations)."""
    return [
        "log", "show", "--style", "compact", "--info", "--debug",
        "--start", start.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        "--predicate", f'subsystem == "{APP_LOG_SUBSYSTEM}"',
    ]


def _read_app_log_lines(start: datetime, run) -> list:
    """[(utc datetime, rendered line)] from `log show`, or [] with a
    one-line warning on stderr if it is missing or fails."""
    argv = _app_log_show_argv(start)
    try:
        result = run(argv, capture_output=True, text=True, timeout=APP_LOG_TIMEOUT_SECS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"warning: could not read app logs ({type(exc).__name__}: {exc}); "
              "showing agentboard.log only", file=sys.stderr)
        return []
    rc = _returncode(result)
    if rc not in (None, 0):
        detail = _first_line(_stderr_text(result), 200) or f"exit {rc}"
        print(f"warning: `log show` failed ({detail}); showing agentboard.log only",
              file=sys.stderr)
        return []
    entries = []
    for raw in _stdout_text(result).splitlines():
        match = _APP_LINE_RE.match(raw)
        if not match:
            continue  # header row, or a continuation without a timestamp
        try:
            local = datetime.fromisoformat(match.group(1))
        except ValueError:
            continue
        ts = local.astimezone().astimezone(timezone.utc)
        entries.append((ts, f"{_format_utc(ts)} APP {match.group(2)}"))
    return entries


def cmd_logs(args, run=subprocess.run, now=None):
    now = now or datetime.now(timezone.utc)
    cutoff = None
    if args.since:
        try:
            cutoff = _parse_since(args.since, now)
        except ValueError:
            print(
                f"error: --since must be <n>s, <n>m, <n>h, <n>d or an ISO-8601 time, got {args.since!r}",
                file=sys.stderr,
            )
            return 2

    lines = _read_log_lines(db.home_dir())
    stamped = []
    previous = None
    for line in lines:
        ts = _log_line_timestamp(line)
        if cutoff is not None and ts is not None and ts < cutoff:
            previous = ts
            continue
        if args.grep and args.grep not in line:
            previous = ts or previous
            continue
        stamped.append((ts or previous, line))
        previous = ts or previous

    limit = args.lines if args.lines is not None else (None if args.since else DEFAULT_LOG_LINES)
    if limit is not None:
        stamped = stamped[-limit:] if limit > 0 else []

    output = stamped
    if args.app:
        start = cutoff
        if start is None:
            known = [ts for ts, _ in stamped if ts is not None]
            start = min(known) if known else now - APP_LOG_DEFAULT_WINDOW
        app_entries = [
            entry for entry in _read_app_log_lines(start, run)
            if not args.grep or args.grep in entry[1]
        ]
        floor = datetime.min.replace(tzinfo=timezone.utc)
        output = sorted(stamped + app_entries, key=lambda entry: entry[0] or floor)

    text = "".join(f"{line}\n" for _, line in output)
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except BrokenPipeError:
        # `agentboard logs | head`: stop quietly instead of a traceback.
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except (OSError, ValueError, AttributeError):
            pass
    return 0


# -- argparse wiring --------------------------------------------------------------

def build_parser():
    # allow_abbrev=False on the parser and every subparser (add_parser does
    # not inherit it): an abbreviated flag (`--not` for `--note`) would
    # otherwise be accepted and slip past diag.redact's exact-flag match.
    parser = argparse.ArgumentParser(prog="agentboard", allow_abbrev=False)
    sub = parser.add_subparsers(
        dest="command", required=True,
        parser_class=functools.partial(argparse.ArgumentParser, allow_abbrev=False),
    )

    p = sub.add_parser("hook")
    p.add_argument("--tool", choices=["claude", "codex"], required=True)
    p.set_defaults(func=cmd_hook)

    p = sub.add_parser("snapshot")
    p.add_argument("--json", action="store_true")
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("mark")
    p.add_argument("tool", choices=["claude", "codex"])
    p.add_argument("session_id")
    p.add_argument("action", choices=["done", "open", "snooze"])
    p.add_argument("--hours", type=float, default=None)
    p.add_argument("--note", default=None)
    p.set_defaults(func=cmd_mark)

    p = sub.add_parser("backfill")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--session", default=None, help="TOOL:ID -- re-parse just this one session")
    p.add_argument("--transcript", default=None, help="transcript/rollout path (with --session)")
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("refresh-links")
    p.set_defaults(func=cmd_refresh_links)

    p = sub.add_parser("link")
    p.add_argument("--kind", choices=["pr", "ticket", "url"], required=True)
    p.add_argument("--ref", required=True)
    p.add_argument("--url", default=None)
    p.add_argument("--state", choices=["OPEN", "MERGED", "CLOSED", "DONE"], default=None)
    p.add_argument("--title", default=None)
    p.add_argument("--branch", default=None,
                   help="the PR's head branch (default: the session's current branch)")
    p.add_argument("--session", default=None, help="TOOL:ID, auto-resolved if omitted")
    p.set_defaults(func=cmd_link)

    p = sub.add_parser("status")
    p.add_argument("--text", required=True)
    p.add_argument("--ttl", type=float, default=None, help="minutes until this status expires")
    p.add_argument("--session", default=None, help="TOOL:ID, auto-resolved if omitted")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("title", help="name a session (a title you set yourself is kept)")
    p.add_argument("--text", required=True)
    p.add_argument("--session", default=None, help="TOOL:ID, auto-resolved if omitted")
    p.set_defaults(func=cmd_title)

    p = sub.add_parser("note", help="add a retained note to a session")
    p.add_argument("--kind", choices=list(notes_mod.KINDS), required=True)
    p.add_argument("--text", required=True)
    p.add_argument("--session", default=None, help="TOOL:ID, auto-resolved if omitted")
    p.set_defaults(func=cmd_note)

    p = sub.add_parser("notes", help="list, dismiss or rebuild retained notes")
    p.add_argument("--session", default=None, help="TOOL:ID: only this session's notes")
    p.add_argument("--all", action="store_true", help="include dismissed notes")
    p.add_argument("--kind", nargs="+", choices=list(notes_mod.KINDS), default=None,
                   help="only notes of these kinds (a listing or --dismiss-all)")
    p.add_argument("--json", action="store_true")
    action = p.add_mutually_exclusive_group()
    action.add_argument("--dismiss", nargs="+", type=int, metavar="ID", default=None)
    action.add_argument("--dismiss-all", action="store_true",
                        help="dismiss every open note (only --session's with --session, "
                             "only --kind's with --kind)")
    action.add_argument("--rebuild", action="store_true",
                        help="classify recorded Stop events the hook has not seen")
    p.add_argument("--days", type=int, default=None,
                   help="with --rebuild: only the last N days (older events are then marked "
                        "replayed; use --reset to revisit them)")
    p.add_argument("--reset", action="store_true",
                   help="with --rebuild: drop open stop/backfill notes and replay every event")
    p.set_defaults(func=cmd_notes)

    p = sub.add_parser("install-hooks")
    p.add_argument("--claude", action="store_true")
    p.add_argument("--codex", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--bin-path", default=None,
        help="override the path baked into the hook command (install.sh --dry-run previews)",
    )
    p.set_defaults(func=cmd_install_hooks)

    p = sub.add_parser("uninstall-hooks")
    p.add_argument("--claude", action="store_true")
    p.add_argument("--codex", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_uninstall_hooks)

    p = sub.add_parser("enable-codex-hooks")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_enable_codex_hooks)

    p = sub.add_parser("focus")
    p.add_argument("tool", choices=["claude", "codex"])
    p.add_argument("session_id")
    p.set_defaults(func=cmd_focus)

    p = sub.add_parser("resume")
    p.add_argument("tool", choices=["claude", "codex"])
    p.add_argument("session_id")
    p.add_argument("--print", action="store_true")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("map-codex")
    p.add_argument("--session", required=True)
    p.add_argument("--cwd", required=True)
    p.add_argument("--at", default=None)
    p.set_defaults(func=cmd_map_codex)

    p = sub.add_parser("prune")
    p.add_argument("--days", type=int, default=None,
                   help="delete sessions idle this long (config prune_days, default 90; 0 never)")
    p.add_argument("--compact-days", type=int, default=None,
                   help="delete tool-use events older than this (config compact_days, default 7; "
                        "0 never)")
    p.add_argument("--dry-run", action="store_true", help="print the counts, change nothing")
    p.add_argument("--auto", action="store_true",
                   help="run only if the last auto run was 24 h ago or more")
    p.add_argument("--vacuum", action="store_true", help="VACUUM the database afterwards")
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser("paths")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_paths)

    p = sub.add_parser("logs", help="print the diagnostic log (agentboard.log.1 + agentboard.log)")
    p.add_argument("--since", default=None, help="<n>s|m|h|d, or an ISO-8601 time")
    p.add_argument("--grep", default=None, help="plain substring filter")
    p.add_argument("--lines", "-n", type=int, default=None,
                   help=f"show only the last N matching lines (default {DEFAULT_LOG_LINES} without --since)")
    p.add_argument("--app", action="store_true",
                   help="also merge the app's OSLog lines from `log show`")
    p.set_defaults(func=cmd_logs)

    return parser


# Subcommands the app calls every few seconds (or that only read state):
# their start/end lines are debug, so a quiet `info` log isn't one line per
# poll. A failure or a slow run (SLOW_QUIET_MS) is still logged at info.
QUIET_COMMANDS = {"snapshot", "paths", "hook", "logs"}
SLOW_QUIET_MS = 1000
# The polled subset: a slow run of one of these is worth an info line.
SLOW_WATCHED_COMMANDS = {"snapshot", "paths", "hook"}


def _hook_spawned(command, args):
    """The children hook.py spawns once per assistant turn or Codex prompt
    (`backfill --session`, `map-codex`): routine successes stay at debug,
    like the polled commands, so `info` keeps meaning real problems."""
    return command == "map-codex" or (command == "backfill" and bool(getattr(args, "session", None)))


def main(argv=None):
    parser = build_parser()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(raw_argv)
    command = args.command
    spawned = _hook_spawned(command, args)
    quiet = command in QUIET_COMMANDS or spawned
    # `logs` stays side-effect free on success: neither a start line (which
    # would be the last line it prints at debug level) nor an end line, so
    # repeated `logs` runs while debugging do not fill the file with their
    # own footprints. A failing `logs` still records its end line.
    self_silent = command == "logs"
    if not self_silent:
        diag.debug("cli", "start", cmd=command, argv=diag.redact(raw_argv))
    started = time.monotonic()
    try:
        rc = args.func(args)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        if not (self_silent and code == 0):
            diag.log("debug" if quiet and code == 0 else "info", "cli", "end",
                     cmd=command, exit=code, ms=diag.ms_since(started))
        raise
    except BaseException as exc:
        diag.error("cli", "error", cmd=command, exc=type(exc).__name__,
                   msg=str(exc), ms=diag.ms_since(started))
        raise
    elapsed = diag.ms_since(started)
    exit_code = rc if isinstance(rc, int) else (0 if rc is None else 1)
    level = "info"
    slow = (
        (command in SLOW_WATCHED_COMMANDS or spawned)
        and elapsed is not None and elapsed > SLOW_QUIET_MS
    )
    if quiet and exit_code == 0 and not slow:
        level = "debug"
    if not (self_silent and exit_code == 0):
        diag.log(level, "cli", "end", cmd=command, exit=exit_code, ms=elapsed)
    return rc


if __name__ == "__main__":
    sys.exit(main())
