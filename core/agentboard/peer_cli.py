"""`agentboard peers`, `peer-open` and the `--machine` branch of focus/resume/mark.

These are the only commands besides `refresh-links` and `peer-sync` that may
touch the network: each remote action is one bounded ssh call (or, for
`peer-open`, an interactive ssh exec'd inside a terminal window).
"""

import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone

from . import diag
from . import peers

# A mark or best-effort focus on a peer: one ssh call, never longer than this.
REMOTE_CALL_TIMEOUT_SECS = 15


def _report(event, message, **fields):
    print(message, file=sys.stderr)
    diag.log("error", "peers", event, msg=message, **fields)


def _emit(data):
    print(json.dumps(data))


# -- peers ------------------------------------------------------------------------

def _list_payload(now=None, run=subprocess.run, connect=None):
    now = now or datetime.now(timezone.utc)
    config = peers.load_config()
    machine = peers.ensure_machine(run=run)
    from . import peer_address
    # Detected once per listing: the menu, the automatic choice and the code
    # all read the same candidates.
    candidates = peer_address.candidates(run)
    address = peers.advertised_address(config, run, candidates=candidates)
    code, code_error = None, None
    try:
        code = peers.build_code(run=run, address=address["address"])
    except peers.PeerError as exc:
        code_error = str(exc)
    _entries, peer_list = peers.remote_entries(config, now)
    authorized = peers.authorized_ids()
    for item in peer_list:
        found = peers.find_peer(config, item["id"]) or {}
        item["user"] = found.get("user")
        item["authorized"] = item["id"] in authorized
        item["fingerprint"] = found.get("fingerprint")
    return {
        "machine": machine,
        "code": code["code"] if code else None,
        "fingerprint": code["fingerprint"] if code else None,
        "host": code["host"] if code else None,
        "user": code["user"] if code else None,
        "code_error": code_error,
        "address": address,
        "address_automatic": peer_address.automatic(candidates),
        "address_candidates": candidates,
        "remote_login": peers.remote_login_status(connect) if connect else peers.remote_login_status(),
        "share": peers.share_level(config),
        "peers": peer_list,
    }


def cmd_peers(args):
    action = args.peers_command
    try:
        if action == "list":
            payload = _list_payload()
            if args.json:
                _emit(payload)
            else:
                m = payload["machine"]
                a = payload["address"]
                print(f"this Mac: {m['name']} ({m['id']}), address {a['address']} "
                      f"({'set by you' if a['source'] == 'config' else 'automatic'}), "
                      f"share {payload['share']}, Remote Login {payload['remote_login']}")
                for p in payload["peers"]:
                    print(f"{p['status']:<12} {p['name']:<24} {p['host']} "
                          f"({p['session_count']} sessions)")
            return 0
        if action == "add":
            if args.dry_run:
                parsed = peers.parse_code(args.code)
                relation = peers.code_relation(parsed)
                previous = peers.replaced_entry(peers.load_config(), parsed)
                replaces = previous and {k: previous.get(k) for k in ("id", "name", "host",
                                                                      "allow_open")}
                _emit({k: parsed[k] for k in ("id", "name", "user", "host", "fingerprint")}
                      | {"own": relation == "own",
                         "copied_identity": relation == "copied",
                         "error": (peers.COPIED_IDENTITY_ERROR if relation == "copied"
                                   else peers.OWN_CODE_ERROR if relation == "own" else None),
                         "known": previous is not None, "replaces": replaces or None,
                         "current_host": previous.get("host") if previous else None})
                return 0
            allow = None if args.allow_open is None else args.allow_open == "on"
            peer = peers.add_peer(args.code, host=args.host, name=args.name, allow_open=allow)
            _emit({k: peer[k] for k in ("id", "name", "host", "user", "allow_open", "fingerprint")})
            return 0
        if action == "remove":
            if not args.all and not args.id:
                _report("remove", "error: pass --id ID or --all")
                return 2
            dropped = peers.remove_peer(None if args.all else args.id, dry_run=args.dry_run)
            if args.dry_run:
                print(f"would remove {dropped} AgentBoard line(s) from {peers.authorized_keys_path()}")
            else:
                _emit({"removed_authorized_keys_lines": dropped})
            return 0
        if action == "set":
            allow = None if args.allow_open is None else args.allow_open == "on"
            peer = peers.set_peer(args.id, host=args.host, name=args.name, allow_open=allow,
                                  dismiss_address=args.dismiss_address)
            _emit({k: peer[k] for k in ("id", "name", "host", "user", "allow_open")})
            return 0
        if action == "set-self":
            if args.address is not None or args.auto_address:
                peers.set_address(None if args.auto_address else args.address)
            if args.share:
                peers.set_share(args.share)
            machine = peers.set_machine_name(args.name) if args.name else peers.ensure_machine()
            config = peers.load_config()
            _emit({"machine": machine, "share": peers.share_level(config),
                   "address": peers.advertised_address(config)})
            return 0
    except peers.PeerError as exc:
        _report(f"{action}_failed", f"error: {exc}")
        return 1
    _report("unknown", f"error: unknown peers action {action!r}")
    return 2


# -- ssh argv ---------------------------------------------------------------------

class PeerUnavailable(Exception):
    pass


def _ssh_path_option(name, path) -> str:
    """`Name="path"` for ssh -o: ssh splits an unquoted value at whitespace
    (the default data directory is ".../Application Support/AgentBoard") and
    expands %-tokens in these options, so the path is quoted and % doubled.
    A path ssh would still rewrite inside the quotes (a double quote, a
    backslash, `${NAME}`) or a control character is refused."""
    text = str(path)
    if ('"' in text or "\\" in text or "${" in text
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in text)):
        raise PeerUnavailable(f"AgentBoard's data folder path can't be passed to ssh: {text!r}")
    return '%s="%s"' % (name, text.replace("%", "%%"))


def ssh_argv(peer, remote_command, tty=False, home=None):
    """The ssh argv for one call to `peer`'s gate: AgentBoard's own key and
    known_hosts only, the host key pinned under its alias, no prompts.

    Raises PeerUnavailable when AgentBoard's key or pinned known_hosts is
    missing: ssh would otherwise skip the missing key and offer the user's
    default personal keys (~/.ssh/id_*), which log in past the forced
    command wherever they are authorized."""
    key, known_hosts = peers.key_path(home), peers.known_hosts_path(home)
    if not key.is_file():
        raise PeerUnavailable("AgentBoard's ssh key is missing on this Mac; "
                              "remove the machine and pair it again")
    if not known_hosts.is_file():
        raise PeerUnavailable("the pinned host keys are missing on this Mac; "
                              "remove the machine and pair it again")
    argv = [
        "/usr/bin/ssh", "-tt" if tty else "-T",
        # No ssh_config at all: a personal IdentityFile from it would log in
        # past the forced command, and ProxyCommand, KnownHostsCommand or
        # VerifyHostKeyDNS from it would bypass the pinned host key.
        "-F", "none",
        # IdentityFile as an option, not -i: it keeps the identity list at
        # this one file even if it disappears between the check above and
        # ssh reading it.
        "-o", _ssh_path_option("IdentityFile", key),
        "-o", "IdentitiesOnly=yes",
        "-o", "IdentityAgent=none",
        "-o", "BatchMode=yes",
        "-o", _ssh_path_option("UserKnownHostsFile", known_hosts),
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"HostKeyAlias={peers.host_alias(peer['id'])}",
        "-o", "ConnectTimeout=5",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=2",
        "-o", "ControlMaster=no",
        "-o", "ControlPath=none",
        "-o", "ClearAllForwardings=yes",
        "-o", "ForwardAgent=no",
        "-o", "ForwardX11=no",
        "-o", "VerifyHostKeyDNS=no",
        "-o", "PermitLocalCommand=no",
        "-o", "LogLevel=ERROR",
        "-l", peer["user"],
        "--", peer["host"],
        remote_command,
    ]
    return argv


# -- remote focus / resume / mark ------------------------------------------------------


def _remote_target(machine_id, tool, session_id, now=None):
    """(peer, session entry or None, status) for a remote action; raises
    PeerUnavailable with a user-facing message when it can't be reached."""
    now = now or datetime.now(timezone.utc)
    if not peers._SESSION_ID_RE.match(session_id or ""):
        raise PeerUnavailable(f"invalid session id {session_id!r}")
    config = peers.load_config()
    peer = peers.find_peer(config, machine_id)
    if peer is None:
        raise PeerUnavailable(f"no paired machine with id {machine_id}")
    cache = peers.read_cache(machine_id)
    status, last_seen = peers.peer_status(cache, now)
    if status != "online":
        raise PeerUnavailable(f"{peer['name']} is {status if status != 'never' else 'not connected yet'}")
    session = next((s for s in (cache or {}).get("sessions") or []
                    if isinstance(s, dict) and s.get("tool") == tool
                    and s.get("session_id") == session_id), None)
    return peer, session


def _shim():
    from . import cli
    return str(cli._bin_path())


def peer_open_command(machine_id, verb, tool, session_id) -> str:
    return " ".join(shlex.quote(part) for part in (
        _shim(), "peer-open", "--machine", machine_id, "--verb", verb, tool, session_id,
    ))


def _open_terminal_window(command, run):
    from . import cli
    installed = cli._run_bounded(run, ["open", "-Ra", "iTerm"])
    if not isinstance(installed, cli._TimedOut) and cli._returncode(installed) == 0:
        script, strategy = cli._ITERM_NEW_WINDOW_APPLESCRIPT, "iterm_new_window"
    else:
        script, strategy = cli._TERMINAL_RESUME_APPLESCRIPT, "terminal_script"
    result = cli._run_bounded(run, ["osascript", "-e", script, command])
    if isinstance(result, cli._TimedOut):
        return strategy, None
    return strategy, cli._returncode(result)


def remote_focus(args, run=subprocess.run, now=None):
    """focus/resume --machine: open the session the way its origin says
    (`remote_open`). A live session is never resumed (that would fork the
    conversation), so `resume` on a live tmux row attaches instead.
    Returns an exit code."""
    key = f"{args.tool}:{args.session_id}@{args.machine}"
    try:
        peer, session = _remote_target(args.machine, args.tool, args.session_id, now)
    except PeerUnavailable as exc:
        _report("remote_focus", str(exc), key=key, outcome="unavailable")
        return peers.EXIT_PEER_UNAVAILABLE
    if session is None:
        _report("remote_focus", f"{peer['name']} no longer lists {args.tool}:{args.session_id}",
                key=key, outcome="unknown_session")
        return 1
    how = session.get("remote_open")
    if how is None:
        _report("remote_focus", f"{peer['name']} does not allow opening its sessions from this Mac",
                key=key, outcome="not_allowed")
        return 1
    if how == "remote-control":
        return _open_remote_control(peer, session, args, key, run)
    verb = {"tmux": "attach", "resume": "resume"}.get(how)
    if verb is None:
        return _screen_share(peer, args, key, run)
    try:
        ssh_argv(peer, verb)  # fail here, not in the new window, when the key files are gone
    except PeerUnavailable as exc:
        _report("remote_focus", str(exc), key=key, outcome="unavailable")
        return peers.EXIT_PEER_UNAVAILABLE
    command = peer_open_command(peer["id"], verb, args.tool, args.session_id)
    if getattr(args, "print", False):
        print(command)
        return 0
    strategy, rc = _open_terminal_window(command, run)
    diag.info("peers", "remote_open", key=key, verb=verb, strategy=strategy, rc=rc)
    if rc != 0:
        _report("remote_open", f"opening a terminal window for {peer['name']} failed", key=key)
        return 1
    return 0


def _open_remote_control(peer, session, args, key, run):
    """A session served by Claude Code Remote Control: open its URL in the
    browser here (validated on receipt, peers._REMOTE_CONTROL_URL_RE)."""
    from . import cli
    url = session.get("remote_control_url")
    if not isinstance(url, str) or not peers._REMOTE_CONTROL_URL_RE.match(url):
        return _screen_share(peer, args, key, run)
    if getattr(args, "print", False):
        print(url)
        return 0
    opened = cli._run_bounded(run, ["open", url])
    rc = None if isinstance(opened, cli._TimedOut) else cli._returncode(opened)
    diag.info("peers", "remote_open", key=key, verb="remote-control", rc=rc)
    if rc != 0:
        _report("remote_open", "couldn't open the Remote Control link", key=key)
        return 1
    return 0


def vnc_url(user, host) -> str:
    """`vnc://user@host`, with an IPv6 host in brackets and its zone id's
    `%` encoded as `%25` (RFC 6874)."""
    if ":" in host:
        host = "[" + host.replace("%", "%25") + "]"
    return f"vnc://{user}@{host}"


def _screen_share(peer, args, key, run):
    # Best effort: ask the peer to bring the session's tab forward, then
    # open Screen Sharing; a refused or failed focus is ignored.
    from . import cli
    try:
        result = cli._run_bounded(
            run, ssh_argv(peer, f"focus {args.tool} {args.session_id}"), timeout=REMOTE_CALL_TIMEOUT_SECS,
        )
        focus_rc = None if isinstance(result, cli._TimedOut) else cli._returncode(result)
    except PeerUnavailable:
        focus_rc = None
    url = vnc_url(peer["user"], peer["host"])
    opened = cli._run_bounded(run, ["open", url])
    rc = None if isinstance(opened, cli._TimedOut) else cli._returncode(opened)
    diag.info("peers", "remote_open", key=key, verb="screen", focus_rc=focus_rc, rc=rc)
    if rc != 0:
        _report("remote_open", f"couldn't open Screen Sharing to {peer['name']}", key=key)
        return 1
    return 0


def remote_mark(args, run=subprocess.run, now=None):
    key = f"{args.tool}:{args.session_id}@{args.machine}"
    try:
        peer, _session = _remote_target(args.machine, args.tool, args.session_id, now)
    except PeerUnavailable as exc:
        _report("remote_mark", str(exc), key=key, outcome="unavailable")
        return peers.EXIT_PEER_UNAVAILABLE
    from . import cli
    command = f"mark {args.tool} {args.session_id} {args.action}"
    if args.action == "snooze" and args.hours is not None:
        # Formatted the way the gate checks (_HOURS_RE: no exponent, at most
        # 3 decimals), so a value it would refuse fails here with a clear message.
        from . import peer_gate
        hours = f"{round(float(args.hours), 3):.3f}".rstrip("0").rstrip(".")
        if not peer_gate._HOURS_RE.match(hours):
            _report("remote_mark", "--hours for a session on another Mac must be "
                    "between 0 and 99999 with at most 3 decimals", key=key, outcome="bad_hours")
            return 1
        command += f" {hours}"
    try:
        argv = ssh_argv(peer, command)
    except PeerUnavailable as exc:
        _report("remote_mark", str(exc), key=key, outcome="unavailable")
        return peers.EXIT_PEER_UNAVAILABLE
    result = cli._run_bounded(run, argv, timeout=REMOTE_CALL_TIMEOUT_SECS)
    if isinstance(result, cli._TimedOut):
        _report("remote_mark", f"marking on {peer['name']} timed out", key=key, outcome="timeout")
        return peers.EXIT_PEER_UNAVAILABLE
    rc = cli._returncode(result)
    diag.info("peers", "remote_mark", key=key, action=args.action, rc=rc)
    if rc != 0:
        _report("remote_mark", f"{peer['name']} refused the mark: "
                f"{diag.tail_text(cli._stderr_text(result), 200) or 'ssh failed'}", key=key)
        return 1
    return 0


def cmd_peer_open(args, execv=os.execv):
    """Runs inside the terminal window focus --machine opened: exec ssh with
    a pty to the peer's gate (`attach` or `resume`)."""
    config = peers.load_config()
    peer = peers.find_peer(config, args.machine)
    if not peers._SESSION_ID_RE.match(args.session_id):
        _report("peer_open", f"invalid session id {args.session_id!r}")
        return 2
    if peer is None:
        _report("peer_open", f"no paired machine with id {args.machine}")
        return peers.EXIT_PEER_UNAVAILABLE
    try:
        argv = ssh_argv(peer, f"{args.verb} {args.tool} {args.session_id}", tty=True)
    except PeerUnavailable as exc:
        _report("peer_open", f"error: {exc}")
        return peers.EXIT_PEER_UNAVAILABLE
    diag.info("peers", "peer_open", peer=peer["id"], verb=args.verb,
              key=f"{args.tool}:{args.session_id}")
    execv(argv[0], argv)
    return 0
