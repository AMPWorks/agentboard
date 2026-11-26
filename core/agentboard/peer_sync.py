"""`agentboard peer-sync`: pull every paired Mac's export over ssh.

One ssh child per configured peer runs that peer's `peer-gate serve` and
streams one JSON line per change. Each line is validated at this trust
boundary (peers.validate_line) and written atomically to
`peers/<machine_id>.json` with the receive time, followed by a rewrite of
`peers.stamp` so the app's directory watcher sees the change. A child that
exits is restarted with backoff (2 s doubling to 60 s). config.json is
re-read when it changes, a lock keeps it to one instance, and it exits when
its parent (the app) goes away or no peers are left.

This and `refresh-links` are the only long-lived network users; `snapshot`
only ever reads the files written here.
"""

import collections
import fcntl
import os
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone

from . import db
from . import diag
from . import peers

BACKOFF_START_SECS = 2
BACKOFF_MAX_SECS = 60
LOOP_SECS = 0.5
STDERR_TAIL = 300
EXIT_ALREADY_RUNNING = 3
# A peer's gate sends at most one line a second; handling no more than
# that bounds what a misbehaving peer costs here (each line is a cache
# write, an fsync and an app refresh). Each line is a whole export, so
# lines that arrive faster are not queued: only the newest one waiting is
# kept (see _Latest), and the board is never behind the peer by a backlog.
MIN_LINE_SECS = 1.0
TOO_LARGE = object()  # stands in for an oversized line in _Latest


class _Latest:
    """A one-slot mailbox between a connection's reader and its handler:
    `put` replaces whatever is waiting, so memory and delay stay bounded
    however fast the peer writes. `keep_all` (min_line_secs=0, tests only)
    queues every line in order instead."""

    def __init__(self, keep_all=False):
        self.cond = threading.Condition()
        self.items = collections.deque()
        self.keep_all = keep_all
        self.eof = False
        self.replaced = 0

    def put(self, item):
        with self.cond:
            if self.items and not self.keep_all:
                self.items.clear()
                self.replaced += 1
            self.items.append(item)
            self.cond.notify()

    def close(self):
        with self.cond:
            self.eof = True
            self.cond.notify()

    def take(self):
        """The next waiting item, or None once the reader is done and
        nothing is left."""
        with self.cond:
            while not self.items and not self.eof:
                self.cond.wait()
            return self.items.popleft() if self.items else None

    def newer(self, item):
        """`item`, or the line that replaced it while the handler waited."""
        with self.cond:
            if self.items and not self.keep_all:
                self.replaced += 1
                return self.items.popleft()
        return item


_SSH_ERRORS = (
    ("Host key verification failed", "its ssh host key does not match the pinned one; remove and pair it again"),
    ("REMOTE HOST IDENTIFICATION HAS CHANGED", "its ssh host key does not match the pinned one; remove and pair it again"),
    ("Permission denied", "this Mac is not authorized there; add this Mac's pairing code on it"),
    ("Could not resolve hostname", "its host name can't be found; edit the host"),
    ("Connection refused", "Remote Login is off there"),
    ("Operation timed out", "not reachable (asleep, or on another network)"),
    ("Connection timed out", "not reachable (asleep, or on another network)"),
    ("No route to host", "not reachable (asleep, or on another network)"),
    ("refused:", "it refused the connection (paired there? see its log)"),
)


def _friendly_error(stderr_text):
    text = (stderr_text or "").strip()
    for needle, message in _SSH_ERRORS:
        if needle in text:
            return message
    return diag.tail_text(text, 200) or "the connection closed"


def default_spawner(peer, argv):
    return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)


class Link:
    """One peer's connection state."""

    def __init__(self, peer):
        self.peer = peer
        self.proc = None
        self.reader = None
        self.backoff = BACKOFF_START_SECS
        self.next_attempt = 0.0
        self.lines = 0
        self.connection_lines = 0
        # Set by PeerSync._stop: the peer was removed or changed, so a line
        # its reader thread is still handling must not write the cache.
        self.closed = False
        # Bumped (under lock) when a connection is reaped or a new one
        # starts: a handler still working through an old connection's last
        # line then writes nothing, so it never interleaves "online" and old
        # sessions with the next connection's lines.
        self.generation = 0
        self.handler = None
        self.last_stderr = ""
        self.lock = threading.Lock()

    def identity(self):
        # The keys too: a peer paired again with new keys gets a new connection.
        return (self.peer["host"], self.peer["user"], self.peer["allow_open"],
                self.peer.get("public_key"), self.peer.get("host_key"))


class PeerSync:
    def __init__(self, home=None, spawner=None, clock=time.monotonic,
                 wall=lambda: datetime.now(timezone.utc), ssh_argv=None, once=False,
                 min_line_secs=MIN_LINE_SECS, line_sleep=time.sleep):
        self.home = home or db.home_dir()
        self.min_line_secs = min_line_secs
        self.line_sleep = line_sleep
        self.spawner = spawner or default_spawner
        self.clock = clock
        self.wall = wall
        self.once = once
        self.links = {}
        self.config_sig = None
        self.done_once = set()
        if ssh_argv is None:
            from . import peer_cli
            ssh_argv = lambda peer: peer_cli.ssh_argv(peer, "serve", home=self.home)
        self.ssh_argv = ssh_argv

    # -- config --------------------------------------------------------------

    def _config_signature(self):
        try:
            st = os.stat(peers.config_file(self.home))
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def reload(self, force=False):
        sig = self._config_signature()
        if not force and sig == self.config_sig:
            return
        self.config_sig = sig
        wanted = {p["id"]: p for p in peers.configured_peers(peers.load_config(self.home))}
        for peer_id in list(self.links):
            link = self.links[peer_id]
            if peer_id not in wanted or Link(wanted[peer_id]).identity() != link.identity():
                self._stop(link)
                del self.links[peer_id]
                if peer_id not in wanted:
                    # Removed (or replaced under another id): remove_peer
                    # deleted the cache, but a line handled before this
                    # reload may have written it again. _stop closed the
                    # link under its lock, so nothing writes it after this.
                    try:
                        peers.cache_path(peer_id, self.home).unlink()
                    except OSError:
                        pass
        for peer_id, peer in wanted.items():
            if peer_id in self.links:
                self.links[peer_id].peer = peer
            else:
                self.links[peer_id] = Link(peer)
        diag.debug("peer_sync", "config", peers=len(self.links))

    # -- cache writes ----------------------------------------------------------

    def _still_paired(self, link):
        """The link's Mac is still in config.json with the same key, checked
        at write time: a Mac removed in the half second before the next
        reload gets no cache file back."""
        try:
            current = peers.find_peer(peers.load_config(self.home), link.peer["id"])
        except Exception:
            return False
        return current is not None and current.get("public_key") == link.peer.get("public_key")

    def _write(self, link, generation=None, **changes):
        peer_id = link.peer["id"]
        with link.lock:
            if link.closed or (generation is not None and generation != link.generation):
                return
            if not self._still_paired(link):
                return
            cache = peers.read_cache(peer_id, self.home) or {
                "peer_id": peer_id, "sessions": [], "received_at": None, "last_seen_at": None,
            }
            cache.update(changes)
            try:
                peers.write_cache(peer_id, cache, self.home, now=self.wall())
            except OSError as exc:
                diag.warn("peer_sync", "cache_write_failed", peer=peer_id, exc=type(exc).__name__)

    def _rejected(self, link, reason, sessions=None, generation=None):
        """Record why lines are rejected where Settings shows it. Status and
        last_seen_at stay as they were, so the cached sessions (no longer
        current) turn stale and the peer offline as for a silent peer, with
        this error instead of ssh's."""
        error = peers.rejection_error(reason, sessions)
        if reason == "incompatible":
            self._write(link, generation, status="incompatible", error=error)
        else:
            self._write(link, generation, error=error)

    def handle_line(self, link, raw, generation=None):
        """Validate one received line and cache it. Bad lines are dropped
        (logged); the connection stays up. With `generation`, nothing is
        written once that connection has been superseded."""
        peer_id = link.peer["id"]
        try:
            data = peers.validate_line(raw)
        except peers.LineRejected as exc:
            reason = str(exc)
            diag.warn("peer_sync", "line_rejected", peer=peer_id, reason=reason, bytes=len(raw))
            self._rejected(link, reason, exc.sessions, generation)
            return False
        if data["machine"]["id"] != peer_id:
            diag.warn("peer_sync", "line_rejected", peer=peer_id, reason="machine mismatch")
            self._rejected(link, "machine mismatch", generation=generation)
            return False
        now = self.wall()
        generated = peers.parse_ts(data["generated_at"])
        skew = round((generated - now).total_seconds(), 1) if generated else None
        stamp = peers.now_iso(now)
        self._write(link, generation, status="online", error=None, received_at=stamp, last_seen_at=stamp,
                    generated_at=data["generated_at"], clock_skew_secs=skew,
                    machine=data["machine"], share=data["share"], sessions=data["sessions"])
        link.lines += 1
        link.connection_lines += 1
        if self.once:
            self.done_once.add(peer_id)
        return True

    # -- children ---------------------------------------------------------------

    def _read(self, link, proc, box):
        """Drain the child's stdout as fast as it writes, keeping only the
        newest complete line (or an oversized-line marker) for the handler."""
        stream = proc.stdout
        try:
            while True:
                raw = stream.readline(peers.MAX_LINE_BYTES + 1)
                if not raw:
                    break
                if len(raw) > peers.MAX_LINE_BYTES and not raw.endswith(b"\n"):
                    # Skip the rest of an oversized line.
                    while True:
                        chunk = stream.readline(1024 * 1024)
                        if not chunk or chunk.endswith(b"\n"):
                            break
                    box.put(TOO_LARGE)
                    continue
                box.put(raw.rstrip(b"\n"))
        except (OSError, ValueError) as exc:
            diag.debug("peer_sync", "read_failed", peer=link.peer["id"], exc=type(exc).__name__)
        finally:
            box.close()
        try:
            err = proc.stderr.read() if proc.stderr else b""
        except (OSError, ValueError):
            err = b""
        link.last_stderr = err.decode("utf-8", "replace") if isinstance(err, bytes) else str(err or "")

    def _handle(self, link, box, generation):
        """Handle at most one line per min_line_secs, rejections included;
        stops as soon as this connection is superseded."""
        last = None
        while True:
            item = box.take()
            if item is None or link.closed or generation != link.generation:
                return
            if self.min_line_secs and last is not None:
                elapsed = time.monotonic() - last
                if elapsed < self.min_line_secs:
                    self.line_sleep(self.min_line_secs - elapsed)
                    item = box.newer(item)  # a newer line may have come meanwhile
                if link.closed or generation != link.generation:
                    return
            last = time.monotonic()
            if box.replaced:
                diag.debug("peer_sync", "lines_superseded", peer=link.peer["id"], count=box.replaced)
                box.replaced = 0
            if item is TOO_LARGE:
                diag.warn("peer_sync", "line_rejected", peer=link.peer["id"], reason="too large")
                self._rejected(link, "too large", generation=generation)
                continue
            try:
                self.handle_line(link, item, generation)
            except Exception as exc:  # a handler must outlive any one bad line
                diag.warn("peer_sync", "line_failed", peer=link.peer["id"],
                          exc=type(exc).__name__)
                self._rejected(link, "invalid", generation=generation)

    def _start(self, link):
        from . import peer_cli
        try:
            argv = self.ssh_argv(link.peer)
        except peer_cli.PeerUnavailable as exc:
            self._failed(link, str(exc))
            return
        link.connection_lines = 0
        link.last_stderr = ""
        try:
            proc = self.spawner(link.peer, argv)
        except OSError as exc:
            self._failed(link, f"couldn't start ssh ({type(exc).__name__})")
            return
        with link.lock:
            link.generation += 1
            generation = link.generation
        box = _Latest(keep_all=not self.min_line_secs)
        link.proc = proc
        link.reader = threading.Thread(target=self._read, args=(link, proc, box), daemon=True)
        link.handler = threading.Thread(target=self._handle, args=(link, box, generation), daemon=True)
        link.reader.start()
        link.handler.start()
        diag.debug("peer_sync", "connect", peer=link.peer["id"], host=link.peer["host"])

    def _failed(self, link, error):
        link.proc = None
        link.reader = None
        link.handler = None
        if link.connection_lines:
            link.backoff = BACKOFF_START_SECS
        delay = link.backoff
        link.next_attempt = self.clock() + delay
        link.backoff = min(link.backoff * 2, BACKOFF_MAX_SECS)
        cache = peers.read_cache(link.peer["id"], self.home) or {}
        if cache.get("status") != "incompatible":
            self._write(link, status="offline", error=error)
        diag.debug("peer_sync", "disconnected", peer=link.peer["id"], retry_in=delay, error=error)
        if self.once:
            self.done_once.add(link.peer["id"])

    def _reap(self, link):
        proc = link.proc
        if proc is None or proc.poll() is None:
            return
        for thread in (link.reader, link.handler):
            if thread is not None:
                thread.join(timeout=2)
        with link.lock:
            link.generation += 1  # whatever the old handler still holds is stale now
        rc = proc.poll()
        error = None if rc == 0 and link.connection_lines else _friendly_error(link.last_stderr)
        if error is None:
            error = "the connection closed"
        diag.debug("peer_sync", "exit", peer=link.peer["id"], rc=rc,
                   stderr=diag.tail_text(link.last_stderr, STDERR_TAIL) or None)
        self._failed(link, error)

    def _stop(self, link):
        with link.lock:
            link.closed = True
        proc = link.proc
        link.proc = None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def tick(self):
        self.reload()
        now = self.clock()
        for link in list(self.links.values()):
            if link.proc is not None:
                self._reap(link)
            if link.proc is None and now >= link.next_attempt:
                if self.once and link.peer["id"] in self.done_once:
                    continue
                self._start(link)

    def stop_all(self):
        for link in self.links.values():
            self._stop(link)

    def finished_once(self):
        return self.once and self.links and set(self.links) <= self.done_once


def _acquire_lock(home):
    path = home / peers.LOCK_FILE
    home.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def run(once=False, spawner=None, sleep=time.sleep, parent_alive=None, max_loops=None):
    home = db.home_dir()
    fd = _acquire_lock(home)
    if fd is None:
        diag.debug("peer_sync", "already_running")
        return EXIT_ALREADY_RUNNING
    if parent_alive is None:
        start_parent = os.getppid()
        parent_alive = lambda: os.getppid() == start_parent
    sync = PeerSync(home, spawner=spawner, once=once)

    def on_term(signum, frame):
        raise SystemExit(0)

    try:
        signal.signal(signal.SIGTERM, on_term)
    except ValueError:
        pass  # not the main thread (tests)
    diag.info("peer_sync", "start", once=once)
    loops = 0
    reason = "unknown"
    try:
        while True:
            sync.tick()
            if not sync.links:
                reason = "no_peers"
                break
            if sync.finished_once():
                reason = "once"
                break
            if not parent_alive():
                reason = "orphaned"
                break
            loops += 1
            if max_loops is not None and loops >= max_loops:
                reason = "max_loops"
                break
            sleep(LOOP_SECS)
    finally:
        sync.stop_all()
        diag.info("peer_sync", "stop", reason=reason)
        os.close(fd)
    return 0
