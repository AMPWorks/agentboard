"""Claude desktop app sessions: recognising them and opening them there.

The Claude desktop app runs Claude Code itself, with
$CLAUDE_CODE_ENTRYPOINT set to `claude-desktop`, so its sessions fire the
same hooks as a terminal session but have no terminal to focus. `focus`
opens them in the desktop app instead.

The desktop app keeps its own record of each Claude Code session it runs,
one JSON file per session under
`~/Library/Application Support/Claude/claude-code-sessions/<org>/<account>/`,
whose `cliSessionId` is the Claude Code session id AgentBoard knows and
whose `sessionId` (`local_<id>`) is the app's own id. The app's
`claude://code/continue?session=local_<id>` link opens that session. Both
are internal to the desktop app and may change between versions, so every
use here is read-only, bounded, and falls back to plain app activation
(`open -b com.anthropic.claudefordesktop`). Nothing here runs from
`snapshot` or the hook: only `focus`, at click time.
"""

import json
import os
import re
import time
from pathlib import Path
from urllib.parse import quote

DESKTOP_ENTRYPOINT = "claude-desktop"
DESKTOP_BUNDLE_ID = "com.anthropic.claudefordesktop"
DEEP_LINK_TEMPLATE = "claude://code/continue?session={}"

# The desktop app's own check on the `session` parameter of that link.
LOCAL_SESSION_ID_RE = re.compile(r"local_[A-Za-z0-9-]{1,64}")
# A Claude Code session id worth searching for (a UUID in practice).
CLI_SESSION_ID_RE = re.compile(r"[A-Za-z0-9-]{8,64}")

# Bounds on the store scan: it runs on a click, so it must stay fast even
# if the store grows or holds something unexpected.
MAX_LISTED_FILES = 5000
MAX_READ_FILES = 300
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
SCAN_DEADLINE_SECS = 2.0


def is_desktop(entrypoint) -> bool:
    return entrypoint == DESKTOP_ENTRYPOINT


def store_dir(home: Path | None = None) -> Path:
    base = home if home is not None else Path.home()
    return base / "Library" / "Application Support" / "Claude" / "claude-code-sessions"


def deep_link(local_session_id: str) -> str:
    return DEEP_LINK_TEMPLATE.format(quote(local_session_id, safe=""))


def _session_files(root: Path):
    """`local_*.json` files two directory levels below `root`
    (<org>/<account>/), newest first, at most MAX_LISTED_FILES looked at."""
    found = []
    listed = 0
    try:
        orgs = list(os.scandir(root))
    except OSError:
        return []
    for org in orgs:
        if listed >= MAX_LISTED_FILES:
            break
        try:
            if not org.is_dir(follow_symlinks=False):
                continue
            accounts = list(os.scandir(org.path))
        except OSError:
            continue
        for account in accounts:
            if listed >= MAX_LISTED_FILES:
                break
            try:
                if not account.is_dir(follow_symlinks=False):
                    continue
                entries = list(os.scandir(account.path))
            except OSError:
                continue
            for entry in entries:
                listed += 1
                if listed > MAX_LISTED_FILES:
                    break
                if not (entry.name.startswith("local_") and entry.name.endswith(".json")):
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                found.append((stat.st_mtime, stat.st_size, entry.path))
    found.sort(reverse=True)
    return found


def find_local_session(cli_session_id: str, home: Path | None = None, clock=time.monotonic):
    """(local session id, reason) for the desktop app's record of
    `cli_session_id`. The id is None when there is none to open, and reason
    says why: `found`, `bad_id`, `no_store`, `not_found`, `archived` or
    `scan_limit` (a bound was hit before a match). Never raises."""
    try:
        if not isinstance(cli_session_id, str) or not CLI_SESSION_ID_RE.fullmatch(cli_session_id):
            return None, "bad_id"
        root = store_dir(home)
        if not root.is_dir():
            return None, "no_store"
        needle = cli_session_id.encode()
        started = clock()
        total = 0
        files = _session_files(root)
        for index, (_mtime, size, path) in enumerate(files):
            if index >= MAX_READ_FILES or clock() - started > SCAN_DEADLINE_SECS:
                return None, "scan_limit"
            if size > MAX_FILE_BYTES:
                continue
            if total + size > MAX_TOTAL_BYTES:
                return None, "scan_limit"
            try:
                with open(path, "rb") as fh:
                    data = fh.read(MAX_FILE_BYTES + 1)
            except OSError:
                continue
            total += len(data)
            if needle not in data or len(data) > MAX_FILE_BYTES:
                continue
            try:
                record = json.loads(data)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            if cli_session_id not in (record.get("cliSessionId"),
                                      record.get("lastAssistantCliSessionId")):
                continue
            local_id = record.get("sessionId")
            if not isinstance(local_id, str) or not LOCAL_SESSION_ID_RE.fullmatch(local_id):
                continue
            if record.get("isArchived") is True:
                return None, "archived"
            return local_id, "found"
        return None, "not_found"
    except Exception:  # noqa: BLE001 - a click must never fail on the store
        return None, "error"
