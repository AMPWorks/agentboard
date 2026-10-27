"""Plan usage sources: the snapshot's `usage` list.

A usage source is anything that knows how much of a plan or spend limit an
agent has used: a source name, a display label, when the numbers were
measured, and an ordered list of windows, each with its own label, a
used percentage and optionally when it resets and the dollars behind it.
Nothing here knows a plan, a billing model or a window name; labels come
from the data.

Where sources come from, all local files, so `snapshot` stays network-free:

- Claude Code's local cache `~/.claude/rate-limits.json` (undocumented; the
  same numbers the status line receives as `rate_limits`, which exist only
  for Pro/Max plans or behind a gateway that sets a spend limit). Read as a
  best-effort cache.
- The drop directory `$AGENTBOARD_HOME/usage/`: any agent or tool keeps one
  `<source>.json` file there in the generic shape (see `read_source_file`)
  and replaces it when its numbers change. AgentBoard's own Codex reader is
  one such writer (`record_source`, fed from the rollout's newest
  `token_count` event by backfill).

Every reader is feature-detected and never raises; an absent source is the
normal case (API-key, Bedrock/Vertex, most gateways, or no Claude at all)
and is silent, and anything malformed is logged at debug only, because the
app polls `snapshot` every few seconds.
"""

import json
import math
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import diag

# Far more than any source file needs; a runaway file is not parsed.
MAX_BYTES = 64 * 1024
# Bounds on what one snapshot carries, whatever lands in the directory.
MAX_SOURCES = 8
MAX_WINDOWS = 8
MAX_LABEL_CHARS = 40
# A source whose numbers are older than this is left out: whoever wrote it
# has stopped reporting (an old plan, an uninstalled tool).
MAX_AGE = timedelta(days=7)

USAGE_DIR_NAME = "usage"
CLAUDE_SOURCE = "claude"

# Optional window fields copied when present (the documented status line
# `spend_limit` shape: used_usd, limit_usd, period).
_NUMBER_FIELDS = ("used_usd", "limit_usd")
_STRING_FIELDS = ("period",)

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirty": 30,
}
_UNITS = {
    "minute": "m", "minutes": "m", "min": "m",
    "hour": "h", "hours": "h",
    "day": "d", "days": "d",
    "week": "w", "weeks": "w",
    "month": "mo", "months": "mo",
}


def usage_dir(home: Path) -> Path:
    return Path(home) / USAGE_DIR_NAME


def _is_number(value) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # an int too large for a float
        return False


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value):
    """An aware UTC datetime from epoch seconds or an ISO 8601 string, or
    None when the value is neither or falls outside what a datetime holds
    once converted to UTC (`9999-12-31T23:59:59-05:00`), so callers never
    see an OverflowError."""
    if _is_number(value):
        if value < 0:
            return None
        try:
            return datetime.fromtimestamp(value, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except (OverflowError, ValueError):
            return None
    return None


def _time_to_iso(value):
    parsed = _parse_time(value)
    return _iso(parsed) if parsed is not None else None


def _clean_label(value):
    if not isinstance(value, str):
        return None
    label = " ".join(value.split())
    if not label:
        return None
    return label[:MAX_LABEL_CHARS]


def label_from_key(key: str) -> str:
    """A window label from a data key, by rule rather than by name: a
    leading count and time unit shorten (`five_hour` -> `5h`, `seven_day`
    -> `7d`, `seven_day_opus` -> `7d opus`, `30_minute` -> `30m`); any
    other key reads as words (`spend_limit` -> `spend limit`)."""
    words = [w for w in str(key).replace("-", "_").split("_") if w]
    if len(words) >= 2:
        count = _NUMBER_WORDS.get(words[0].lower())
        # ASCII digits only and a sane length: int() rejects other Unicode
        # digits ("²") and very long digit strings with a ValueError.
        if count is None and words[0].isascii() and words[0].isdigit() \
                and len(words[0]) <= 6:
            count = int(words[0])
        unit = _UNITS.get(words[1].lower())
        if count is not None and unit is not None:
            return " ".join([f"{count}{unit}"] + words[2:])[:MAX_LABEL_CHARS]
    return (" ".join(words) or str(key))[:MAX_LABEL_CHARS]


def label_from_minutes(minutes):
    """`5h`, `7d`, `30m` for a window length in minutes, or None. A length
    a minute short of a whole hour or day (some writers report 299 for a
    5-hour window) rounds to it."""
    if not _is_number(minutes) or minutes <= 0:
        return None
    minutes = int(round(minutes))
    for size, suffix in ((1440, "d"), (60, "h")):
        whole = round(minutes / size)
        if whole >= 1 and abs(minutes - whole * size) <= 1:
            return f"{whole}{suffix}"
    return f"{minutes}m"


def _window(raw, label):
    """One snapshot window from its raw object, or None when it has no
    numeric used_percentage or no label."""
    label = _clean_label(label)
    if label is None or not isinstance(raw, dict) or not _is_number(raw.get("used_percentage")):
        return None
    # No upper clamp: a spend limit runs past 100 once it is exceeded.
    percentage = round(max(0.0, float(raw["used_percentage"])), 1)
    window = {"label": label, "used_percentage": percentage}
    resets_at = _time_to_iso(raw.get("resets_at"))
    if resets_at is not None:
        window["resets_at"] = resets_at
    for key in _NUMBER_FIELDS:
        if _is_number(raw.get(key)):
            window[key] = raw[key]
    for key in _STRING_FIELDS:
        if isinstance(raw.get(key), str):
            window[key] = raw[key]
        elif _is_number(raw.get(key)):
            window[key] = str(raw[key])
    return window


def _display_path(path: Path) -> str:
    """The path with the home directory shown as `~`, for tooltips."""
    path = Path(path)
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def _read_json_object(path: Path, source: str):
    """The JSON object in `path`, or None (missing silently, anything else
    at debug)."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        diag.debug("usage", "unreadable", source=source, path=path, exc=type(exc).__name__)
        return None
    if len(data) > MAX_BYTES:
        diag.debug("usage", "unreadable", source=source, path=path, reason="too_large")
        return None
    try:
        raw = json.loads(data)
    except (ValueError, RecursionError):  # deep nesting raises RecursionError
        diag.debug("usage", "unreadable", source=source, path=path, reason="invalid_json")
        return None
    if not isinstance(raw, dict):
        diag.debug("usage", "unreadable", source=source, path=path, reason="not_an_object")
        return None
    return raw


def read_claude_rate_limits(path: Path):
    """The Claude source from Claude Code's rate-limit cache, or None when
    the file is missing, unreadable, not a JSON object, or has no window
    with a numeric used_percentage. Windows are copied generically, in the
    file's order: any top-level object with a numeric used_percentage is
    one, labelled from its key (`label_from_key`)."""
    raw = _read_json_object(path, CLAUDE_SOURCE)
    if raw is None:
        return None
    windows = []
    for key, value in raw.items():
        window = _window(value, label_from_key(key))
        if window is not None:
            windows.append(window)
    if not windows:
        diag.debug("usage", "unreadable", source=CLAUDE_SOURCE, path=path, reason="no_windows")
        return None
    return {
        "source": CLAUDE_SOURCE,
        "label": "Claude",
        "updated_at": _time_to_iso(raw.get("cached_at")),
        "origin": _display_path(path),
        "windows": windows[:MAX_WINDOWS],
    }


def read_source_file(path: Path):
    """One source from a drop file `usage/<source>.json`, or None. The
    file's shape (only `windows` is required):

        {"label": "Codex",
         "updated_at": "2026-10-26T12:00:00Z",
         "windows": [{"label": "5h", "used_percentage": 57,
                      "resets_at": "2026-10-26T15:00:00Z"},
                     {"label": "spend", "used_percentage": 63,
                      "used_usd": 314, "limit_usd": 500, "period": "monthly"}]}

    The source name is the file name without `.json`; `label` defaults to
    it. `updated_at` and `resets_at` take ISO 8601 or epoch seconds;
    `updated_at` defaults to the file's modification time. Windows keep
    their order; one without a label or a numeric used_percentage is
    skipped."""
    path = Path(path)
    source = path.stem
    raw = _read_json_object(path, source)
    if raw is None:
        return None
    raw_windows = raw.get("windows")
    windows = []
    for item in raw_windows if isinstance(raw_windows, list) else []:
        window = _window(item, item.get("label") if isinstance(item, dict) else None)
        if window is not None:
            windows.append(window)
    if not windows:
        diag.debug("usage", "unreadable", source=source, path=path, reason="no_windows")
        return None
    updated_at = _time_to_iso(raw.get("updated_at"))
    if updated_at is None:
        try:
            updated_at = _iso(datetime.fromtimestamp(path.stat().st_mtime, timezone.utc))
        except (OSError, OverflowError, ValueError):
            updated_at = None
    return {
        "source": source,
        "label": _clean_label(raw.get("label")) or source,
        "updated_at": updated_at,
        "origin": _display_path(path),
        "windows": windows[:MAX_WINDOWS],
    }


def _newer(candidate, current) -> bool:
    new = _parse_time(candidate.get("updated_at"))
    old = _parse_time(current.get("updated_at"))
    if new is None:
        return False
    return old is None or new > old


def read_usage_sources(home: Path, claude_path: Path, now=None) -> list:
    """Every usage source for the snapshot: Claude's cache first, then the
    drop files by name. Two with the same source name keep the one
    measured most recently; a source older than MAX_AGE is left out.
    Empty when there is none, which is the normal case for many setups."""
    now = now or datetime.now(timezone.utc)
    found = []
    claude = read_claude_rate_limits(claude_path)
    if claude is not None:
        found.append(claude)
    directory = usage_dir(home)
    try:
        paths = sorted(directory.glob("*.json")) if directory.is_dir() else []
    except OSError as exc:
        diag.debug("usage", "unreadable", path=directory, exc=type(exc).__name__)
        paths = []
    for path in paths[: MAX_SOURCES * 2]:
        source = read_source_file(path)
        if source is not None:
            found.append(source)

    by_name = {}
    order = []
    for source in found:
        updated = _parse_time(source.get("updated_at"))
        if updated is not None and now - updated > MAX_AGE:
            diag.debug("usage", "stale_source", source=source["source"],
                       updated_at=source["updated_at"])
            continue
        name = source["source"]
        if name not in by_name:
            by_name[name] = source
            order.append(name)
        elif _newer(source, by_name[name]):
            by_name[name] = source
    return [by_name[name] for name in order][:MAX_SOURCES]


def record_source(home: Path, source: str, data: dict) -> bool:
    """Write `usage/<source>.json` atomically (temp file + rename) unless
    the file already holds numbers measured at the same time or later.
    For AgentBoard's own readers (Codex); other writers follow the same
    shape. Never raises; True when the file was written."""
    directory = usage_dir(home)
    path = directory / f"{source}.json"
    try:
        current = read_source_file(path) if path.exists() else None
        if current is not None and not _newer(data, current):
            return False
        directory.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{source}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError as exc:
        diag.debug("usage", "write_failed", source=source, exc=type(exc).__name__)
        return False
    diag.debug("usage", "recorded", source=source, updated_at=data.get("updated_at"))
    return True


# -- Codex ----------------------------------------------------------------------
#
# A Codex rollout's `token_count` event (codex-rs protocol `TokenCountEvent`)
# carries `rate_limits`, a `RateLimitSnapshot`: `limit_id` ("codex" for the
# plan's own limit), `primary` and `secondary` windows of
# `{used_percent, window_minutes, resets_at}` (epoch seconds; releases before
# 0.50 sent `resets_in_seconds` instead), and optional credits. It is null
# when the account has no plan limits (API-key auth).

CODEX_SOURCE = "codex"


def codex_source(rate_limits, measured_at):
    """The generic source for a Codex `rate_limits` snapshot measured at
    `measured_at` (the event's ISO timestamp), or None when it has no
    usable window or timestamp, or belongs to a limit other than the plan's
    own."""
    if not isinstance(rate_limits, dict):
        return None
    limit_id = rate_limits.get("limit_id")
    if limit_id not in (None, CODEX_SOURCE):
        return None
    measured = _parse_time(measured_at) if isinstance(measured_at, str) else None
    if measured is None:
        return None
    windows = []
    for name in ("primary", "secondary"):
        raw = rate_limits.get(name)
        if not isinstance(raw, dict):
            continue
        resets_at = raw.get("resets_at")
        seconds = raw.get("resets_in_seconds")
        if resets_at is None and _is_number(seconds) and seconds >= 0:
            try:
                resets_at = _iso(measured + timedelta(seconds=seconds))
            except OverflowError:
                resets_at = None
        window = _window(
            {"used_percentage": raw.get("used_percent"), "resets_at": resets_at},
            label_from_minutes(raw.get("window_minutes")) or name,
        )
        if window is not None:
            windows.append(window)
    if not windows:
        return None
    return {
        "label": "Codex",
        "updated_at": _iso(measured),
        "windows": windows,
    }
