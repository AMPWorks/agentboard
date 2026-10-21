"""Diagnostic log: one size-capped, append-only text file under AGENTBOARD_HOME.

Every line is ``<utc-iso> <LEVEL> <component>.<event> key=value ...`` so the
file stays greppable and ``tail -f`` friendly. Growth is bounded by
construction: when the next line would push ``agentboard.log`` past
``MAX_LOG_BYTES`` it is renamed over ``agentboard.log.1`` (replacing it, never
numbering further), so at most two files ever exist and together they never
hold much more than twice the cap. With one writer each file stays within the
cap; under concurrency a writer whose size check passed just before another
process rotated (or that gave up waiting for the rotation lock) appends to the
file being rotated, so ``.1`` may exceed the cap by a few lines -- still
bounded, and never a ``.2``. On a filesystem without ``flock`` support
(ENOTSUP/EOPNOTSUPP/ENOLCK, e.g. some network or SMB volumes used as
AGENTBOARD_HOME) rotation still happens, lock-free behind the same inode
check, so the cap holds there too; the price is a rare race that can
clobber ``.1`` with a nearly empty file, never unbounded growth.

Contract: nothing in here ever raises. The hook imports this lazily and the
app polls ``snapshot`` every few seconds, so a broken log must never break
either one. ``hook-errors.log`` (hook.py) is separate and unchanged.
"""

import json
import os
import shlex
from datetime import datetime, timezone

LOG_FILENAME = "agentboard.log"
ROTATED_FILENAME = LOG_FILENAME + ".1"
MAX_LOG_BYTES = 2 * 1024 * 1024
# A single line is cut to this many bytes (and never to more than the cap
# itself), which also keeps every append below the size at which O_APPEND
# writes from concurrent processes could interleave.
MAX_LINE_BYTES = 4000

LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}
DEFAULT_LEVEL = "info"
LEVEL_ENV = "AGENTBOARD_LOG_LEVEL"

REDACTED_FLAGS = ("--note", "--text", "--title")
LONG_ARG_LIMIT = 200
LONG_ARG_KEEP = 80

# A busy rotation lock is retried briefly (the holder only stats, renames
# and reopens, a few microseconds) so the loser normally writes to the fresh
# file; past this budget it appends without rotating rather than block a hook.
LOCK_RETRIES = 10
LOCK_RETRY_SECS = 0.001

# _try_lock outcomes. UNSUPPORTED means flock itself failed (not merely
# busy): rotate without the lock rather than never rotate.
LOCKED = "locked"
BUSY = "busy"
UNSUPPORTED = "unsupported"

_config_cache = None  # (path, mtime_ns, size, level-or-None)


def _home():
    override = os.environ.get("AGENTBOARD_HOME")
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), "Library", "Application Support", "AgentBoard")


def log_path():
    return os.path.join(_home(), LOG_FILENAME)


def rotated_path():
    return os.path.join(_home(), ROTATED_FILENAME)


def _normalize_level(value):
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    if value == "warning":
        value = "warn"
    return value if value in LEVELS else None


def _config_level():
    global _config_cache
    path = os.path.join(_home(), "config.json")
    try:
        st = os.stat(path)
    except OSError:
        return None
    cached = _config_cache
    if cached and cached[0] == path and cached[1] == st.st_mtime_ns and cached[2] == st.st_size:
        return cached[3]
    level = None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            level = _normalize_level(data.get("log_level"))
    except (OSError, ValueError):
        level = None
    _config_cache = (path, st.st_mtime_ns, st.st_size, level)
    return level


def current_level():
    """AGENTBOARD_LOG_LEVEL, else config.json's log_level, else info."""
    try:
        return (
            _normalize_level(os.environ.get(LEVEL_ENV))
            or _config_level()
            or DEFAULT_LEVEL
        )
    except Exception:
        return DEFAULT_LEVEL


def enabled(level):
    try:
        return LEVELS.get(level, 0) >= LEVELS[current_level()]
    except Exception:
        return False


def _format_value(value):
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        value = f"{value:.1f}"
    elif isinstance(value, (list, tuple)):
        value = shlex.join(str(item) for item in value)
    else:
        value = str(value)
    if value == "":
        return '""'
    if any(ch in value for ch in ' "=\\\n\r\t'):
        escaped = (
            value.replace("\\", "\\\\").replace('"', '\\"')
            .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
        )
        return f'"{escaped}"'
    return value


def format_line(level, component, event, fields, now=None):
    now = now or datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
    parts = [ts, level.upper(), f"{component}.{event}"]
    for key, value in fields.items():
        parts.append(f"{key}={_format_value(value)}")
    return " ".join(parts)


def _encode_line(text, cap):
    data = text.replace("\n", "\\n").encode("utf-8", "replace")
    limit = max(1, min(MAX_LINE_BYTES, cap) - 1)
    if len(data) > limit:
        data = data[: max(0, limit - 3)] + b"..."
        data = data[:limit]
    return data + b"\n"


def _open_append(path):
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    try:
        return os.open(path, flags, 0o644)
    except FileNotFoundError:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return os.open(path, flags, 0o644)


def _rotate_if_needed(fd, path, incoming, cap):
    """Rotate when appending `incoming` bytes would push the file past `cap`.

    Returns the fd to write to (a fresh one if the file was rotated or
    another writer already rotated it underneath us). Concurrency: the
    flock is taken on this same fd (non-blocking, retried for at most
    LOCK_RETRIES x LOCK_RETRY_SECS; a lock still busy after that means this
    write just appends -- to the inode about to become `.1`, one reason
    `.1` can end up a few lines over the cap under contention), and the
    rename only happens if the path still names the inode this fd has open
    and that inode is still over the cap -- so two writers racing to rotate
    can never rename a freshly-started log over the real `.1`.

    If flock fails outright (anything but "busy", e.g. ENOTSUP on a network
    volume) the same inode-gated rename runs without the lock. Two writers
    can then both pass the inode check and the second may rename a fresh
    log over `.1`; that loses some history but keeps growth bounded, which
    skipping rotation forever would not.
    """
    st = os.fstat(fd)
    if st.st_size == 0 or st.st_size + incoming <= cap:
        return fd
    if _try_lock(fd) == BUSY:
        return fd
    try:
        try:
            path_st = os.stat(path)
        except FileNotFoundError:
            path_st = None
        if path_st is None or path_st.st_ino != st.st_ino or path_st.st_dev != st.st_dev:
            # Someone else rotated between our open and our lock: write to
            # the live file, not into what is now `.1`.
            new_fd = _open_append(path)
            os.close(fd)
            return new_fd
        if path_st.st_size + incoming <= cap:
            return fd
        os.replace(path, path + ".1")
        new_fd = _open_append(path)
        os.close(fd)  # releases the flock on the rotated inode
        return new_fd
    except Exception:
        return fd


def _try_lock(fd):
    """LOCKED, BUSY (another process held it for the whole retry budget) or
    UNSUPPORTED (flock failed for any other reason, typically ENOTSUP,
    EOPNOTSUPP or ENOLCK on a filesystem that does not support it)."""
    import time

    try:
        import fcntl
    except ImportError:
        return UNSUPPORTED
    for attempt in range(LOCK_RETRIES):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return LOCKED
        except BlockingIOError:
            if attempt + 1 < LOCK_RETRIES:
                time.sleep(LOCK_RETRY_SECS)
        except OSError:
            return UNSUPPORTED
    return BUSY


def _write(data, cap):
    path = log_path()
    fd = _open_append(path)
    try:
        fd = _rotate_if_needed(fd, path, len(data), cap)
        os.write(fd, data)
    finally:
        os.close(fd)


def log(level, component, event, /, **fields):
    """Append one line if `level` passes the configured threshold. Never raises."""
    try:
        level = _normalize_level(level) or "info"
        if not enabled(level):
            return
        cap = int(MAX_LOG_BYTES)
        line = format_line(level, component, event, fields)
        _write(_encode_line(line, cap), cap)
    except Exception:
        pass


def debug(component, event, /, **fields):
    log("debug", component, event, **fields)


def info(component, event, /, **fields):
    log("info", component, event, **fields)


def warn(component, event, /, **fields):
    log("warn", component, event, **fields)


def error(component, event, /, **fields):
    log("error", component, event, **fields)


def redact(argv):
    """argv safe to log: the free text after --note/--text/--title becomes
    `<redacted:N chars>` (both `--flag value` and `--flag=value`), any other
    argument over 200 chars keeps its first 80 plus `...`; tools, session
    ids, paths and flags pass through verbatim."""
    try:
        out = []
        items = [str(item) for item in (argv or [])]
        i = 0
        while i < len(items):
            arg = items[i]
            if arg in REDACTED_FLAGS:
                out.append(arg)
                if i + 1 < len(items):
                    out.append(f"<redacted:{len(items[i + 1])} chars>")
                    i += 1
            else:
                flag, sep, value = arg.partition("=")
                if sep and flag in REDACTED_FLAGS:
                    out.append(f"{flag}=<redacted:{len(value)} chars>")
                elif len(arg) > LONG_ARG_LIMIT:
                    out.append(arg[:LONG_ARG_KEEP] + "...")
                else:
                    out.append(arg)
            i += 1
        return out
    except Exception:
        return []


def tail_text(text, limit=500):
    """The last `limit` chars of `text`, stripped, for stdout/stderr fields."""
    try:
        if text is None:
            return None
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        text = str(text).strip()
        return text if len(text) <= limit else "..." + text[-limit:]
    except Exception:
        return None


def ms_since(start):
    """Whole milliseconds since a time.monotonic() reading."""
    import time

    try:
        return int((time.monotonic() - start) * 1000)
    except Exception:
        return None


__all__ = [
    "LOG_FILENAME", "ROTATED_FILENAME", "MAX_LOG_BYTES", "MAX_LINE_BYTES", "LEVELS",
    "log", "debug", "info", "warn", "error", "redact", "current_level", "enabled",
    "log_path", "rotated_path", "format_line", "tail_text", "ms_since",
]

