"""Which terminal windows are on screen: the check behind `agentboard focus`.

A focus AppleScript can select the right tab and bring the terminal app to
the front while macOS stays on the current desktop (Space), so the window
never appears. This module tells "reached the window" apart from "selected
it, but it is still on another desktop" with public CoreGraphics calls only:
`CGWindowListCopyWindowInfo` lists every window, on every Space, with its
window number, owner pid, layer, bounds and whether it is on screen. None of
that needs the Screen Recording permission; window titles would, and are
never read.

Contract: nothing here raises for a system failure. A probe that cannot run
returns None ("unknown"), which callers never treat as a failure. Only
cmd_focus imports this module, so the hook never loads it.
"""

import ctypes
import subprocess
import sys
import threading
import time
from typing import NamedTuple

from . import diag

# CGWindowListOption values (CGWindow.h).
_OPTION_ALL = 0
_OPTION_ON_SCREEN_ONLY = 1 << 0
_NULL_WINDOW_ID = 0

# How often wait_onscreen re-reads the window list.
POLL_INTERVAL_SECS = 0.05
# The longest one window-list read may take before it counts as "unknown"
# (a wedged window server, or no GUI session at all).
WINDOW_LIST_TIMEOUT_SECS = 1.0
# The least time wait_onscreen gives a read; with less left before its
# deadline it stops on the answers it has. A warm read takes about 5 ms.
MIN_READ_SECS = 0.05
# `defaults read` is local and instant; this only bounds a wedged cfprefsd.
DEFAULTS_TIMEOUT_SECS = 2
# Bounds the `ps` read behind app_pids.
PS_TIMEOUT_SECS = 2

_KEY_NUMBER = "kCGWindowNumber"
_KEY_PID = "kCGWindowOwnerPID"
_KEY_LAYER = "kCGWindowLayer"
_KEY_BOUNDS = "kCGWindowBounds"
_KEY_ONSCREEN = "kCGWindowIsOnscreen"


class WindowState(NamedTuple):
    onscreen: bool
    pid: int | None
    layer: int
    bounds: tuple | None


def _as_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _bounds(value):
    if not isinstance(value, dict):
        return None
    try:
        return tuple(int(value[key]) for key in ("X", "Y", "Width", "Height"))
    except (KeyError, TypeError, ValueError):
        return None


def parse_window_list(entries, onscreen_ids=None, pids=None):
    """`{window number: WindowState}` from CGWindowList-style dicts.

    Only layer-0 windows (ordinary app windows) are kept. A window is on
    screen when its number is in `onscreen_ids` (the OnScreenOnly list), or,
    without that set, when its own `kCGWindowIsOnscreen` says so. `pids`
    keeps only windows those processes own. Malformed entries are skipped.
    """
    wanted = set(pids) if pids else None
    states = {}
    for entry in entries or ():
        if not isinstance(entry, dict):
            continue
        number = _as_int(entry.get(_KEY_NUMBER))
        layer = _as_int(entry.get(_KEY_LAYER))
        if number is None or layer != 0:
            continue
        pid = _as_int(entry.get(_KEY_PID))
        if wanted is not None and pid not in wanted:
            continue
        if onscreen_ids is not None:
            onscreen = number in onscreen_ids
        else:
            onscreen = entry.get(_KEY_ONSCREEN) is True
        states[number] = WindowState(onscreen, pid, layer, _bounds(entry.get(_KEY_BOUNDS)))
    return states


class _CGRect(ctypes.Structure):
    _fields_ = [(name, ctypes.c_double) for name in ("x", "y", "w", "h")]


def _ctypes_window_lists():
    """(all entries, on-screen window numbers) through ctypes, or raises.
    The frameworks load only here, on the first probe."""
    cf = ctypes.cdll.LoadLibrary(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    cg = ctypes.cdll.LoadLibrary(
        "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    vp = ctypes.c_void_p
    cg.CGWindowListCopyWindowInfo.restype = vp
    cg.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    cg.CGRectMakeWithDictionaryRepresentation.restype = ctypes.c_bool
    cg.CGRectMakeWithDictionaryRepresentation.argtypes = [vp, ctypes.POINTER(_CGRect)]
    cf.CFArrayGetCount.restype = ctypes.c_long
    cf.CFArrayGetCount.argtypes = [vp]
    cf.CFArrayGetValueAtIndex.restype = vp
    cf.CFArrayGetValueAtIndex.argtypes = [vp, ctypes.c_long]
    cf.CFDictionaryGetValue.restype = vp
    cf.CFDictionaryGetValue.argtypes = [vp, vp]
    cf.CFStringCreateWithCString.restype = vp
    cf.CFStringCreateWithCString.argtypes = [vp, ctypes.c_char_p, ctypes.c_uint32]
    cf.CFNumberGetValue.restype = ctypes.c_bool
    cf.CFNumberGetValue.argtypes = [vp, ctypes.c_long, vp]
    cf.CFBooleanGetValue.restype = ctypes.c_bool
    cf.CFBooleanGetValue.argtypes = [vp]
    cf.CFGetTypeID.restype = ctypes.c_ulong
    cf.CFGetTypeID.argtypes = [vp]
    cf.CFNumberGetTypeID.restype = ctypes.c_ulong
    cf.CFBooleanGetTypeID.restype = ctypes.c_ulong
    cf.CFDictionaryGetTypeID.restype = ctypes.c_ulong
    cf.CFRelease.argtypes = [vp]
    cf.CFRelease.restype = None

    utf8 = 0x08000100  # kCFStringEncodingUTF8
    sint64 = 4  # kCFNumberSInt64Type
    number_type = cf.CFNumberGetTypeID()
    boolean_type = cf.CFBooleanGetTypeID()
    dict_type = cf.CFDictionaryGetTypeID()
    names = (_KEY_NUMBER, _KEY_PID, _KEY_LAYER, _KEY_BOUNDS, _KEY_ONSCREEN)
    keys = {name: cf.CFStringCreateWithCString(None, name.encode(), utf8) for name in names}

    def value_of(ref):
        if not ref:
            return None
        type_id = cf.CFGetTypeID(ref)
        if type_id == number_type:
            out = ctypes.c_int64()
            return out.value if cf.CFNumberGetValue(ref, sint64, ctypes.byref(out)) else None
        if type_id == boolean_type:
            return bool(cf.CFBooleanGetValue(ref))
        if type_id == dict_type:
            rect = _CGRect()
            if cg.CGRectMakeWithDictionaryRepresentation(ref, ctypes.byref(rect)):
                return {"X": rect.x, "Y": rect.y, "Width": rect.w, "Height": rect.h}
        return None

    def copy_list(option, wanted):
        array = cg.CGWindowListCopyWindowInfo(option, _NULL_WINDOW_ID)
        if not array:
            raise OSError("CGWindowListCopyWindowInfo returned NULL")
        try:
            entries = []
            for i in range(cf.CFArrayGetCount(array)):
                item = cf.CFArrayGetValueAtIndex(array, i)
                entries.append({
                    name: value_of(cf.CFDictionaryGetValue(item, keys[name])) for name in wanted
                })
            return entries
        finally:
            cf.CFRelease(array)

    try:
        all_entries = copy_list(_OPTION_ALL, names)
        onscreen = copy_list(_OPTION_ON_SCREEN_ONLY, (_KEY_NUMBER,))
    finally:
        for ref in keys.values():
            if ref:
                cf.CFRelease(ref)
    onscreen_ids = {entry[_KEY_NUMBER] for entry in onscreen if entry[_KEY_NUMBER] is not None}
    return all_entries, onscreen_ids


class Background:
    """`fn()` running on a daemon thread, started at once.

    `result(timeout)` waits at most `timeout` seconds and returns fn's
    value, or None ("unknown") if it is still running. A call stuck in the
    window server is abandoned, never waited on: the thread is a daemon, so
    it doesn't hold the process open. An exception fn raised is re-raised by
    result() in the caller's thread (window_states and app_pids catch their
    own failures, so that only surfaces bugs and the test guard).
    `thread_factory` replaces threading.Thread in tests.
    """

    def __init__(self, fn, thread_factory=None):
        self._done = threading.Event()
        self._value = None
        self._error = None

        def target():
            try:
                self._value = fn()
            except Exception as exc:  # handed to result()
                self._error = exc
            finally:
                self._done.set()

        self.thread = (thread_factory or threading.Thread)(target=target, daemon=True)
        self.thread.start()

    def result(self, timeout):
        if not self._done.wait(max(0.0, timeout)):
            diag.debug("spaces", "background.timeout", secs=timeout)
            return None
        if self._error is not None:
            raise self._error
        return self._value


def window_states(pids=None):
    """Every ordinary window, keyed by CGWindowID, or None when the window
    list can't be read (not macOS, CoreGraphics unavailable, any error)."""
    if sys.platform != "darwin":
        return None
    started = time.monotonic()
    try:
        entries, onscreen_ids = _ctypes_window_lists()
        states = parse_window_list(entries, onscreen_ids, pids)
    except Exception as exc:  # any failure is "unknown", never a crash
        diag.debug("spaces", "window_states.failed", error=f"{type(exc).__name__}: {exc}")
        return None
    diag.debug("spaces", "window_states", windows=len(states), ms=diag.ms_since(started))
    return states


def bounded_call(fn, timeout, thread_factory=None):
    """`fn()`, or None if it hasn't returned within `timeout` seconds."""
    return Background(fn, thread_factory).result(timeout)


def wait_onscreen(window_id, probe, timeout, sleep=None, clock=None, interval=POLL_INTERVAL_SECS,
                  call_timeout=None, bounded=None):
    """Poll `probe()` until `window_id` is on screen or `timeout` seconds pass.

    Returns `(onscreen, ms)`: True once it is on screen, False if it still
    isn't at the deadline, None when the probe can't tell (it returned None,
    doesn't list that window at all, e.g. the id isn't a CGWindowID, or
    didn't answer in time). Each probe call is bounded by `call_timeout`
    (default WINDOW_LIST_TIMEOUT_SECS) and by what is left of `timeout`, so
    a slow or stuck window server never stretches the wait past `timeout`.
    A read that gets its fair share of time and doesn't answer is unknown;
    one that would get less than MIN_READ_SECS isn't attempted, so the
    off-screen answers already in hand give False. `sleep`, `clock` and `bounded(fn, secs)` default to time.sleep,
    time.monotonic and bounded_call at call time.
    """
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    bounded = bounded or bounded_call
    call_timeout = WINDOW_LIST_TIMEOUT_SECS if call_timeout is None else call_timeout
    start = clock()
    deadline = start + timeout
    budget = min(call_timeout, timeout)
    while True:
        states = bounded(probe, budget)
        now = clock()
        ms = round((now - start) * 1000)
        if states is None:
            return None, ms
        state = states.get(window_id)
        if state is None:
            return None, ms
        if state.onscreen:
            return True, ms
        if now >= deadline:
            return False, ms
        sleep(min(interval, deadline - now))
        now = clock()
        budget = min(call_timeout, deadline - now)
        if budget < MIN_READ_SECS:
            # Too little time left for a read to answer fairly: the reads
            # that did answer (all off screen) decide, rather than a read
            # cut short by the deadline turning them into "unknown".
            return False, round((now - start) * 1000)


# The runners auto_swoosh and app_pids use when none is passed; tests
# replace them.
_run_defaults = subprocess.run
_run_ps = subprocess.run


def app_pids(app_name, run=None):
    """The pids of `<app_name>.app`'s own executables (anything running from
    its `Contents/MacOS/`), read from `ps`, or None when none is running or
    `ps` can't be read. Window ids are trusted only when the window list says
    one of these processes owns the window: an AppleScript window id that
    isn't a CGWindowID could otherwise name another app's window."""
    run = run or _run_ps
    marker = f"/{app_name}.app/Contents/MacOS/"
    try:
        result = run(["ps", "-axo", "pid=,comm="], capture_output=True, text=True,
                     timeout=PS_TIMEOUT_SECS)
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        diag.debug("spaces", "app_pids.failed", error=type(exc).__name__)
        return None
    out = getattr(result, "stdout", "")
    if getattr(result, "returncode", None) != 0 or not isinstance(out, str):
        return None
    pids = set()
    for line in out.splitlines():
        pid, _, command = line.strip().partition(" ")
        if pid.isdigit() and marker in command:
            pids.add(int(pid))
    return pids or None


def auto_swoosh(run=None):
    """System Settings > Desktop & Dock > "When switching to an application,
    switch to a Space with open windows": True (on), False (off), or None
    when it can't be read. A missing key means on, the macOS default."""
    run = run or _run_defaults
    try:
        result = run(
            ["defaults", "read", "com.apple.dock", "workspaces-auto-swoosh"],
            capture_output=True, text=True, timeout=DEFAULTS_TIMEOUT_SECS,
        )
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        diag.debug("spaces", "auto_swoosh.failed", error=type(exc).__name__)
        return None
    code = getattr(result, "returncode", None)
    out = (getattr(result, "stdout", "") or "").strip()
    err = getattr(result, "stderr", "") or ""
    if code == 0:
        return {"1": True, "0": False}.get(out)
    if isinstance(err, str) and "does not exist" in err:
        return True
    return None


def classify_osa_error(stderr):
    """Which permission an osascript failure points at: "automation" (error
    -1743, not allowed to send Apple events), "accessibility" (-25211 or the
    "assistive access" texts), or None. A bare -1719 ("Invalid index") is
    not a permission error."""
    if not isinstance(stderr, str) or not stderr:
        return None
    text = stderr.lower()
    if "-1743" in text or "not authorized to send apple events" in text:
        return "automation"
    if "-25211" in text or "assistive access" in text or "not allowed to send keystrokes" in text:
        return "accessibility"
    return None
