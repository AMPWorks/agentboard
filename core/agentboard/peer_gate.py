"""`agentboard peer-gate <peer_id>`: the forced command a paired Mac's key runs.

sshd runs it for every connection made with that key (authorized_keys
`command="... peer-gate <id>"`), with what the client asked for in
`SSH_ORIGINAL_COMMAND`. Only a fixed verb list is accepted, every argument
is checked against a strict pattern and against this Mac's own database,
and no text from the client ever reaches a shell:

    serve                               stream this Mac's export, one JSON line per change
    mark <tool> <id> done|open|snooze [hours]
    attach <tool> <id>                  exec tmux attach to the session's pane (needs a pty)
    resume <tool> <id>                  exec the session's resume command (ended sessions only)
    focus <tool> <id>                   bring the session's tab forward on this Mac

attach, resume and focus need the peer's "allow opening" switch on.
"""

import argparse
import os
import pwd
import re
import select
import signal
import subprocess
import sys
import time

from . import db
from . import diag
from . import peers

VERBS = ("serve", "mark", "attach", "resume", "focus")
OPEN_VERBS = ("attach", "resume", "focus")
TOOLS = ("claude", "codex")
MARK_ACTIONS = ("done", "open", "snooze")
MAX_COMMAND_CHARS = 256
_COMMAND_RE = re.compile(r"\A[A-Za-z0-9 ._:-]+\Z")
_SESSION_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_HOURS_RE = re.compile(r"\A[0-9]{1,5}(\.[0-9]{1,3})?\Z")
EXIT_REFUSED = 2
POLL_SECS = 1.0


class Refused(Exception):
    pass


def _refuse(peer_id, reason, verb=None):
    diag.warn("peer", "gate_refused", peer=peer_id, verb=verb, reason=reason)
    print(f"agentboard: refused: {reason}", file=sys.stderr)
    return EXIT_REFUSED


def parse_command(command):
    """(verb, args) from SSH_ORIGINAL_COMMAND, or Refused(reason)."""
    if not command:
        raise Refused("no command")
    if len(command) > MAX_COMMAND_CHARS or not _COMMAND_RE.match(command):
        raise Refused("bad characters")
    parts = command.split(" ")
    if any(not part for part in parts):
        raise Refused("bad spacing")
    verb, args = parts[0], parts[1:]
    if verb not in VERBS:
        raise Refused("unknown verb")
    if verb == "serve":
        if args:
            raise Refused("extra arguments")
        return verb, {}
    if verb == "mark":
        if len(args) not in (3, 4):
            raise Refused("wrong arguments")
    elif len(args) != 2:
        raise Refused("wrong arguments")
    tool, session_id = args[0], args[1]
    if tool not in TOOLS:
        raise Refused("unknown tool")
    if not _SESSION_RE.match(session_id):
        raise Refused("bad session id")
    parsed = {"tool": tool, "session_id": session_id}
    if verb == "mark":
        action = args[2]
        if action not in MARK_ACTIONS:
            raise Refused("unknown mark action")
        parsed["action"] = action
        parsed["hours"] = None
        if len(args) == 4:
            if action != "snooze" or not _HOURS_RE.match(args[3]):
                raise Refused("bad hours")
            hours = float(args[3])
            if not 0 < hours <= 24 * 366:
                raise Refused("bad hours")
            parsed["hours"] = hours
    return verb, parsed


def _session_row(tool, session_id):
    conn = db.connect()
    try:
        return conn.execute(
            "SELECT * FROM sessions WHERE tool=? AND session_id=?", (tool, session_id),
        ).fetchone()
    finally:
        conn.close()


def _local_entry(tool, session_id):
    from . import cli
    conn = db.connect()
    try:
        snapshot = cli.build_snapshot(conn, include_all=True, include_peers=False)
    finally:
        conn.close()
    key = f"{tool}:{session_id}"
    return next((e for e in snapshot["sessions"] if e["key"] == key), None)


# -- serve -------------------------------------------------------------------------

def _change_signature(home):
    sig = []
    for name in (db.DB_FILENAME, db.DB_FILENAME + "-wal", "config.json", peers.MACHINE_FILE):
        try:
            st = os.stat(home / name)
            sig.append((st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append(None)
    return tuple(sig)


def _stdin_closed(stdin, timeout, sleep=time.sleep, clock=time.monotonic):
    """Wait up to `timeout` for the client to go away; True on EOF. Data the
    client sends is read and dropped once per call, and the rest of the
    timeout is still waited, so a client flooding stdin can't make the serve
    loop spin."""
    started = clock()
    try:
        fd = stdin.fileno()
    except (AttributeError, OSError, ValueError):
        sleep(timeout)
        return False
    try:
        ready, _, _ = select.select([fd], [], [], timeout)
    except (OSError, ValueError):
        sleep(timeout)
        return False
    if not ready:
        return False
    try:
        if os.read(fd, 4096) == b"":
            return True
    except OSError:
        return True
    remaining = timeout - (clock() - started)
    if remaining > 0:
        sleep(remaining)
    return False


def serve(peer, out=None, stdin=None, clock=time.monotonic, wait=None, max_lines=None,
          build=None):
    """Stream exports to `out`: one line now, one whenever the board changes
    (checked every POLL_SECS, so at most one a second), and a heartbeat
    (the full export again) every peers.HEARTBEAT_SECS, which also carries
    time-driven state changes. Returns 0 when the client goes away or the
    peer is no longer paired (`build` returns None)."""
    out = out or sys.stdout
    stdin = stdin if stdin is not None else sys.stdin
    wait = wait or (lambda: _stdin_closed(stdin, POLL_SECS))
    home = db.home_dir()

    def default_build():
        # Looked up again on every build (a config.json change triggers one):
        # a removed peer's stream ends here, since sshd keeps an open
        # connection after its authorized_keys line goes, and a changed
        # allow-open switch reaches the next line.
        config = peers.load_config(home)
        current = peers.find_peer(config, peer["id"])
        if current is None or current.get("public_key") != peer.get("public_key"):
            return None  # removed, or paired again with new keys
        conn = db.connect()
        try:
            return peers.build_export(conn, peers.share_level(config),
                                      allow_open=current["allow_open"], home=home)
        finally:
            conn.close()

    build = build or default_build
    last_body, last_sent, last_sig, sent = None, None, None, 0
    diag.info("peer", "serve_start", peer=peer["id"])
    while True:
        sig = _change_signature(home)
        now = clock()
        heartbeat_due = last_sent is None or now - last_sent >= peers.HEARTBEAT_SECS
        if sig != last_sig or heartbeat_due:
            last_sig = sig
            export = build()
            if export is None:
                diag.info("peer", "serve_end", peer=peer["id"], reason="unpaired", lines=sent)
                return 0
            body = dict(export)
            body.pop("generated_at", None)
            if heartbeat_due or body != last_body:
                last_body = body
                try:
                    out.write(peers.encode_export(export) + "\n")
                    out.flush()
                except (BrokenPipeError, OSError, ValueError):
                    diag.info("peer", "serve_end", peer=peer["id"], reason="pipe", lines=sent)
                    return 0
                last_sent = now
                sent += 1
                if max_lines is not None and sent >= max_lines:
                    return 0
        if wait():
            diag.info("peer", "serve_end", peer=peer["id"], reason="eof", lines=sent)
            return 0


# -- the other verbs ---------------------------------------------------------------

def _mark(parsed):
    from . import cli
    args = argparse.Namespace(tool=parsed["tool"], session_id=parsed["session_id"],
                              action=parsed["action"], hours=parsed["hours"], note=None)
    return cli.cmd_mark(args)


# -- attach / resume: the gate stays the parent ---------------------------------------

_UNSET = object()


def open_allowed(peer_id, home=None, public_key=_UNSET) -> bool:
    """The peer is still paired with opening allowed (config.json re-read).
    With `public_key`, also with that same key: pairing the id again with
    new keys (rotating a leaked one) ends what the old key opened."""
    try:
        peer = peers.find_peer(peers.load_config(home), peer_id)
    except Exception:  # an unreadable config ends the session (fail closed)
        return False
    if not (peer and peer["allow_open"]):
        return False
    return public_key is _UNSET or peer.get("public_key") == public_key


def _process_table(run=subprocess.run):
    """{pid: ppid} for every live (non-zombie) process, or None when ps fails."""
    try:
        result = run(["/bin/ps", "-A", "-o", "pid=,ppid=,stat="], capture_output=True,
                     text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    table = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit() \
                and not parts[2].startswith("Z"):
            table[int(parts[0])] = int(parts[1])
    return table


def _session_members(sid, include_leader=True, table=None, known=()):
    """Every process the child's session started: members of session `sid`
    (whatever process group job control gave them), plus every descendant
    of those, which also covers one that called setsid() itself while its
    parent lives. `known` (pids found by an earlier call) stay tracked while
    alive, so one whose parent was ended first is not lost. The leader's pid
    is a root only while it is not reaped (a reaped pid can be reused).
    Never this process."""
    table = _process_table() if table is None else table
    if table is None:  # no ps: the leader's own group is all that can be found
        try:
            os.killpg(sid, 0)
            return {sid}
        except OSError:
            return set()
    members = {pid for pid in known if pid in table}
    for pid in table:
        try:
            if os.getsid(pid) == sid:
                members.add(pid)
        except OSError:
            pass
    if include_leader and sid in table:
        members.add(sid)
    children = {}
    for pid, ppid in table.items():
        children.setdefault(ppid, []).append(pid)
    todo = list(members)
    while todo:
        for kid in children.get(todo.pop(), ()):
            if kid not in members:
                members.add(kid)
                todo.append(kid)
    members.discard(os.getpid())
    members.discard(0)
    members.discard(1)
    return members


def _end_child(child, own_session, wait_secs=2.0, sleep=time.sleep, clock=time.monotonic,
               members=None):
    """SIGHUP (a tmux client detaches, a shell hangs up), then SIGTERM, then
    SIGKILL. With `own_session` (the child leads its own session), each step
    goes to every process the session started (_session_members: its
    process groups, the ones job control made included, and descendants)
    and waits until none is left, not only the child: one that ignores
    SIGHUP (nohup) gets SIGTERM, then SIGKILL. HUP and TERM are followed by
    SIGCONT, so a stopped process acts on them. Returns whether everything
    ended."""
    members = members or _session_members
    known = set()

    def left():
        if not own_session:
            return set()
        found = members(child.pid, include_leader=child.poll() is None, known=known)
        known.update(found)
        return found

    for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGKILL):
        targets = left() if own_session else None
        if own_session:
            for pid in targets:
                for each in ((sig,) if sig == signal.SIGKILL else (sig, signal.SIGCONT)):
                    try:
                        os.kill(pid, each)
                    except OSError:
                        pass
        else:
            try:
                child.send_signal(sig)
            except OSError:
                pass
        deadline = clock() + wait_secs
        while True:
            if child.poll() is not None and not left():
                return True
            if clock() >= deadline:
                break
            sleep(0.05)
    return False


class _PtyChild:
    """A child on a pty of its own, leading its own session (forkpty), so
    everything it starts stays findable by session id. poll() also sees a
    stop: with no job-control shell above it (zsh execs the command), Ctrl+Z
    would leave the window frozen, so a stopped child is continued."""

    def __init__(self, pid, master_fd):
        self.pid = pid
        self.master_fd = master_fd
        self.returncode = None

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        try:
            pid, status = os.waitpid(self.pid, os.WNOHANG | os.WUNTRACED)
        except ChildProcessError:
            self.returncode = 255
            return self.returncode
        if pid == 0:
            return None
        if os.WIFSTOPPED(status):
            diag.info("peer", "gate_child_stopped", signal=os.WSTOPSIG(status))
            for target in (lambda: os.killpg(self.pid, signal.SIGCONT),
                           lambda: os.kill(self.pid, signal.SIGCONT)):
                try:
                    target()
                    break
                except OSError:
                    pass
            return None
        self.returncode = os.waitstatus_to_exitcode(status)
        return self.returncode

    def send_signal(self, sig):
        os.kill(self.pid, sig)


def _spawn_on_pty(argv):
    pid, master_fd = os.forkpty()
    if pid == 0:  # the child: its own session, the new pty as its terminal
        for sig in (signal.SIGPIPE, signal.SIGXFSZ):  # as Popen's restore_signals
            signal.signal(sig, signal.SIG_DFL)
        try:
            os.execvp(argv[0], argv)
        except OSError as exc:
            os.write(2, f"agentboard: couldn't start {os.path.basename(argv[0])} "
                        f"({type(exc).__name__})\r\n".encode())
        os._exit(127)
    return _PtyChild(pid, master_fd)


class _Relay:
    """Copies bytes between the ssh session's terminal (`in_fd`/`out_fd`)
    and the child's own pty, with the window size following. The outer
    terminal is put in raw mode, so keys (Ctrl+C, Ctrl+Z) reach the child's
    pty, which turns them into signals there."""

    def __init__(self, in_fd, out_fd, master_fd):
        self.in_fd, self.out_fd, self.master_fd = in_fd, out_fd, master_fd
        self.to_child, self.to_out = b"", b""
        self.master_open, self.in_open = True, True
        self._saved_attrs = None
        self._saved_flags = {}
        self._winch = True
        self._previous_winch = None

    def __enter__(self):
        import termios
        import tty
        try:
            self._saved_attrs = termios.tcgetattr(self.in_fd)
            termios.tcsetattr(self.master_fd, termios.TCSANOW, self._saved_attrs)
            tty.setraw(self.in_fd, termios.TCSANOW)
        except (OSError, termios.error):
            pass
        for fd in {self.in_fd, self.out_fd, self.master_fd}:
            try:
                flags = fcntl_get(fd)
                self._saved_flags[fd] = flags
                fcntl_set(fd, flags | os.O_NONBLOCK)
            except OSError:
                pass
        self._previous_winch = signal.signal(signal.SIGWINCH, self._on_winch)
        return self

    def __exit__(self, *exc):
        import termios
        signal.signal(signal.SIGWINCH, self._previous_winch or signal.SIG_DFL)
        for fd, flags in self._saved_flags.items():
            try:
                fcntl_set(fd, flags)
            except OSError:
                pass
        if self._saved_attrs is not None:
            try:
                termios.tcsetattr(self.in_fd, termios.TCSADRAIN, self._saved_attrs)
            except (OSError, termios.error):
                pass
        return False

    def _on_winch(self, signum, frame):
        self._winch = True

    def _copy_winsize(self):
        import fcntl
        import termios
        self._winch = False
        try:
            size = fcntl.ioctl(self.in_fd, termios.TIOCGWINSZ, b"\0" * 8)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, size)
        except OSError:
            pass

    def pump(self, timeout, clock=time.monotonic):
        """Relay for up to `timeout` seconds. Returns "hangup" when the ssh
        side went away, "closed" when the child's pty closed, else None."""
        deadline = clock() + timeout
        while True:
            if self._winch:
                self._copy_winsize()
            remaining = deadline - clock()
            if remaining <= 0:
                return None
            rlist, wlist = [], []
            if self.in_open and not self.to_child and self.master_open:
                rlist.append(self.in_fd)
            if self.master_open and not self.to_out:
                rlist.append(self.master_fd)
            if self.to_child and self.master_open:
                wlist.append(self.master_fd)
            if self.to_out:
                wlist.append(self.out_fd)
            if not (rlist or wlist):
                time.sleep(min(remaining, 0.1))
                return "closed" if not self.master_open else None
            try:
                readable, writable, _ = select.select(rlist, wlist, [], min(remaining, 0.25))
            except (OSError, ValueError):
                return "hangup"
            if self.master_fd in readable:
                data = self._read(self.master_fd)
                if data is None:
                    self.master_open = False
                elif data:
                    self.to_out += data
            if self.in_fd in readable:
                data = self._read(self.in_fd)
                if data is None:
                    self.in_open = False
                    return "hangup"
                self.to_child += data
            if self.master_fd in writable and self.to_child:
                sent = self._write(self.master_fd, self.to_child)
                if sent is None:
                    self.to_child = b""
                else:
                    self.to_child = self.to_child[sent:]
            if self.out_fd in writable and self.to_out:
                sent = self._write(self.out_fd, self.to_out)
                if sent is None:
                    return "hangup"
                self.to_out = self.to_out[sent:]
            if not self.master_open and not self.to_out:
                return "closed"

    def drain(self, secs=1.0, clock=time.monotonic):
        """After the child exits: pass on what is left in its pty."""
        deadline = clock() + secs
        while clock() < deadline and (self.to_out or self.master_open):
            if self.master_open and not self.to_out:
                try:
                    ready, _, _ = select.select([self.master_fd], [], [], 0.05)
                except (OSError, ValueError):
                    return
                if not ready:
                    return
                data = self._read(self.master_fd)
                if not data:
                    self.master_open = False
                    continue
                self.to_out = data
            try:
                _, ready, _ = select.select([], [self.out_fd], [], 0.05)
            except (OSError, ValueError):
                return
            if ready:
                sent = self._write(self.out_fd, self.to_out)
                if sent is None:
                    return
                self.to_out = self.to_out[sent:]

    @staticmethod
    def _read(fd):
        """Bytes, b"" when nothing was ready, None at end of file or EIO."""
        try:
            data = os.read(fd, 65536)
        except BlockingIOError:
            return b""
        except OSError:
            return None
        return data or None

    @staticmethod
    def _write(fd, data):
        try:
            return os.write(fd, data[:65536])
        except BlockingIOError:
            return 0
        except OSError:
            return None


def fcntl_get(fd):
    import fcntl
    return fcntl.fcntl(fd, fcntl.F_GETFL)


def fcntl_set(fd, flags):
    import fcntl
    fcntl.fcntl(fd, fcntl.F_SETFL, flags)


def supervise(peer_id, argv, allowed=None, spawn=None, poll_secs=POLL_SECS, tty_fd=None,
              out_fd=1):
    """Run `argv` (tmux attach, or the resume shell) as a child instead of
    exec'ing it, and end it as soon as the peer is removed or loses "allow
    opening": sshd keeps an open session after its authorized_keys line
    goes, so without this a removed Mac would keep its shell.

    The child leads a session of its own: on a pty of its own when `tty_fd`
    is a terminal (relayed to and from the ssh session's terminal), else
    with start_new_session. Whatever process groups job control (`-i`)
    gives the commands it runs, they stay in that session, so ending it
    (_end_child) reaches everything it started. That also happens when the
    child exits by itself, so nothing it left behind outlives the window.
    Returns the child's exit code, or EXIT_REFUSED when ended."""
    allowed = allowed or (lambda: open_allowed(peer_id))
    own_session = spawn is None
    if spawn is None:
        if tty_fd is not None:
            spawn = _spawn_on_pty
        else:
            spawn = lambda args: subprocess.Popen(args, start_new_session=True)
    stop = {"signal": None}

    def on_signal(signum, frame):
        stop["signal"] = signum

    handled = (signal.SIGHUP, signal.SIGTERM)
    previous = {}
    if own_session:
        previous = {sig: signal.signal(sig, on_signal) for sig in handled}
    try:
        # Checked once before starting, not only a poll interval later.
        if not allowed():
            return _refuse(peer_id, "opening sessions is not allowed for this machine")
        try:
            child = spawn(argv)
        except OSError as exc:
            return _refuse(peer_id, f"couldn't start {os.path.basename(argv[0])} ({type(exc).__name__})")
        master_fd = getattr(child, "master_fd", None)
        relay = _Relay(tty_fd, out_fd, master_fd) if master_fd is not None else None
        try:
            if relay:
                relay.__enter__()
            while True:
                rc = child.poll()
                if rc is None:
                    if relay:
                        if relay.pump(poll_secs) == "hangup":
                            stop["signal"] = stop["signal"] or signal.SIGHUP
                        rc = child.poll()
                    else:
                        try:
                            rc = child.wait(timeout=poll_secs)
                        except subprocess.TimeoutExpired:
                            rc = None
                if rc is not None:
                    if relay:
                        relay.drain()
                    if own_session:
                        _end_child(child, True, wait_secs=0.5)  # what it left behind
                    return rc if rc >= 0 else 128 - rc
                reason = ("signal" if stop["signal"] is not None
                          else None if allowed() else "not allowed")
                if reason:
                    diag.info("peer", "gate_open_ended", peer=peer_id, reason=reason)
                    _end_child(child, own_session)
                    if relay:
                        relay.drain(0.2)
                        relay.__exit__(None, None, None)
                        relay = None
                    if reason == "not allowed" and tty_fd is not None:
                        print("\r\nagentboard: this Mac no longer allows opening sessions "
                              "from yours; disconnected.\r", file=sys.stderr)
                    return EXIT_REFUSED
        finally:
            if relay:
                relay.__exit__(None, None, None)
            if master_fd is not None:
                try:
                    os.close(master_fd)
                except OSError:
                    pass
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _attach(peer_id, row, launch):
    from . import cli
    socket_path, pane = row["tmux_socket"], row["tmux_pane"]
    if not (socket_path and pane):
        return _refuse(peer_id, "session is not in tmux", "attach")
    binary = cli._tmux_binary()
    if binary is None:
        return _refuse(peer_id, "tmux not found", "attach")
    result = cli._run_tmux(subprocess.run, binary, socket_path,
                           ["display-message", "-p", "-t", pane, cli.TMUX_DISPLAY_FORMAT])
    target = None if isinstance(result, cli._TimedOut) else cli._parse_tmux_display(result)
    if target is None:
        return _refuse(peer_id, "tmux pane is gone", "attach")
    session_id, _name, window_id = target
    for tmux_args in (["select-window", "-t", window_id], ["select-pane", "-t", pane]):
        cli._run_tmux(subprocess.run, binary, socket_path, tmux_args)
    diag.info("peer", "gate_attach", peer=peer_id, tmux_session_id=session_id)
    # Shared attach (never -d): whoever is attached on this Mac stays attached.
    # -u: ssh runs with -F none, so no LANG/LC_* arrives, and tmux without
    # a UTF-8 locale draws every non-ASCII character as "_".
    argv = [binary, "-u", "-S", socket_path, "attach-session", "-t", session_id]
    rc = launch(peer_id, argv)
    return 0 if rc is None else rc


def _resume(peer_id, parsed, row, launch):
    from . import cli
    command = cli._resume_command(parsed["tool"], row["cwd"], parsed["session_id"])
    if not command:
        return _refuse(peer_id, "no resume command", "resume")
    shell = pwd.getpwuid(os.getuid()).pw_shell or "/bin/zsh"
    diag.info("peer", "gate_resume", peer=peer_id, key=f"{parsed['tool']}:{parsed['session_id']}")
    # The command is built here from the database's cwd (shell-quoted) and
    # a session id that matched _SESSION_RE; nothing from the client.
    # Interactive as well as login (the session has a pty), so the user's
    # ~/.zshrc or ~/.bashrc runs and a PATH set there finds claude/codex.
    rc = launch(peer_id, [shell, "-l", "-i", "-c", command])
    return 0 if rc is None else rc


def _focus(parsed):
    from . import cli
    args = argparse.Namespace(tool=parsed["tool"], session_id=parsed["session_id"])
    return cli.cmd_focus(args)


def _default_launch(peer_id, argv, public_key=_UNSET):
    tty_fd = 0 if os.isatty(0) else None
    return supervise(peer_id, argv, tty_fd=tty_fd,
                     allowed=lambda: open_allowed(peer_id, public_key=public_key))


def run(peer_id, command=None, launch=None, out=None, stdin=None, entry_for=None):
    """`launch(peer_id, argv)` runs attach's tmux or resume's shell
    (default: `supervise`) and returns its exit code."""
    command = os.environ.get("SSH_ORIGINAL_COMMAND") if command is None else command
    if not peers.valid_id(peer_id):
        return _refuse(peer_id, "bad peer id")
    peer = peers.find_peer(peers.load_config(), peer_id)
    if peer is None:
        return _refuse(peer_id, "peer not paired")
    if launch is None:
        key = peer.get("public_key")
        launch = lambda pid, argv: _default_launch(pid, argv, public_key=key)
    try:
        verb, parsed = parse_command(command)
    except Refused as exc:
        return _refuse(peer_id, str(exc))
    if verb == "serve":
        return serve(peer, out=out, stdin=stdin)
    if verb in OPEN_VERBS and not peer["allow_open"]:
        return _refuse(peer_id, "opening sessions is not allowed for this machine", verb)
    row = _session_row(parsed["tool"], parsed["session_id"])
    if row is None:
        return _refuse(peer_id, "unknown session", verb)
    if verb == "mark":
        diag.info("peer", "gate_mark", peer=peer_id, action=parsed["action"],
                  key=f"{parsed['tool']}:{parsed['session_id']}")
        return _mark(parsed)
    if verb == "focus":
        return _focus(parsed)
    entry = (entry_for or _local_entry)(parsed["tool"], parsed["session_id"])
    how = peers.remote_open_for(entry or {}, True)
    if verb == "attach":
        if how != "tmux":
            return _refuse(peer_id, "session is not live in tmux", verb)
        return _attach(peer_id, row, launch)
    if how != "resume":
        return _refuse(peer_id, "session is still running; attach instead", verb)
    return _resume(peer_id, parsed, row, launch)
