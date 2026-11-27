"""Peer sync between the user's own Macs: identity, config, pairing, export, merge.

Each install has a random machine id (`machine.json`) and an ed25519 key
pair used for nothing but AgentBoard (`peer-keys/`). Pairing exchanges a
code that holds only public data; adding one pins the peer's ssh host key
(`peer-keys/known_hosts`, under `HostKeyAlias=agentboard-<id>`), authorizes
the peer's key in `~/.ssh/authorized_keys` behind a forced command
(`agentboard peer-gate <id>`), and writes the peer to `config.json`.

`peer-sync` (peer_sync.py) pulls each peer's export over ssh into
`peers/<id>.json`; `build_snapshot` merges those files with `merge_into`,
reading local files only. The export (`build_export`) holds this Mac's own
sessions only, filtered by `peer_share`, so nothing is relayed or loops.
"""

import base64
import binascii
import contextlib
import fcntl
import hashlib
import json
import os
import platform
import re
import shlex
import socket
import subprocess
import tempfile
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db
from . import diag

PROTOCOL_VERSION = 1
MACHINE_FILE = "machine.json"
KEYS_DIR = "peer-keys"
KEY_FILE = "id_ed25519"
KNOWN_HOSTS_FILE = "known_hosts"
PEERS_DIR = "peers"
STAMP_FILE = "peers.stamp"
LOCK_FILE = "peer-sync.lock"

AUTH_MARKER = "agentboard-peer:"
CODE_PREFIX = "agentboard-pair:"
MAX_CODE_CHARS = 4096
HOST_KEY_PATH = Path("/etc/ssh/ssh_host_ed25519_key.pub")

SHARE_LEVELS = ("full", "titles", "state")
DEFAULT_SHARE = "full"
DEFAULT_STALE_DAYS = 7
# No line (export or heartbeat) for this long: the peer is offline and its
# sessions are shown stale. The serving side sends a heartbeat every 15 s.
OFFLINE_AFTER_SECS = 45
HEARTBEAT_SECS = 15

COMMAND_TIMEOUT_SECS = 5
REMOTE_LOGIN_PORT = 22

# Exit code of focus/resume/mark --machine when the peer is offline or
# unknown, distinct from focus's 1/4/5.
EXIT_PEER_UNAVAILABLE = 6

# Every pattern is anchored with \A and \Z, never ^ and $: `$` also matches
# before a trailing newline, which would let "value\n" through.
_UUID_RE = re.compile(r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_USER_RE = re.compile(r"\A[A-Za-z0-9_][A-Za-z0-9._-]{0,63}\Z")
# First character alphanumeric, so a host can never read as an ssh option.
_HOST_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9.:%_-]{0,252}\Z")
_ED25519_PREFIX = "AAAAC3NzaC1lZDI1NTE5"
_KEY_RE = re.compile(r"\Assh-ed25519 (" + _ED25519_PREFIX + r"[A-Za-z0-9+/]{1,200}={0,2})\Z")
MAX_NAME_CHARS = 64


class PeerError(Exception):
    """A user-facing failure (bad code, unknown peer, malformed config)."""


class MalformedConfigError(PeerError):
    pass


# -- small helpers ----------------------------------------------------------------

def _home(home=None) -> Path:
    return Path(home) if home is not None else db.home_dir()


def now_iso(now=None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_ts(value):
    # ASCII only: fromisoformat takes any character as the date/time
    # separator, a lone surrogate included, which the app can't decode.
    if not isinstance(value, str) or not value or not value.isascii():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _atomic_write(path: Path, data: bytes, mode=0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def clean_name(value) -> str | None:
    """A display name: a string of 1..64 printable characters, else None."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > MAX_NAME_CHARS:
        return None
    if any(unicodedata.category(ch)[0] == "C" for ch in value):
        return None
    return value


def valid_id(value) -> bool:
    return isinstance(value, str) and bool(_UUID_RE.match(value))


def valid_host(value) -> bool:
    return isinstance(value, str) and bool(_HOST_RE.match(value))


def same_host(a, b) -> bool:
    """True when two addresses name the same host: DNS names ignore case and
    a trailing dot (the `.local` name keeps LocalHostName's case)."""
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    return a.rstrip(".").lower() == b.rstrip(".").lower()


def valid_user(value) -> bool:
    return isinstance(value, str) and bool(_USER_RE.match(value))


def valid_key(value) -> bool:
    if not isinstance(value, str):
        return False
    match = _KEY_RE.match(value)
    if not match:
        return False
    try:
        blob = base64.b64decode(match.group(1), validate=True)
    except (binascii.Error, ValueError):
        return False
    # string "ssh-ed25519" (4+11) + string key (4+32)
    return len(blob) == 51


def _run(run, argv, timeout=COMMAND_TIMEOUT_SECS):
    try:
        return run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def _scutil(run, key):
    result = _run(run, ["/usr/sbin/scutil", "--get", key], timeout=2)
    if result is None or getattr(result, "returncode", 1) != 0:
        return None
    return (result.stdout or "").strip() or None


# -- identity ---------------------------------------------------------------------

def read_machine(home=None) -> dict | None:
    """This Mac's {id, name} from machine.json, or None (never creates it)."""
    path = _home(home) / MACHINE_FILE
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not valid_id(data.get("id")):
        return None
    return {"id": data["id"], "name": clean_name(data.get("name")) or "Mac"}


def default_machine_name(run=subprocess.run) -> str:
    name = clean_name(_scutil(run, "ComputerName"))
    if name:
        return name
    node = (platform.node() or "").split(".")[0]
    return clean_name(node) or "Mac"


def ensure_machine(home=None, run=subprocess.run) -> dict:
    """machine.json's identity, created on first use with a random id."""
    existing = read_machine(home)
    if existing:
        return existing
    machine = {"id": str(uuid.uuid4()), "name": default_machine_name(run)}
    _atomic_write(_home(home) / MACHINE_FILE, (json.dumps(machine, indent=2) + "\n").encode(), 0o644)
    diag.info("peers", "machine_created", id=machine["id"])
    return machine


def set_machine_name(name, home=None, run=subprocess.run) -> dict:
    clean = clean_name(name)
    if not clean:
        raise PeerError(f"invalid machine name: {name!r}")
    machine = ensure_machine(home, run)
    machine["name"] = clean
    _atomic_write(_home(home) / MACHINE_FILE, (json.dumps(machine, indent=2) + "\n").encode(), 0o644)
    return machine


def keys_dir(home=None) -> Path:
    return _home(home) / KEYS_DIR


def key_path(home=None) -> Path:
    return keys_dir(home) / KEY_FILE


def known_hosts_path(home=None) -> Path:
    return keys_dir(home) / KNOWN_HOSTS_FILE


def ensure_keypair(home=None, run=subprocess.run) -> str:
    """AgentBoard's own ed25519 public key, generating the pair on first use
    (ssh-keygen; both files 0600 in a 0700 directory)."""
    private = key_path(home)
    public = private.with_suffix(".pub")
    if not (private.is_file() and public.is_file()):
        directory = keys_dir(home)
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        for path in (private, public):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        machine = ensure_machine(home, run)
        result = _run(run, ["ssh-keygen", "-q", "-t", "ed25519", "-N", "",
                            "-C", f"agentboard-{machine['id']}", "-f", str(private)], timeout=20)
        if result is None or getattr(result, "returncode", 1) != 0 or not public.is_file():
            raise PeerError("couldn't generate AgentBoard's ssh key (ssh-keygen failed)")
        diag.info("peers", "key_created")
    for path in (private, public):
        os.chmod(path, 0o600)
    key = " ".join(public.read_text().split()[:2])
    if not valid_key(key):
        raise PeerError(f"{public} does not hold an ed25519 public key")
    return key


def read_host_key(path=HOST_KEY_PATH) -> str | None:
    try:
        parts = Path(path).read_text().split()
    except OSError:
        return None
    key = " ".join(parts[:2])
    return key if valid_key(key) else None


def login_name() -> str:
    import pwd
    return pwd.getpwuid(os.getuid()).pw_name


def suggested_host(run=subprocess.run) -> str:
    """The `.local` name (peer_address.local_name)."""
    from . import peer_address
    return peer_address.local_name(run)


# -- config -----------------------------------------------------------------------

def config_file(home=None) -> Path:
    return _home(home) / "config.json"


def load_config(home=None) -> dict:
    path = config_file(home)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def update_config(mutate, home=None) -> dict:
    """Read config.json, apply `mutate(dict)` and write it back atomically,
    keeping every other key. A file that is not a JSON object is refused
    (MalformedConfigError) rather than overwritten. The read-modify-write
    holds a lock on the data directory, so two changes at once (Settings,
    a CLI call) never lose one."""
    path = config_file(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _locked_dir(path.parent):
        return _update_config_locked(path, mutate)


def _update_config_locked(path, mutate) -> dict:
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise MalformedConfigError(f"{path} is not valid JSON ({exc}); fix or remove it first")
        if not isinstance(data, dict):
            raise MalformedConfigError(f"{path} is not a JSON object; fix or remove it first")
    mode = 0o644
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        pass
    mutate(data)
    _atomic_write(path, (json.dumps(data, indent=2) + "\n").encode(), mode)
    return data


def advertised_address(config, run=subprocess.run, candidates=None) -> dict:
    """The address this Mac's pairing code and export carry, as
    {address, source, kind}: config.json's `peer_address` when it is a
    valid host (no detection runs then), else the automatic choice from
    `candidates` (detected when not given)."""
    value = config.get("peer_address")
    if value is not None:
        if valid_host(value):
            return {"address": value, "source": "config", "kind": "custom"}
        diag.debug("peers", "peer_address_invalid", value=str(value)[:80])
    from . import peer_address
    if candidates is None:
        candidates = peer_address.candidates(run)
    return {**peer_address.automatic(candidates), "source": "auto"}


def set_address(value, home=None) -> None:
    """Save the address other Macs reach this one at (`peer_address`);
    None goes back to choosing it automatically."""
    if value is not None:
        value = value.strip() if isinstance(value, str) else value
        if not valid_host(value):
            raise PeerError(f"invalid address: {value!r} (use a host name or IP address, "
                            "starting with a letter or digit)")

    def mutate(data):
        if value is None:
            data.pop("peer_address", None)
        else:
            data["peer_address"] = value

    update_config(mutate, home)
    touch_stamp(home)


def share_level(config) -> str:
    value = config.get("peer_share")
    return value if value in SHARE_LEVELS else DEFAULT_SHARE


def stale_days(config) -> int:
    value = config.get("peer_stale_days")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return DEFAULT_STALE_DAYS


def _clean_peer(entry) -> dict | None:
    if not isinstance(entry, dict):
        return None
    peer_id, host, user = entry.get("id"), entry.get("host"), entry.get("user")
    if not (valid_id(peer_id) and valid_host(host) and valid_user(user)):
        return None
    public_key = entry.get("public_key")
    host_key = entry.get("host_key")
    return {
        "id": peer_id,
        "name": clean_name(entry.get("name")) or host,
        "host": host,
        "user": user,
        # Missing means on (the default); anything but true otherwise is off,
        # so a hand-edited "off" or 0 never allows opening.
        "allow_open": entry.get("allow_open", True) is True,
        "public_key": public_key if valid_key(public_key) else None,
        "host_key": host_key if valid_key(host_key) else None,
        "fingerprint": fp if isinstance(fp := entry.get("fingerprint"), str)
        and _FINGERPRINT_RE.match(fp) else None,
        "dismissed_address": dismissed if valid_host(dismissed := entry.get("dismissed_address"))
        else None,
    }


def configured_peers(config) -> list:
    """The valid entries of config.json's `peers`, in order; invalid ones and
    repeated ids are skipped."""
    raw = config.get("peers")
    if not isinstance(raw, list):
        return []
    peers, seen = [], set()
    for entry in raw:
        peer = _clean_peer(entry)
        if peer and peer["id"] not in seen:
            seen.add(peer["id"])
            peers.append(peer)
    return peers


def find_peer(config, peer_id) -> dict | None:
    return next((p for p in configured_peers(config) if p["id"] == peer_id), None)


# -- pairing code -----------------------------------------------------------------

FINGERPRINT_GROUPS = 6
_FINGERPRINT_RE = re.compile(r"\A[0-9A-F]{4}(-[0-9A-F]{4}){%d}\Z" % (FINGERPRINT_GROUPS - 1))
_CODE_FIELDS = ("id", "name", "user", "host", "key", "host_key")


def fingerprint(payload) -> str:
    """96 bits (six groups of four hex digits) of SHA-256 over every field of
    a pairing code's payload, so no part of the code (host and login name
    included) can change without the fingerprint changing."""
    canonical = json.dumps({k: payload.get(k) for k in _CODE_FIELDS}, sort_keys=True,
                           separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(canonical.encode()).hexdigest().upper()
    return "-".join(digest[i:i + 4] for i in range(0, 4 * FINGERPRINT_GROUPS, 4))


def build_code(home=None, run=subprocess.run, host_key_path=HOST_KEY_PATH, address=None) -> dict:
    """This Mac's pairing code and the parts shown next to it. Its host is
    `address`, else advertised_address()'s."""
    if address is not None and not valid_host(address):
        raise PeerError(f"invalid address: {address!r}")
    machine = ensure_machine(home, run)
    public_key = ensure_keypair(home, run)
    host_key = read_host_key(host_key_path)
    if host_key is None:
        raise PeerError(f"couldn't read this Mac's ssh host key ({host_key_path}); "
                        "turn on Remote Login first")
    payload = {
        "v": PROTOCOL_VERSION,
        "id": machine["id"],
        "name": machine["name"],
        "user": login_name(),
        "host": address or advertised_address(load_config(home), run)["address"],
        "key": public_key,
        "host_key": host_key,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    code = CODE_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return {"code": code, "fingerprint": fingerprint(payload),
            **{k: payload[k] for k in ("id", "name", "user", "host")}}


def parse_code(code) -> dict:
    """The validated payload of a pairing code; PeerError on anything off."""
    if not isinstance(code, str):
        raise PeerError("the pairing code is not text")
    code = "".join(code.split())
    if len(code) > MAX_CODE_CHARS:
        raise PeerError("the pairing code is too long")
    if not code.startswith(CODE_PREFIX):
        raise PeerError("this is not an AgentBoard pairing code")
    body = code[len(CODE_PREFIX):]
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        payload = json.loads(raw.decode())
    except (binascii.Error, ValueError, UnicodeDecodeError, RecursionError):
        raise PeerError("the pairing code is damaged (copy all of it)")
    if not isinstance(payload, dict):
        raise PeerError("the pairing code is damaged (copy all of it)")
    if payload.get("v") != PROTOCOL_VERSION:
        raise PeerError("the pairing code is from an incompatible AgentBoard version")
    expected = {"v", "id", "name", "user", "host", "key", "host_key"}
    if set(payload) != expected:
        raise PeerError("the pairing code has unexpected fields")
    name = clean_name(payload["name"])
    checks = (
        (valid_id(payload["id"]), "machine id"),
        (name is not None, "name"),
        (valid_user(payload["user"]), "login name"),
        (valid_host(payload["host"]), "host"),
        (valid_key(payload["key"]), "key"),
        (valid_key(payload["host_key"]), "host key"),
    )
    for ok, what in checks:
        if not ok:
            raise PeerError(f"the pairing code has an invalid {what}")
    return {
        "id": payload["id"], "name": name, "user": payload["user"], "host": payload["host"],
        "public_key": payload["key"], "host_key": payload["host_key"],
        "fingerprint": fingerprint(payload),
    }


# -- known_hosts and authorized_keys ------------------------------------------------

def host_alias(peer_id) -> str:
    return f"agentboard-{peer_id}"


def write_known_hosts(peers, home=None) -> None:
    """Regenerate peer-keys/known_hosts from the configured peers' pinned keys."""
    lines = [f"{host_alias(p['id'])} {p['host_key']}\n" for p in peers if p.get("host_key")]
    directory = keys_dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    _atomic_write(known_hosts_path(home), "".join(lines).encode(), 0o600)


def authorized_keys_path() -> Path:
    return Path.home() / ".ssh" / "authorized_keys"


def gate_bin_path() -> Path:
    """The shim the forced command names: the installed core copy when there
    is one (stable across reinstalls and app moves), else this one."""
    from . import cli
    core_dir = os.environ.get("AGENTBOARD_CORE_DIR")
    installed = (Path(core_dir) if core_dir else
                 Path.home() / ".local" / "share" / "agentboard" / "core") / "bin" / "agentboard"
    return installed if installed.is_file() else cli._bin_path()


def authorized_line(peer_id, public_key, allow_open, bin_path=None) -> str:
    if not valid_id(peer_id) or not valid_key(public_key):
        raise PeerError("invalid peer id or key")
    bin_path = str(bin_path or gate_bin_path())
    # A quote would make shlex.quote emit `"`, which ends command="..." early;
    # control characters (a newline) would split the line.
    if any(ch in bin_path for ch in '"\'\\') or not bin_path.isprintable():
        raise PeerError(f"the AgentBoard path {bin_path!r} can't be used in authorized_keys")
    command = f"{shlex.quote(bin_path)} peer-gate {peer_id}"
    options = "restrict,pty" if allow_open else "restrict"
    line = f'{options},command="{command}" {public_key} {AUTH_MARKER}{peer_id}'
    # One line, and exactly the spaces put there: after the command, the one
    # inside the key, and before the marker (plus any in the quoted path).
    if not line.isprintable() or line.count(" ") != command.count(" ") + 3:
        raise PeerError("invalid authorized_keys line")
    return line


_MARKED_RE = re.compile(r" " + re.escape(AUTH_MARKER) + r"([0-9a-f-]{36})\s*$")


def _marked_id(line: bytes):
    try:
        text = line.decode()
    except UnicodeDecodeError:
        return None
    match = _MARKED_RE.search(text)
    return match.group(1) if match else None


@contextlib.contextmanager
def _locked_dir(directory: Path):
    """flock on the directory itself (no lock file left in ~/.ssh), so two
    AgentBoard edits of authorized_keys never interleave."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        yield
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _rewrite_authorized_keys(keep, append=(), dry_run=False, dropped_out=None):
    """Rewrite ~/.ssh/authorized_keys keeping every line `keep(line)` accepts
    byte-identical, then appending the `append` lines. Returns the number
    dropped; the dropped lines go into `dropped_out` when given.

    A symlinked file (a dotfiles checkout) is written through: the link's
    target is rewritten, the link stays. Lines split on "\n" only, as sshd
    reads them. Only an append creates ~/.ssh; with nothing to add, a
    missing directory means there is nothing to remove."""
    if isinstance(append, str):
        append = [append]
    path = Path(os.path.realpath(authorized_keys_path()))
    ssh_dir = path.parent
    if not ssh_dir.exists():
        if dry_run or not append:
            return 0
        ssh_dir.mkdir(mode=0o700, parents=True)
    with _locked_dir(ssh_dir):
        try:
            original = path.read_bytes()
        except FileNotFoundError:
            original = b""
        lines = [line for line in re.split(rb"(?<=\n)", original) if line]
        kept = [line for line in lines if keep(line)]
        dropped = len(lines) - len(kept)
        if dropped_out is not None:
            dropped_out.extend(line for line in lines if not keep(line))
        for extra in append:
            if kept and not kept[-1].endswith(b"\n"):
                kept[-1] += b"\n"
            kept.append(extra if isinstance(extra, bytes) else extra.encode() + b"\n")
        new = b"".join(kept)
        if dry_run or new == original:
            return dropped
        mode = 0o600
        try:
            mode = path.stat().st_mode & 0o777
        except OSError:
            pass
        _atomic_write(path, new, mode)
    return dropped


def _marked_with_key(line: bytes, public_key) -> bool:
    """A marked line (any id) that authorizes `public_key`."""
    return (_marked_id(line) is not None
            and f" {public_key} {AUTH_MARKER}".encode() in line)


def authorize(peer_id, public_key, allow_open, bin_path=None) -> tuple:
    """Write the one marked line for `peer_id`, dropping its old line and any
    marked line of another id with the same key: sshd uses the first line
    that matches a key, so a second line would keep that id's switches in
    force whatever this id's say. Returns (line written, lines dropped) so a
    caller can undo it with `unauthorize`."""
    line = authorized_line(peer_id, public_key, allow_open, bin_path)
    dropped = []
    _rewrite_authorized_keys(
        lambda raw: _marked_id(raw) != peer_id and not _marked_with_key(raw, public_key),
        append=line, dropped_out=dropped)
    return line, dropped


def unauthorize(written, restore=()) -> None:
    """Undo `authorize`: take its line back out and put back what it dropped."""
    target = written.encode() + b"\n"
    _rewrite_authorized_keys(lambda raw: raw.rstrip(b"\n") + b"\n" != target, append=list(restore))


def deauthorize(peer_id=None, dry_run=False) -> int:
    """Remove the marked line of `peer_id` (every marked line when None)."""
    if peer_id is None:
        return _rewrite_authorized_keys(lambda raw: _marked_id(raw) is None, dry_run=dry_run)
    return _rewrite_authorized_keys(lambda raw: _marked_id(raw) != peer_id, dry_run=dry_run)


def authorized_ids() -> set:
    try:
        lines = authorized_keys_path().read_bytes().splitlines()
    except OSError:
        return set()
    return {peer_id for line in lines if (peer_id := _marked_id(line))}


def remote_login_status(connect=socket.create_connection) -> str:
    """on | off | unknown, from a bounded connect to sshd on loopback."""
    try:
        sock = connect(("127.0.0.1", REMOTE_LOGIN_PORT), timeout=0.5)
    except ConnectionRefusedError:
        return "off"
    except OSError:
        return "unknown"
    try:
        sock.close()
    except OSError:
        pass
    return "on"


# -- peer management (the `peers` subcommand's core) ---------------------------------

OWN_CODE_ERROR = "that is this Mac's own pairing code; paste it on the other Mac"
COPIED_IDENTITY_ERROR = (
    "this code comes from another Mac that has this Mac's AgentBoard identity: its AgentBoard "
    "data folder was copied from this one (by Migration Assistant or a restored backup), so "
    "the two can't be paired. On that other Mac, quit AgentBoard, move machine.json and the "
    "peer-keys folder from ~/Library/Application Support/AgentBoard to the Trash, open "
    "AgentBoard again, and copy its new pairing code")


def code_relation(parsed, home=None, host_key_path=HOST_KEY_PATH) -> str | None:
    """"own" when the code is this Mac's own, "copied" when it carries this
    Mac's machine id but another Mac's ssh host key (a data folder copied
    to a second Mac), else None."""
    machine = read_machine(home)
    if not machine or machine["id"] != parsed["id"]:
        return None
    host_key = read_host_key(host_key_path)
    if host_key is not None and host_key != parsed["host_key"]:
        return "copied"
    return "own"


def replaced_entry(config, parsed) -> dict | None:
    """The paired Mac a code would replace: the same id, else the same key
    (that Mac paired again under a new id)."""
    entries = configured_peers(config)
    return (next((p for p in entries if p["id"] == parsed["id"]), None)
            or next((p for p in entries if p.get("public_key") == parsed["public_key"]), None))


def add_peer(code, home=None, host=None, name=None, allow_open=None, bin_path=None,
             host_key_path=HOST_KEY_PATH) -> dict:
    """Pair with the Mac a code names. Pairing again (same id, or the same
    key under a new id) replaces that entry and keeps its "allow opening"
    switch and host unless `allow_open`/`host` are given; a new Mac starts
    with opening allowed and the code's host."""
    parsed = parse_code(code)
    relation = code_relation(parsed, home, host_key_path)
    if relation == "own":
        raise PeerError(OWN_CODE_ERROR)
    if relation == "copied":
        raise PeerError(COPIED_IDENTITY_ERROR)
    if host is not None and not valid_host(host):
        raise PeerError(f"invalid host: {host!r}")
    if name is not None and clean_name(name) is None:
        raise PeerError(f"invalid name: {name!r}")
    ensure_machine(home)
    previous = replaced_entry(load_config(home), parsed)
    if allow_open is None:
        allow_open = previous["allow_open"] if previous else True
    if host is None and previous and valid_host(previous.get("host")):
        host = previous["host"]
    peer = {
        "id": parsed["id"],
        "name": clean_name(name) if name else parsed["name"],
        "host": host or parsed["host"],
        "user": parsed["user"],
        "allow_open": bool(allow_open),
        "public_key": parsed["public_key"],
        "host_key": parsed["host_key"],
        # Kept so Settings shows the fingerprint the code had, unchanged by
        # a later host or name edit.
        "fingerprint": parsed["fingerprint"],
    }

    replaced = []

    def mutate(data):
        raw = data.get("peers") if isinstance(data.get("peers"), list) else []
        keep = []
        for p in raw:
            if isinstance(p, dict) and p.get("id") == peer["id"]:
                continue
            # The same key under another id is the same Mac re-paired with a
            # new id (its machine.json was lost): this add replaces it, so
            # one key never has two entries with different switches.
            if isinstance(p, dict) and p.get("public_key") == peer["public_key"]:
                replaced.append(p.get("id"))
                continue
            keep.append(p)
        data["peers"] = keep + [peer]

    # authorized_keys first: a key line whose id is not in config.json is
    # refused by the gate, so a failure below never leaves a half-paired Mac
    # that looks paired in Settings. If the config write then fails, the
    # line is taken back out and the previous ones restored.
    try:
        written, dropped = authorize(peer["id"], peer["public_key"], peer["allow_open"], bin_path)
    except OSError as exc:
        raise PeerError(f"couldn't update {authorized_keys_path()} ({exc.strerror or type(exc).__name__})")
    try:
        config = update_config(mutate, home)
    except (OSError, PeerError) as exc:
        try:
            unauthorize(written, dropped)
        except OSError as undo_exc:
            diag.warn("peers", "add_rollback_failed", peer=peer["id"],
                      error=undo_exc.strerror or type(undo_exc).__name__)
        if isinstance(exc, PeerError):
            raise
        raise PeerError(f"couldn't save the machine ({exc.strerror or type(exc).__name__})")
    try:
        write_known_hosts(configured_peers(config), home)
    except OSError as exc:
        raise PeerError(f"couldn't save the machine ({exc.strerror or type(exc).__name__})")
    for old_id in replaced:
        if valid_id(old_id):
            try:
                cache_path(old_id, home).unlink()
            except OSError:
                pass
    if replaced:
        touch_stamp(home)
    diag.info("peers", "added", peer=peer["id"], host=peer["host"], replaced=len(replaced))
    return dict(peer)


def remove_peer(peer_id=None, home=None, dry_run=False) -> int:
    """Remove one peer (or all when None) from config, known_hosts, its cache
    and authorized_keys. Returns how many authorized_keys lines went."""
    removed_ids = []

    def mutate(data):
        raw = data.get("peers") if isinstance(data.get("peers"), list) else []
        keep = []
        for p in raw:
            pid = p.get("id") if isinstance(p, dict) else None
            if peer_id is None or pid == peer_id:
                removed_ids.append(pid)
            else:
                keep.append(p)
        data["peers"] = keep

    # authorized_keys first: access to this Mac goes even when config.json
    # can't be edited (uninstall's --all tolerates a malformed one).
    dropped = deauthorize(peer_id, dry_run=dry_run)
    if not dry_run and (config_file(home).exists() or peer_id is not None):
        try:
            config = update_config(mutate, home)
        except MalformedConfigError:
            if peer_id is not None:
                raise
            diag.warn("peers", "remove_config_skipped", reason="malformed config.json")
            config = None
        if config is not None and keys_dir(home).exists():
            write_known_hosts(configured_peers(config), home)
        for pid in removed_ids:
            if valid_id(pid):
                try:
                    cache_path(pid, home).unlink()
                except OSError:
                    pass
        if removed_ids:
            touch_stamp(home)
    diag.info("peers", "removed", peer=peer_id or "all", lines=dropped, dry_run=dry_run)
    return dropped


def set_peer(peer_id, home=None, host=None, name=None, allow_open=None, bin_path=None,
             dismiss_address=None) -> dict:
    """Change a paired Mac's host, name or allow-opening switch, or stop
    suggesting an address it advertises (`dismiss_address`)."""
    if host is not None and not valid_host(host):
        raise PeerError(f"invalid host: {host!r}")
    if dismiss_address is not None and not valid_host(dismiss_address):
        raise PeerError(f"invalid address: {dismiss_address!r}")
    if name is not None and clean_name(name) is None:
        raise PeerError(f"invalid name: {name!r}")
    found = {}

    def mutate(data):
        raw = data.get("peers") if isinstance(data.get("peers"), list) else []
        for p in raw:
            if isinstance(p, dict) and p.get("id") == peer_id:
                if host is not None:
                    p["host"] = host
                if name is not None:
                    p["name"] = clean_name(name)
                if allow_open is not None:
                    p["allow_open"] = bool(allow_open)
                if dismiss_address is not None:
                    p["dismissed_address"] = dismiss_address
                found["peer"] = p

    config = load_config(home)
    if find_peer(config, peer_id) is None:
        raise PeerError(f"no paired machine with id {peer_id}")
    update_config(mutate, home)
    peer = find_peer(load_config(home), peer_id)
    if allow_open is not None and peer.get("public_key"):
        authorize(peer_id, peer["public_key"], peer["allow_open"], bin_path)
    touch_stamp(home)
    return peer


def set_share(level, home=None) -> None:
    if level not in SHARE_LEVELS:
        raise PeerError(f"share level must be one of {', '.join(SHARE_LEVELS)}")
    update_config(lambda data: data.__setitem__("peer_share", level), home)
    touch_stamp(home)


# -- cache files --------------------------------------------------------------------

def peers_dir(home=None) -> Path:
    return _home(home) / PEERS_DIR


def cache_path(peer_id, home=None) -> Path:
    return peers_dir(home) / f"{peer_id}.json"


def stamp_path(home=None) -> Path:
    return _home(home) / STAMP_FILE


def touch_stamp(home=None, now=None) -> None:
    try:
        _atomic_write(stamp_path(home), (now_iso(now) + "\n").encode(), 0o644)
    except OSError as exc:
        diag.debug("peers", "stamp_failed", exc=type(exc).__name__)


def read_cache(peer_id, home=None) -> dict | None:
    try:
        data = json.loads(cache_path(peer_id, home).read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        diag.debug("peers", "cache_unreadable", peer=peer_id, exc=type(exc).__name__)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("sessions", []), list):
        diag.debug("peers", "cache_unreadable", peer=peer_id, exc="shape")
        return None
    return data


def write_cache(peer_id, data, home=None, now=None) -> None:
    _atomic_write(cache_path(peer_id, home), json.dumps(data).encode(), 0o600)
    touch_stamp(home, now)


def peer_status(cache, now) -> tuple:
    """(status, last_seen_at) for a peer from its cache: `online` only while
    lines keep arriving; a cache without a recent line is `offline` whatever
    status peer-sync last wrote (it may not be running at all)."""
    if not cache:
        return "never", None
    last_seen = cache.get("last_seen_at")
    seen = parse_ts(last_seen)
    status = cache.get("status") if isinstance(cache.get("status"), str) else "offline"
    if status in ("incompatible", "error"):
        return status, last_seen
    if seen is None or (now - seen).total_seconds() > OFFLINE_AFTER_SECS:
        return "offline", last_seen
    return ("online" if status == "online" else status), last_seen


# -- export (serving side) -------------------------------------------------------------

_TIMES = ("started_at", "updated_at", "ended_at", "turn_finished_at")
# waiting_reason can hold a note's headline, and a title that falls back
# to the first prompt is prompt text, so neither leaves below `full`.
_STATE_FIELDS = (
    "tool", "session_id", "state", "auto_done",
    "alive", "interactive", *_TIMES, "turn_prompt_kind", "background_tasks",
    "turn_ends_background_wait",
)
_TITLE_FIELDS = _STATE_FIELDS + (
    "title", "title_source", "project", "branch", "notes_count", "denials_count",
    "desktop", "tmux", "entrypoint", "done_reason",
)
_FULL_FIELDS = _TITLE_FIELDS + (
    "waiting_reason", "cwd", "first_prompt", "last_prompt", "last_assistant", "status_text", "model",
    "context_tokens", "context_at", "permission_mode", "tmux_session", "remote_control_url",
)


def remote_open_for(entry, allow_open, level="full") -> str | None:
    """How a click on the peer opens this session: `tmux` (live, in tmux),
    `remote-control` (live and served by Claude Code Remote Control, at the
    `full` share level only, since its URL opens the whole conversation),
    `resume` (its process ended), `screen` (live, anywhere else), or None."""
    if not allow_open:
        return None
    if entry.get("alive"):
        if entry.get("tmux"):
            return "tmux"
        if level == "full" and entry.get("remote_control_url"):
            return "remote-control"
        return "screen"
    if entry.get("resume_command"):
        return "resume"
    return None


def export_entry(entry, level, allow_open) -> dict:
    fields = {"full": _FULL_FIELDS, "titles": _TITLE_FIELDS}.get(level, _STATE_FIELDS)
    out = {name: entry.get(name) for name in fields}
    triage = entry.get("triage") or {}
    out["triage"] = {"status": triage.get("status") or "open", "until": triage.get("until")}
    if level == "full":
        out["triage"]["note"] = triage.get("note")
        out["links"] = entry.get("links") or []
        out["notes"] = entry.get("notes") or []
        out["denials"] = entry.get("denials") or []
    elif level == "titles":
        out["links"] = entry.get("links") or []
        if entry.get("title_source") == "fallback":
            out["title"] = entry.get("project") or entry.get("tool")
    else:
        out["title"] = entry.get("project") or entry.get("tool")
    out["remote_open"] = remote_open_for(entry, allow_open, level)
    return out


def build_export(conn, level, allow_open=True, home=None, now=None, snapshot=None,
                 address=None) -> dict:
    """One export line: this Mac's own sessions (never ones received from a
    peer) at share level `level`, and the address it advertises (`address`,
    when given) so a paired Mac can offer to switch to it."""
    if snapshot is None:
        from . import cli
        snapshot = cli.build_snapshot(conn, now=now, include_peers=False)
    machine = read_machine(home) or {"id": None, "name": None}
    # A session id the receiver would refuse (it rejects the whole line) is
    # left out here instead.
    local = [e for e in snapshot.get("sessions", []) if not e.get("machine")
             and isinstance(e.get("session_id"), str) and _SESSION_ID_RE.match(e["session_id"])]
    sessions = [export_entry(e, level, allow_open) for e in _within_cap(local, MAX_SESSIONS)]
    if len(sessions) < len(local):
        diag.debug("peers", "export_capped", sessions=len(local), sent=len(sessions))
    export = {
        "v": PROTOCOL_VERSION,
        "machine": {"id": machine["id"], "name": machine["name"]},
        "generated_at": now_iso(now),
        "share": level,
        "sessions": sessions,
    }
    if address is not None and valid_host(address):
        export["advertised_address"] = address
    return export


def _send_rank(entry):
    """Which sessions an over-large export keeps: live and needs_you ones
    first, then the most recently updated."""
    urgent = bool(entry.get("alive")) or entry.get("state") == "needs_you"
    updated = parse_ts(entry.get("updated_at"))
    return (0 if urgent else 1, -(updated.timestamp() if updated else 0.0))


def _within_cap(entries, limit):
    """`entries` cut to the `limit` best by _send_rank, in their own order."""
    if len(entries) <= limit:
        return list(entries)
    keep = {id(e) for e in sorted(entries, key=_send_rank)[:limit]}
    return [e for e in entries if id(e) in keep]


def encode_export(export) -> str:
    """One export as a JSON line the receiver accepts: within MAX_SESSIONS
    (build_export already caps) and MAX_LINE_BYTES, dropping the lowest
    ranked sessions until it fits."""
    line = json.dumps(export, separators=(",", ":"))
    sessions = export.get("sessions") or []
    while len(line.encode()) > MAX_LINE_BYTES and sessions:
        sessions = _within_cap(sessions, len(sessions) * 4 // 5)
        line = json.dumps({**export, "sessions": sessions}, separators=(",", ":"))
    return line


# What a peer's `error` is about (peer_sync._SSH_ERRORS), so the app can say
# what to fix: `address` and `unreachable` point at the peer's address.
ERROR_KINDS = ("address", "unreachable", "host_key", "auth", "remote_login", "refused")


def rejection_error(reason, sessions=None) -> str:
    """What Settings shows for a peer whose lines this Mac rejects."""
    if reason in ("too large", "too many sessions"):
        count = f" ({sessions} sessions)" if sessions else ""
        return f"its board is too large to sync{count}; update both Macs"
    if reason == "incompatible":
        return "it runs an incompatible AgentBoard version; update both Macs"
    if reason == "machine mismatch":
        return "it reports a different machine id; remove it and pair it again"
    return f"it sent data this Mac can't read ({reason}); update both Macs"


# -- validation of received lines (receiving side's trust boundary) ----------------------

MAX_LINE_BYTES = 8 * 1024 * 1024
MAX_SESSIONS = 500
MAX_TEXT = 4000
MAX_SHORT = 512
MAX_LIST = 50
_TOOLS = ("claude", "codex")
_STATES = ("needs_you", "interrupted", "running", "review", "unconcluded", "snoozed", "done")
_REMOTE_OPEN = ("tmux", "resume", "screen", "remote-control")
# First character alphanumeric, so an id never reads as a CLI option.
_SESSION_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_REMOTE_CONTROL_URL_RE = re.compile(r"\Ahttps://claude\.ai/code/session_[A-Za-z0-9]{10,64}\Z")

# field -> (kind, cap)
_FIELD_TYPES = {
    "state": ("enum", _STATES),
    "waiting_reason": ("str", MAX_SHORT),
    "auto_done": ("bool", None),
    "done_reason": ("str", MAX_SHORT),
    "alive": ("bool", None),
    "interactive": ("bool", None),
    "started_at": ("ts", None),
    "updated_at": ("ts", None),
    "ended_at": ("ts", None),
    "turn_finished_at": ("ts", None),
    "turn_prompt_kind": ("str", 64),
    "background_tasks": ("int", None),
    "turn_ends_background_wait": ("bool", None),
    "title": ("str", MAX_SHORT),
    "title_source": ("str", 64),
    "project": ("str", MAX_SHORT),
    "branch": ("str", MAX_SHORT),
    "notes_count": ("int", None),
    "denials_count": ("int", None),
    "desktop": ("bool", None),
    "tmux": ("bool", None),
    "entrypoint": ("str", 64),
    "cwd": ("str", 4096),
    "first_prompt": ("str", MAX_TEXT),
    "last_prompt": ("str", MAX_TEXT),
    "last_assistant": ("str", MAX_TEXT),
    "status_text": ("str", MAX_SHORT),
    "model": ("str", 128),
    "context_tokens": ("int", None),
    "context_at": ("ts", None),
    "permission_mode": ("str", 64),
    "tmux_session": ("str", MAX_SHORT),
    "remote_open": ("enum", _REMOTE_OPEN),
    "remote_control_url": ("str", 128),
}
_LINK_FIELDS = {"kind": 16, "ref": MAX_SHORT, "url": 2048, "state": 32, "title": MAX_SHORT,
                "branch": MAX_SHORT}
_NOTE_FIELDS = {"kind": 32, "text": MAX_TEXT, "headline": MAX_SHORT, "ts": 40, "last_ts": 40,
                "source": 64}
_DENIAL_STR_FIELDS = {"tool_name": 128, "reason": 400, "ts": 40, "mcp_server": 128}


class LineRejected(Exception):
    def __init__(self, reason, sessions=None):
        super().__init__(reason)
        self.sessions = sessions


def _check(kind, cap, value):
    if value is None:
        return None
    if kind == "str":
        if not isinstance(value, str):
            raise LineRejected("type")
        # json.loads keeps a lone surrogate ("\ud800"); written back into the
        # snapshot it makes Foundation reject the whole document, so the
        # board would stop updating. Replaced here (as "?").
        return value[:cap].encode("utf-8", "replace").decode("utf-8")
    if kind == "ts":
        if not isinstance(value, str) or len(value) > 40 or not value.isascii() \
                or parse_ts(value) is None:
            raise LineRejected("timestamp")
        return value
    if kind == "bool":
        if not isinstance(value, bool):
            raise LineRejected("type")
        return value
    if kind == "int":
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value < 2 ** 53:
            raise LineRejected("type")
        return value
    if kind == "enum":
        if value not in cap:
            raise LineRejected("enum")
        return value
    raise LineRejected("kind")


def _clean_strs(item, spec):
    if not isinstance(item, dict):
        raise LineRejected("type")
    out = {}
    for name, cap in spec.items():
        out[name] = _check("str", cap, item.get(name))
    return out


def _clean_session(raw) -> dict:
    if not isinstance(raw, dict):
        raise LineRejected("session")
    tool, session_id = raw.get("tool"), raw.get("session_id")
    if tool not in _TOOLS or not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
        raise LineRejected("session id")
    out = {"tool": tool, "session_id": session_id}
    for name, (kind, cap) in _FIELD_TYPES.items():
        out[name] = _check(kind, cap, raw.get(name))
    if out["state"] is None:
        raise LineRejected("state")
    # Only a Remote Control URL of the one known shape is ever opened.
    url = out["remote_control_url"]
    if url is not None and not _REMOTE_CONTROL_URL_RE.match(url):
        out["remote_control_url"] = None
    if out["remote_open"] == "remote-control" and out["remote_control_url"] is None:
        out["remote_open"] = "screen"
    triage = raw.get("triage") if isinstance(raw.get("triage"), dict) else {}
    out["triage"] = {
        "status": triage.get("status") if triage.get("status") in ("open", "done", "snoozed") else "open",
        "until": _check("ts", None, triage.get("until")),
        "note": _check("str", MAX_SHORT, triage.get("note")),
    }
    links = raw.get("links") or []
    notes = raw.get("notes") or []
    denials = raw.get("denials") or []
    if not all(isinstance(v, list) for v in (links, notes, denials)):
        raise LineRejected("type")
    out["links"] = []
    for link in links[:MAX_LIST]:
        clean = _clean_strs(link, _LINK_FIELDS)
        clean["owned"] = link.get("owned") is True
        clean["awaits_you"] = link.get("awaits_you") is True
        if clean["url"] and not re.match(r"^https?://", clean["url"]):
            clean["url"] = None
        out["links"].append(clean)
    out["notes"] = []
    for note in notes[:MAX_LIST]:
        clean = _clean_strs(note, _NOTE_FIELDS)
        clean["id"] = _check("int", None, note.get("id")) or 0
        clean["count"] = _check("int", None, note.get("count")) or 1
        out["notes"].append(clean)
    out["denials"] = []
    for denial in denials[:MAX_LIST]:
        clean = _clean_strs(denial, _DENIAL_STR_FIELDS)
        clean["id"] = _check("int", None, denial.get("id")) or 0
        clean["subagent"] = denial.get("subagent") is True
        out["denials"].append(clean)
    return out


def validate_line(line) -> dict:
    """Parse and normalise one received export line; LineRejected(reason)
    for anything off. Only whitelisted fields of checked types survive."""
    if isinstance(line, str):
        line = line.encode()
    if len(line) > MAX_LINE_BYTES:
        raise LineRejected("too large")
    try:
        data = json.loads(line)
    except (ValueError, UnicodeDecodeError, RecursionError):
        # RecursionError: a deeply nested line ("[" * 200000).
        raise LineRejected("not json")
    if not isinstance(data, dict):
        raise LineRejected("not an object")
    if data.get("v") != PROTOCOL_VERSION:
        raise LineRejected("incompatible")
    machine = data.get("machine")
    if not isinstance(machine, dict) or not valid_id(machine.get("id")):
        raise LineRejected("machine")
    generated = data.get("generated_at")
    if parse_ts(generated) is None:
        raise LineRejected("generated_at")
    sessions = data.get("sessions")
    if not isinstance(sessions, list):
        raise LineRejected("sessions")
    if len(sessions) > MAX_SESSIONS:
        raise LineRejected("too many sessions", sessions=len(sessions))
    # Absent from older peers; anything that is not a valid host is dropped.
    advertised = data.get("advertised_address")
    return {
        "machine": {"id": machine["id"], "name": clean_name(machine.get("name"))},
        "advertised_address": advertised if valid_host(advertised) else None,
        "generated_at": generated,
        "share": data.get("share") if data.get("share") in SHARE_LEVELS else None,
        "sessions": _unique_sessions(_clean_session(s) for s in sessions),
    }


def _unique_sessions(sessions) -> list:
    """The first of each tool:session_id pair; a repeat would give the app
    two rows with one id."""
    out, seen = [], set()
    for session in sessions:
        key = (session["tool"], session["session_id"])
        if key not in seen:
            seen.add(key)
            out.append(session)
    return out


# -- merge (snapshot side; local files only) ----------------------------------------------

_LOCAL_DEFAULTS = {
    "cwd": None, "first_prompt": None, "last_prompt": None, "last_assistant": None,
    "status_text": None, "model": None, "context_tokens": None, "context_at": None,
    "permission_mode": None, "tmux_session": None, "title_source": None, "project": None,
    "branch": None, "notes_count": 0, "denials_count": 0, "desktop": False, "tmux": False,
    "entrypoint": None, "links": [], "notes": [], "denials": [], "interactive": True,
    "alive": False, "resume_command": None, "auto_done": False, "remote_control_url": None,
    "turn_ends_background_wait": False,
}


def remote_entries(config, now, home=None) -> tuple:
    """(entries, peers) for build_snapshot: each configured peer's cached
    sessions as snapshot entries, and the top-level peers list. Reads local
    files only; never a socket or a process."""
    entries, peers = [], []
    cutoff = now - timedelta(days=stale_days(config))
    for peer in configured_peers(config):
        cache = read_cache(peer["id"], home)
        status, last_seen = peer_status(cache, now)
        received = parse_ts((cache or {}).get("received_at"))
        sessions = []
        if cache and received is not None and received >= cutoff:
            sessions = [s for s in cache.get("sessions") or [] if isinstance(s, dict)]
        elif cache and received is not None:
            status = "expired"
        elif cache and status not in ("incompatible", "error"):
            # Never reached (peer-sync writes a cache on the first failed
            # try, the normal middle of pairing), not one that expired.
            status = "never"
        stale = status != "online"
        machine = {"id": peer["id"], "name": peer["name"]}
        count = 0
        for raw in sessions:
            tool, session_id = raw.get("tool"), raw.get("session_id")
            if tool not in _TOOLS or not isinstance(session_id, str):
                continue
            entry = dict(_LOCAL_DEFAULTS)
            entry.update({k: v for k, v in raw.items() if v is not None or k not in _LOCAL_DEFAULTS})
            entry["key"] = f"{tool}:{session_id}@{peer['id']}"
            entry["machine"] = machine
            entry["stale"] = stale
            entry["can_focus"] = entry.get("remote_open") is not None
            entry["resume_command"] = None
            entry.setdefault("triage", {"status": "open", "until": None, "note": None})
            entry["title"] = entry.get("title") or entry.get("project") or session_id
            entries.append(entry)
            count += 1
        suggested = (cache or {}).get("suggested_address")
        if not valid_host(suggested) or any(same_host(suggested, known) for known in
                                            (peer["host"], peer.get("dismissed_address"))):
            suggested = None
        error_kind = (cache or {}).get("error_kind")
        peers.append({
            "id": peer["id"],
            "name": peer["name"],
            "host": peer["host"],
            "status": status,
            "last_seen_at": last_seen,
            "error": (cache or {}).get("error") if isinstance((cache or {}).get("error"), str) else None,
            "error_kind": error_kind if error_kind in ERROR_KINDS else None,
            "suggested_address": suggested,
            "session_count": count,
            "allow_open": peer["allow_open"],
            "clock_skew_secs": (cache or {}).get("clock_skew_secs")
            if isinstance((cache or {}).get("clock_skew_secs"), (int, float)) else None,
        })
    return entries, peers
