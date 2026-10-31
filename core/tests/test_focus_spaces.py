"""`agentboard focus` across desktops (Spaces): the window probe helpers in
spaces.py and how cmd_focus turns "the script matched, but is the window on
screen?" into an outcome, a log line and an exit code.

Nothing here runs an AppleScript, focuses a window or reads the user's
defaults: the osascript runner, the window probe, the `defaults` runner and
sleep/clock are fakes. The one exception is test_window_states_smoke, which
lists windows (read-only, no titles) in a subprocess.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentboard import cli, diag, spaces  # noqa: E402
from tests.helpers import AgentBoardTestCase  # noqa: E402

CORE_DIR = Path(__file__).resolve().parent.parent


def _entry(number, pid=100, layer=0, onscreen=None, bounds=(0, 0, 800, 600)):
    entry = {"kCGWindowNumber": number, "kCGWindowOwnerPID": pid, "kCGWindowLayer": layer}
    if bounds is not None:
        x, y, w, h = bounds
        entry["kCGWindowBounds"] = {"X": x, "Y": y, "Width": w, "Height": h}
    if onscreen is not None:
        entry["kCGWindowIsOnscreen"] = onscreen
    return entry


def _states(**windows):
    """`_states(w12345=True, w678=False)` -> {12345: on screen, 678: off}."""
    return {
        int(name[1:]): spaces.WindowState(onscreen, 100, 0, (0, 0, 800, 600))
        for name, onscreen in windows.items()
    }


class FakeClock:
    """A clock that only moves when the fake sleep is called."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class PidProbe:
    """A window probe over CGWindowList-style entries that honours `pids`,
    like spaces.window_states."""

    def __init__(self, entries, onscreen_ids):
        self.entries, self.onscreen_ids = entries, onscreen_ids
        self.calls = []

    def __call__(self, pids=None):
        self.calls.append(pids)
        return spaces.parse_window_list(self.entries, self.onscreen_ids, pids)


class FakeProbe:
    """Returns each queued window list in turn, then repeats the last."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    def __call__(self, pids=None):
        self.calls += 1
        index = min(self.calls - 1, len(self.results) - 1)
        return self.results[index]


class FakeThreads:
    """A threading.Thread stand-in. The threads started at the 1-based
    positions in `defer` (an int means the first N) are held until
    run_held(), like a thread still working; the others run their target
    inside start(), so nothing races and nothing sleeps. cmd_focus starts
    the pid lookup first and the window-list read second."""

    def __init__(self, defer=0):
        self.defer = set(range(1, defer + 1)) if isinstance(defer, int) else set(defer)
        self.started = 0
        self.held = []

    def __call__(self, target, daemon=None, **kwargs):
        threads = self

        class Thread:
            def start(self):
                threads.started += 1
                if threads.started in threads.defer:
                    threads.held.append(target)
                else:
                    target()

        return Thread()

    def run_held(self):
        held, self.held = self.held, []
        for target in held:
            target()


class SlowCalls:
    """A `bounded(fn, secs)` stand-in for wait_onscreen on the fake clock:
    each probe call "takes" the next duration; one longer than its budget
    times out (the clock moves by the budget, the answer is None)."""

    def __init__(self, fake_clock, *durations):
        self.time = fake_clock
        self.durations = list(durations)
        self.budgets = []

    def __call__(self, fn, secs):
        self.budgets.append(round(secs, 6))
        took = self.durations[min(len(self.budgets) - 1, len(self.durations) - 1)]
        if took > secs:
            self.time.now += secs
            return None
        self.time.now += took
        return fn()


class RecordingThreads:
    """Real daemon threads, remembered so a test can join them."""

    def __init__(self):
        self.threads = []

    def __call__(self, target, daemon=None, **kwargs):
        import threading
        thread = threading.Thread(target=target, daemon=daemon, **kwargs)
        self.threads.append(thread)
        return thread

    def join(self):
        for thread in self.threads:
            thread.join(5)


def _completed(stdout="", returncode=0, stderr="", args=None):
    return subprocess.CompletedProcess(args or ["osascript"], returncode, stdout, stderr)


class ParseWindowListTests(unittest.TestCase):
    def test_onscreen_offscreen_and_minimized_from_the_onscreen_set(self):
        entries = [_entry(1), _entry(2), _entry(3, bounds=None)]
        states = spaces.parse_window_list(entries, onscreen_ids={1})
        self.assertTrue(states[1].onscreen)
        self.assertFalse(states[2].onscreen)  # another desktop
        self.assertFalse(states[3].onscreen)  # minimized: listed, never on screen
        self.assertEqual(states[1].bounds, (0, 0, 800, 600))
        self.assertIsNone(states[3].bounds)
        self.assertEqual(states[1].pid, 100)

    def test_falls_back_to_the_entry_flag_without_an_onscreen_set(self):
        states = spaces.parse_window_list([_entry(1, onscreen=True), _entry(2, onscreen=False), _entry(3)])
        self.assertEqual({k: v.onscreen for k, v in states.items()}, {1: True, 2: False, 3: False})

    def test_pids_keep_only_those_processes(self):
        states = spaces.parse_window_list([_entry(1, pid=100), _entry(2, pid=200)], {1, 2}, pids=[200])
        self.assertEqual(list(states), [2])

    def test_non_zero_layers_are_skipped(self):
        states = spaces.parse_window_list([_entry(1, layer=25), _entry(2, layer=0)], {1, 2})
        self.assertEqual(list(states), [2])

    def test_malformed_entries_are_ignored(self):
        entries = [
            None, "x", {}, {"kCGWindowNumber": "7", "kCGWindowLayer": 0},
            {"kCGWindowNumber": True, "kCGWindowLayer": 0},
            {"kCGWindowNumber": 8},  # no layer
            _entry(9, bounds=None) | {"kCGWindowBounds": {"X": "a"}},
            _entry(10),
        ]
        states = spaces.parse_window_list(entries, {10})
        self.assertEqual(sorted(states), [9, 10])
        self.assertIsNone(states[9].bounds)
        self.assertEqual(spaces.parse_window_list(None), {})


class WaitOnscreenTests(unittest.TestCase):
    def setUp(self):
        self.time = FakeClock()
        patcher = mock.patch.object(time, "sleep", side_effect=AssertionError("real sleep"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def wait(self, probe, window_id=5, timeout=1.5):
        return spaces.wait_onscreen(window_id, probe, timeout, sleep=self.time.sleep, clock=self.time.clock)

    def test_onscreen_at_once(self):
        probe = FakeProbe(_states(w5=True))
        self.assertEqual(self.wait(probe), (True, 0))
        self.assertEqual(probe.calls, 1)
        self.assertEqual(self.time.sleeps, [])

    def test_onscreen_after_some_polls(self):
        probe = FakeProbe(_states(w5=False), _states(w5=False), _states(w5=False), _states(w5=True))
        onscreen, ms = self.wait(probe)
        self.assertTrue(onscreen)
        self.assertEqual(probe.calls, 4)
        self.assertEqual(len(self.time.sleeps), 3)
        self.assertEqual(ms, 150)

    def test_never_onscreen_within_the_timeout(self):
        probe = FakeProbe(_states(w5=False))
        onscreen, ms = self.wait(probe, timeout=0.5)
        self.assertIs(onscreen, False)
        self.assertGreaterEqual(ms, 500)
        self.assertLess(ms, 600)

    def test_a_probe_that_cannot_tell_gives_none(self):
        self.assertEqual(self.wait(FakeProbe(None))[0], None)

    def test_a_window_the_probe_does_not_list_gives_none(self):
        # e.g. an `id of window` that is not a CGWindowID: never a false failure.
        self.assertEqual(self.wait(FakeProbe(_states(w6=False)))[0], None)

    def test_a_stuck_probe_is_unknown_after_the_call_timeout(self):
        calls = SlowCalls(self.time, float("inf"))
        result = spaces.wait_onscreen(5, FakeProbe(_states(w5=False)), 1.5, sleep=self.time.sleep,
                                      clock=self.time.clock, bounded=calls)
        self.assertEqual(result, (None, round(spaces.WINDOW_LIST_TIMEOUT_SECS * 1000)))
        self.assertEqual(calls.budgets, [spaces.WINDOW_LIST_TIMEOUT_SECS])

    def test_a_slow_probe_never_stretches_the_wait_past_its_budget(self):
        # Each read takes 0.6 s: the budget left for each shrinks, and the
        # whole check still ends at 1.5 s.
        calls = SlowCalls(self.time, 0.6)
        onscreen, ms = spaces.wait_onscreen(5, FakeProbe(_states(w5=False)), 1.5, sleep=self.time.sleep,
                                            clock=self.time.clock, call_timeout=1.0, bounded=calls)
        self.assertIsNone(onscreen)  # the last read didn't answer in time: unknown, not a failure
        self.assertEqual(ms, 1500)
        self.assertEqual(calls.budgets, [1.0, 0.85, 0.2])

    def test_a_read_cut_short_by_the_deadline_does_not_hide_an_offscreen_window(self):
        # 5 ms reads: after the second sleep only 2 ms are left, too little
        # for a read to answer. The off-screen answers already in hand
        # decide (space_not_reached), not a read that never had a chance.
        calls = SlowCalls(self.time, 0.005)
        onscreen, ms = spaces.wait_onscreen(5, FakeProbe(_states(w5=False)), 0.112, sleep=self.time.sleep,
                                            clock=self.time.clock, bounded=calls)
        self.assertIs(onscreen, False)
        self.assertEqual(ms, 110)
        self.assertEqual(calls.budgets, [0.112, 0.057])
        self.assertTrue(all(budget >= spaces.MIN_READ_SECS for budget in calls.budgets))

    def test_a_probe_that_never_answers_is_unknown_whatever_the_budgets(self):
        for timeout, call_timeout in ((1.5, 1.0), (0.3, 1.0), (1.5, 0.06), (0.06, 0.06)):
            with self.subTest(timeout=timeout, call_timeout=call_timeout):
                self.time = FakeClock()
                calls = SlowCalls(self.time, float("inf"))
                onscreen, _ = spaces.wait_onscreen(5, FakeProbe(_states(w5=False)), timeout,
                                                   sleep=self.time.sleep, clock=self.time.clock,
                                                   call_timeout=call_timeout, bounded=calls)
                self.assertIsNone(onscreen)
                self.assertEqual(len(calls.budgets), 1)

    def test_the_default_bound_runs_the_probe_on_a_thread(self):
        threads = RecordingThreads()
        probe = FakeProbe(_states(w5=True))
        result = spaces.wait_onscreen(5, probe, 1.5, sleep=self.time.sleep, clock=self.time.clock,
                                      bounded=lambda fn, secs: spaces.bounded_call(fn, secs, threads))
        self.assertEqual(result, (True, 0))
        self.assertEqual(len(threads.threads), 1)


class AutoSwooshTests(unittest.TestCase):
    def run_with(self, result=None, raises=None):
        run = mock.Mock(return_value=result, side_effect=raises)
        value = spaces.auto_swoosh(run)
        argv = run.call_args[0][0]
        self.assertEqual(argv, ["defaults", "read", "com.apple.dock", "workspaces-auto-swoosh"])
        self.assertEqual(run.call_args.kwargs["timeout"], spaces.DEFAULTS_TIMEOUT_SECS)
        return value

    def test_missing_key_means_on(self):
        err = "The domain/default pair of (com.apple.dock, workspaces-auto-swoosh) does not exist\n"
        self.assertIs(self.run_with(_completed("", 1, err)), True)

    def test_zero_means_off_and_one_means_on(self):
        self.assertIs(self.run_with(_completed("0\n")), False)
        self.assertIs(self.run_with(_completed("1\n")), True)

    def test_timeout_or_garbage_is_unknown(self):
        self.assertIsNone(self.run_with(raises=subprocess.TimeoutExpired(["defaults"], 2)))
        self.assertIsNone(self.run_with(raises=OSError("no defaults")))
        self.assertIsNone(self.run_with(_completed("maybe\n")))
        self.assertIsNone(self.run_with(_completed("", 1, "something else")))


class ClassifyOsaErrorTests(unittest.TestCase):
    def test_classification(self):
        cases = {
            "execution error: Not authorized to send Apple events to iTerm. (-1743)": "automation",
            "System Events got an error: osascript is not allowed assistive access. (-25211)": "accessibility",
            "execution error: Invalid index. (-1719)": None,
            "execution error: osascript is not allowed assistive access. (-1719)": "accessibility",
            "osascript is not allowed to send keystrokes. (-1002)": "accessibility",
            "execution error: Can't get window 1. (-1728)": None,
            "": None,
            None: None,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(spaces.classify_osa_error(text), expected)


class ParseFocusReplyTests(unittest.TestCase):
    def test_replies(self):
        iterm, terminal = cli._ITERM_FOCUS_RESULTS, cli._TERMINAL_FOCUS_RESULTS
        R = cli._FocusReply
        cases = [
            ("found:uuid 12345", iterm, R("matched_uuid", 12345)),
            ("found:uuid 12345 min\n", iterm, R("matched_uuid", 12345, minimized=True)),
            ("found:tty 9", iterm, R("matched_tty", 9)),
            ("found:uuid", iterm, R("matched_uuid")),  # an older installed script
            ("found", iterm, R("matched_unknown")),
            ("found:uuid hotkey", iterm, R("matched_uuid", hotkey=True)),
            ("found:uuid 12345 min moved", iterm, R("matched_uuid", 12345, minimized=True, reselect_skipped=True)),
            ("found:tty hotkey moved", iterm, R("matched_tty", hotkey=True, reselect_skipped=True)),
            ("found 678 moved", terminal, R("matched_tty", 678, reselect_skipped=True)),
            ("found 678", terminal, R("matched_tty", 678)),
            ("found", terminal, R("matched_tty")),
            ("activated", terminal, R("activated_only")),
            ("not_found", iterm, None),
            ("", iterm, None),
            ("garbage 12", iterm, None),
            ("found:uuid abc", iterm, R("matched_uuid")),
        ]
        for out, table, expected in cases:
            with self.subTest(out=out):
                self.assertEqual(cli._parse_focus_reply(out, table), expected)


class SpaceBeforeTests(unittest.TestCase):
    def test_space_before(self):
        reply = cli._FocusReply("matched_uuid", 5)
        app = {100}
        self.assertEqual(cli._space_before(reply, _states(w5=True), app), "current")
        self.assertEqual(cli._space_before(reply, _states(w5=False), app), "other")
        self.assertEqual(cli._space_before(reply._replace(minimized=True), _states(w5=True), app), "minimized")
        self.assertEqual(cli._space_before(reply, None, app), "unknown")
        self.assertEqual(cli._space_before(reply, _states(w6=True), app), "unknown")

    def test_a_window_another_process_owns_is_unknown(self):
        reply = cli._FocusReply("matched_tty", 5)
        self.assertEqual(cli._space_before(reply, _states(w5=True), {999}), "unknown")
        self.assertEqual(cli._space_before(reply, _states(w5=True), None), "unknown")


class AppPidsTests(unittest.TestCase):
    PS = (
        "    1 /sbin/launchd\n"
        " 1801 /Applications/iTerm.app/Contents/MacOS/iTerm2\n"
        " 1802 /Applications/iTerm.app/Contents/MacOS/iTermServer\n"
        " 1855 /Applications/iTerm.app/Contents/XPCServices/pidinfo.xpc/Contents/MacOS/pidinfo\n"
        " 2000 /System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal\n"
        " 2100 /Users/me/Apps/My Tools/iTerm.app/Contents/MacOS/iTerm2\n"
        "garbage line\n"
    )

    def run_with(self, app, result=None, raises=None):
        run = mock.Mock(return_value=result, side_effect=raises)
        pids = spaces.app_pids(app, run)
        self.assertEqual(run.call_args[0][0], ["ps", "-axo", "pid=,comm="])
        self.assertEqual(run.call_args.kwargs["timeout"], spaces.PS_TIMEOUT_SECS)
        return pids

    def test_the_apps_own_executables(self):
        self.assertEqual(self.run_with("iTerm", _completed(self.PS)), {1801, 1802, 2100})
        self.assertEqual(self.run_with("Terminal", _completed(self.PS)), {2000})

    def test_not_running_or_unreadable_is_none(self):
        self.assertIsNone(self.run_with("Ghostty", _completed(self.PS)))
        self.assertIsNone(self.run_with("iTerm", _completed("", 1, "ps: denied")))
        self.assertIsNone(self.run_with("iTerm", raises=subprocess.TimeoutExpired(["ps"], 2)))
        self.assertIsNone(self.run_with("iTerm", raises=OSError("no ps")))


class BackgroundTests(unittest.TestCase):
    def test_returns_the_value(self):
        self.assertEqual(spaces.Background(lambda: 42, FakeThreads()).result(1), 42)

    def test_a_call_still_running_is_unknown(self):
        threads = FakeThreads(defer=1)
        call = spaces.Background(lambda: 42, threads)
        self.assertIsNone(call.result(0))
        threads.run_held()
        self.assertEqual(call.result(0), 42)

    def test_an_exception_is_raised_in_the_caller(self):
        def boom():
            raise AssertionError("unmocked window_states")
        call = spaces.Background(boom, FakeThreads())
        with self.assertRaises(AssertionError):
            call.result(1)

    def test_the_thread_is_a_daemon(self):
        seen = {}

        def factory(target, daemon=None):
            seen["daemon"] = daemon
            return FakeThreads()(target)

        spaces.Background(lambda: None, factory)
        self.assertIs(seen["daemon"], True)


class BoundedCallTests(unittest.TestCase):
    def test_a_call_that_blocks_is_abandoned_after_the_timeout(self):
        import threading
        release = threading.Event()
        threads = RecordingThreads()
        self.addCleanup(threads.join)
        self.addCleanup(release.set)
        started = time.monotonic()
        self.assertIsNone(spaces.bounded_call(lambda: release.wait(5), 0.05, threads))
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(threads.threads[0].daemon)


class WindowStatesSmokeTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin", "macOS window list")
    def test_window_states_smoke(self):
        # Read-only: lists windows (numbers, pids, bounds; never titles) in a
        # child process. Never focuses, moves or closes anything.
        code = (
            "import json, os, sys\n"
            f"sys.path.insert(0, {str(CORE_DIR)!r})\n"
            "from agentboard import spaces\n"
            "result = spaces.window_states(pids=[os.getpid()])\n"
            "ok = result is None or (isinstance(result, dict) and all(\n"
            "    isinstance(k, int) and isinstance(v, spaces.WindowState) for k, v in result.items()))\n"
            "print(json.dumps({'type': type(result).__name__, 'ok': ok}))\n"
        )
        with contextlib.ExitStack() as stack:
            import tempfile
            home = stack.enter_context(tempfile.TemporaryDirectory())
            env = dict(os.environ, HOME=home, AGENTBOARD_HOME=home)
            proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                                  text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertIn(result["type"], ("dict", "NoneType"))
        self.assertTrue(result["ok"])


class FocusSpacesTests(AgentBoardTestCase):
    """cmd_focus outcome mapping, one test per row of the plan's table."""

    def setUp(self):
        super().setUp()
        self.time = FakeClock()
        self.stderr = io.StringIO()
        for patcher in (
            mock.patch.object(time, "sleep", side_effect=AssertionError("real sleep")),
            mock.patch("sys.stderr", self.stderr),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def insert(self, term="iTerm.app", tty="ttys005", iterm_id="w0t0p0:ABCD"):
        self.conn.execute(
            "INSERT INTO sessions(tool, session_id, cwd, started_at, updated_at, last_event, "
            "last_event_at, origin, interactive, term_program, tty, iterm_session_id) "
            "VALUES ('claude', 's1', '/tmp/p', '2026-06-15T10:00:00.000000Z', "
            "'2026-06-15T10:00:00.000000Z', 'Stop', '2026-06-15T10:00:00.000000Z', 'hook', 1, ?, ?, ?)",
            (term, tty, iterm_id),
        )

    def focus(self, *results, probe=None, defaults=None, threads=None, pids=frozenset({100})):
        """Run cmd_focus with `results` as the osascript/open answers (a
        callable result is called with the runner's arguments)."""
        queue = list(results)

        def answer(*args, **kwargs):
            result = queue.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result(*args, **kwargs) if callable(result) else result

        self.run = mock.Mock(side_effect=answer)
        self.threads = threads if threads is not None else FakeThreads()
        self.find_pids = mock.Mock(return_value=set(pids) if pids else None)
        self.probe = probe if probe is not None else FakeProbe(None)
        self.defaults = mock.Mock(return_value=defaults) if defaults is not None else mock.Mock(
            side_effect=AssertionError("defaults read on a non-failure path"))
        args = cli.build_parser().parse_args(["focus", "claude", "s1"])
        return cli.cmd_focus(
            args, run_osascript=self.run, window_probe=self.probe, run_defaults=self.defaults,
            sleep=self.time.sleep, clock=self.time.clock, thread_factory=self.threads,
            find_app_pids=self.find_pids,
        )

    def focus_line(self, level):
        path = self.agentboard_home / "agentboard.log"
        lines = [l for l in path.read_text().splitlines() if " cli.focus " in l]
        self.assertEqual(len(lines), 1, path.read_text())
        self.assertIn(f" {level} cli.focus ", lines[0])
        return lines[0]

    def assert_failure_line(self, outcome):
        """A failure writes one WARN cli.focus line whose msg= is the stderr line."""
        line = self.focus_line("WARN")
        self.assertIn(f"outcome={outcome}", line)
        message = self.stderr.getvalue().strip()
        self.assertTrue(message)
        self.assertEqual(len(message.splitlines()), 1)
        self.assertIn(f"msg={diag._format_value(message)} ", line)
        return line, message

    def test_uuid_match_onscreen_is_reached(self):
        self.insert()
        probe = FakeProbe(_states(w12345=False), _states(w12345=False), _states(w12345=True))
        self.assertEqual(self.focus(_completed("found:uuid 12345\n"), probe=probe), 0)
        line = self.focus_line("INFO")
        self.assertIn("outcome=matched_uuid", line)
        self.assertIn("window_id=12345", line)
        self.assertIn("app_pid=100", line)
        self.assertIn("space_before=other", line)
        self.assertIn("onscreen_after=true", line)
        self.assertIn("onscreen_ms=50", line)
        self.find_pids.assert_called_once_with("iTerm")
        self.assertNotIn("swoosh=", line)
        self.assertNotIn("reselect_skipped", line)
        self.defaults.assert_not_called()
        self.assertEqual(self.stderr.getvalue(), "")

    def test_a_skipped_reselect_is_recorded_on_the_one_line(self):
        # The tab moved or closed between the scan and the select: the
        # window still came forward, but the log must not read as a clean match.
        self.insert()
        probe = FakeProbe(_states(w12345=True))
        self.assertEqual(self.focus(_completed("found:uuid 12345 moved\n"), probe=probe), 0)
        line = self.focus_line("INFO")
        self.assertIn("outcome=matched_uuid", line)
        self.assertIn("reselect_skipped=1", line)
        self.assertIn("onscreen_after=true", line)

    def test_a_skipped_reselect_is_recorded_for_a_hotkey_window(self):
        self.insert()
        self.assertEqual(self.focus(_completed("found:uuid hotkey moved\n")), 0)
        self.assertIn("reselect_skipped=1", self.focus_line("INFO"))

    def test_a_skipped_reselect_is_recorded_for_apple_terminal(self):
        self.insert(term="Apple_Terminal", iterm_id=None)
        probe = FakeProbe(_states(w678=True))
        self.assertEqual(self.focus(_completed("found 678 moved\n"), probe=probe), 0)
        line = self.focus_line("INFO")
        self.assertIn("outcome=matched_tty", line)
        self.assertIn("reselect_skipped=1", line)

    def test_tty_match_onscreen_on_the_same_desktop(self):
        self.insert()
        probe = FakeProbe(_states(w9=True))
        self.assertEqual(self.focus(_completed("found:tty 9\n"), probe=probe), 0)
        line = self.focus_line("INFO")
        self.assertIn("outcome=matched_tty", line)
        self.assertIn("space_before=current", line)
        self.assertIn("onscreen_ms=0", line)
        self.assertEqual(self.time.sleeps, [])

    def test_minimized_window_reports_minimized(self):
        self.insert()
        probe = FakeProbe(_states(w7=False), _states(w7=True))
        self.assertEqual(self.focus(_completed("found:uuid 7 min\n"), probe=probe), 0)
        self.assertIn("space_before=minimized", self.focus_line("INFO"))

    def test_still_offscreen_with_the_setting_off_is_space_not_reached(self):
        self.insert()
        probe = FakeProbe(_states(w12345=False))
        code = self.focus(_completed("found:uuid 12345\n"), probe=probe, defaults=_completed("0\n"))
        self.assertEqual(code, cli.FOCUS_EXIT_SPACE_NOT_REACHED)
        line, message = self.assert_failure_line("space_not_reached")
        self.assertIn("swoosh=off", line)
        self.assertIn("onscreen_after=false", line)
        self.assertIn("When switching to an application, switch to a Space with open windows", message)
        self.defaults.assert_called_once()
        # The wait is bounded by FOCUS_ONSCREEN_WAIT_SECS, on the fake clock.
        self.assertAlmostEqual(sum(self.time.sleeps), cli.FOCUS_ONSCREEN_WAIT_SECS, delta=0.06)

    def test_still_offscreen_with_the_setting_on_points_at_mission_control(self):
        self.insert()
        probe = FakeProbe(_states(w12345=False))
        missing = _completed("", 1, "The domain/default pair of (com.apple.dock, workspaces-auto-swoosh) does not exist")
        code = self.focus(_completed("found:uuid 12345\n"), probe=probe, defaults=missing)
        self.assertEqual(code, 5)
        line, message = self.assert_failure_line("space_not_reached")
        self.assertIn("swoosh=on", line)
        self.assertIn("Mission Control", message)

    def test_unknown_probe_keeps_the_match(self):
        self.insert()
        self.assertEqual(self.focus(_completed("found:uuid 12345\n"), probe=FakeProbe(None)), 0)
        line = self.focus_line("INFO")
        self.assertIn("outcome=matched_uuid", line)
        self.assertIn("space_before=unknown", line)
        self.assertIn("onscreen_after=-", line)

    def test_hotkey_window_skips_the_check(self):
        self.insert()
        probe = FakeProbe(_states(w1=False))
        self.assertEqual(self.focus(_completed("found:uuid hotkey\n"), probe=probe), 0)
        line = self.focus_line("INFO")
        self.assertIn("window_id=hotkey", line)
        self.assertNotIn("onscreen_after", line)
        self.assertEqual(probe.calls, 1)  # only the pre-probe

    def test_iterm_automation_denied_still_activates_and_exits_4(self):
        self.insert()
        code = self.focus(
            _completed("", 1, "execution error: Not authorized to send Apple events to iTerm. (-1743)"),
            _completed("", args=["open"]),
        )
        self.assertEqual(code, cli.FOCUS_EXIT_NEEDS_AUTOMATION)
        self.assertEqual(self.run.call_args_list[1][0][0], ["open", "-a", "iTerm"])
        _, message = self.assert_failure_line("needs_automation")
        self.assertIn("iTerm", message)
        self.assertIn("Automation", message)

    def test_terminal_automation_denied_exits_4_naming_terminal(self):
        self.insert(term="Apple_Terminal", iterm_id=None)
        code = self.focus(_completed("", 1, "Not authorized to send Apple events to Terminal. (-1743)"))
        self.assertEqual(code, 4)
        self.run.assert_called_once()
        _, message = self.assert_failure_line("needs_automation")
        self.assertIn("control Terminal", message)

    def test_generic_terminal_open_a_is_unchanged(self):
        self.insert(term="WarpTerminal", iterm_id=None)
        self.assertEqual(self.focus(_completed("", args=["open"])), 0)
        self.assertIn("outcome=activated_only", self.focus_line("INFO"))
        self.assertEqual(self.probe.calls, 0)

    def test_iterm_tab_missed_is_exit_1_with_a_message(self):
        self.insert()
        self.assertEqual(self.focus(_completed("not_found\n"), _completed("", args=["open"])), 1)
        _, message = self.assert_failure_line("activated_only")
        self.assertIn("Couldn't find this session's tab in iTerm", message)
        self.assertIn("Brought iTerm to the front", message)

    def test_iterm_activate_failed_is_exit_1(self):
        self.insert()
        code = self.focus(_completed("", 1, "boom"), _completed("", 1, args=["open"]))
        self.assertEqual(code, 1)
        self.assert_failure_line("activate_failed")

    def test_terminal_found_onscreen_is_matched_tty(self):
        self.insert(term="Apple_Terminal", iterm_id=None)
        probe = FakeProbe(_states(w678=False), _states(w678=True))
        self.assertEqual(self.focus(_completed("found 678\n"), probe=probe), 0)
        line = self.focus_line("INFO")
        self.assertIn("outcome=matched_tty", line)
        self.assertIn("window_id=678", line)

    def test_terminal_activated_only_is_exit_1(self):
        self.insert(term="Apple_Terminal", iterm_id=None)
        self.assertEqual(self.focus(_completed("activated\n")), 1)
        self.assert_failure_line("activated_only")

    def test_terminal_script_error_is_not_found_exit_1(self):
        self.insert(term="Apple_Terminal", iterm_id=None)
        self.assertEqual(self.focus(_completed("", 1, "execution error (-1728)")), 1)
        self.assert_failure_line("not_found")

    def test_timeout_makes_no_probe_or_defaults_call_after_the_script(self):
        self.insert()
        run_timeout = subprocess.TimeoutExpired(["osascript"], cli.OSASCRIPT_TIMEOUT_SECS)
        probe = FakeProbe(_states(w1=False))
        self.assertEqual(self.focus(run_timeout, probe=probe), 1)
        self.assertEqual(probe.calls, 1)  # the pre-probe only
        self.defaults.assert_not_called()
        line, message = self.assert_failure_line("timeout")
        self.assertIn("timed out", message)

    def test_no_terminal_is_one_warn_line_matching_stderr(self):
        self.insert(term=None, tty=None, iterm_id=None)
        self.assertEqual(self.focus(), 1)
        self.assert_failure_line("no_terminal")
        self.assertEqual(self.probe.calls, 0)

    def test_begin_is_logged_before_the_probe_and_the_script(self):
        self.insert()
        seen = []
        path = self.agentboard_home / "agentboard.log"

        def probe(pids=None):
            seen.append(path.read_text() if path.exists() else "")
            return None

        self.assertEqual(self.focus(_completed("found:uuid 3\n"), probe=probe), 0)
        self.assertIn(" INFO cli.focus.begin ", seen[0])
        self.assertNotIn(" cli.focus key=", seen[0])

    def test_the_pre_probe_runs_alongside_the_script_not_before_it(self):
        # A same-desktop click must not wait for a cold window-list read
        # before the script even starts.
        self.insert()
        threads = FakeThreads(defer=2)
        probe = FakeProbe(_states(w12345=False), _states(w12345=True))
        script_saw = {}

        def script(*args, **kwargs):
            script_saw["started"] = threads.started
            script_saw["probe_calls"] = probe.calls
            script_saw["pid_lookups"] = self.find_pids.call_count
            threads.run_held()  # both reads finish while the script runs
            return _completed("found:uuid 12345\n")

        code = self.focus(script, probe=probe, threads=threads)
        self.assertEqual(code, 0)
        self.assertEqual(script_saw, {"started": 2, "probe_calls": 0, "pid_lookups": 0})
        line = self.focus_line("INFO")
        self.assertIn("space_before=other", line)
        self.assertIn("onscreen_after=true", line)

    def test_a_pre_probe_still_running_after_the_script_is_unknown(self):
        self.insert()
        threads = FakeThreads(defer={2})  # the window-list read never finishes
        probe = FakeProbe(_states(w12345=True))
        with mock.patch.object(cli, "FOCUS_PRE_PROBE_JOIN_SECS", 0):
            code = self.focus(_completed("found:uuid 12345\n"), probe=probe, threads=threads)
        self.assertEqual(code, 0)
        line = self.focus_line("INFO")
        self.assertIn("space_before=unknown", line)
        self.assertIn("onscreen_after=true", line)

    def test_a_pid_lookup_still_running_after_the_script_is_unknown(self):
        self.insert()
        threads = FakeThreads(defer={1})  # `ps` never answers
        probe = FakeProbe(_states(w12345=False))
        with mock.patch.object(cli, "FOCUS_PRE_PROBE_JOIN_SECS", 0):
            code = self.focus(_completed("found:uuid 12345\n"), probe=probe, threads=threads)
        self.assertEqual(code, 0)
        line = self.focus_line("INFO")
        self.assertIn("space_before=unknown", line)
        self.assertIn("onscreen_after=-", line)
        self.assertEqual(probe.calls, 1)  # the pre-probe only: no post-check without pids

    def test_a_window_id_another_app_owns_is_unknown_not_space_not_reached(self):
        # Apple Terminal's AppleScript window id was never checked against
        # the CGWindowID. If it collides with another app's window number,
        # that window's state must not decide the outcome.
        self.insert(term="Apple_Terminal", iterm_id=None)
        probe = PidProbe([_entry(678, pid=999), _entry(41, pid=100)], onscreen_ids={41})
        code = self.focus(_completed("found 678\n"), probe=probe)
        self.assertEqual(code, 0)
        line = self.focus_line("INFO")
        self.assertIn("outcome=matched_tty", line)
        self.assertIn("window_id=678", line)
        self.assertIn("space_before=unknown", line)
        self.assertIn("onscreen_after=-", line)
        self.assertEqual(probe.calls[1:], [{100}])  # the post-check lists only the app's windows
        self.find_pids.assert_called_once_with("Terminal")
        self.defaults.assert_not_called()
        self.assertEqual(self.stderr.getvalue(), "")

    def test_a_window_the_app_owns_is_still_judged(self):
        self.insert(term="Apple_Terminal", iterm_id=None)
        probe = PidProbe([_entry(678, pid=100), _entry(41, pid=999)], onscreen_ids={41})
        code = self.focus(_completed("found 678\n"), probe=probe, defaults=_completed("1\n"))
        self.assertEqual(code, cli.FOCUS_EXIT_SPACE_NOT_REACHED)
        line, _ = self.assert_failure_line("space_not_reached")
        self.assertIn("space_before=other", line)

    def test_no_app_pid_found_is_unknown(self):
        self.insert()
        probe = FakeProbe(_states(w12345=False))
        self.assertEqual(self.focus(_completed("found:uuid 12345\n"), probe=probe, pids=None), 0)
        line = self.focus_line("INFO")
        self.assertIn("outcome=matched_uuid", line)
        self.assertIn("app_pid=-", line)
        self.assertIn("onscreen_after=-", line)
        self.assertEqual(probe.calls, 1)
        self.defaults.assert_not_called()

    def test_a_window_server_that_never_answers_is_unknown_not_a_hang(self):
        # The pre-probe and the post-check both block; focus still runs the
        # script, gives up on each read at its bound and exits 0.
        import threading
        self.insert()
        release = threading.Event()
        threads = RecordingThreads()
        self.addCleanup(threads.join)
        self.addCleanup(release.set)

        def stuck(pids=None):
            release.wait(5)
            return _states(w12345=False)

        with mock.patch.object(cli, "FOCUS_PRE_PROBE_JOIN_SECS", 0.05), \
                mock.patch.object(spaces, "WINDOW_LIST_TIMEOUT_SECS", 0.05):
            code = self.focus(_completed("found:uuid 12345\n"), probe=stuck, threads=threads)
        self.assertEqual(code, 0)
        self.run.assert_called_once()
        line = self.focus_line("INFO")
        self.assertIn("outcome=matched_uuid", line)
        self.assertIn("space_before=unknown", line)
        self.assertIn("onscreen_after=-", line)
        self.defaults.assert_not_called()

    def test_post_check_reads_that_never_answer_are_unknown_not_space_not_reached(self):
        # The pids and the pre-probe answer; every post-check read is still
        # running at its bound (0 here, so nothing waits).
        self.insert()
        threads = FakeThreads(defer=range(3, 100))
        probe = FakeProbe(_states(w12345=False))
        with mock.patch.object(spaces, "WINDOW_LIST_TIMEOUT_SECS", 0):
            code = self.focus(_completed("found:uuid 12345\n"), probe=probe, threads=threads)
        self.assertEqual(code, 0)
        line = self.focus_line("INFO")
        self.assertIn("space_before=other", line)
        self.assertIn("onscreen_after=-", line)
        self.defaults.assert_not_called()

    def test_unmocked_probe_is_refused_by_the_test_guard(self):
        # AgentBoardTestCase replaces the real window list and defaults runner.
        with self.assertRaises(AssertionError):
            spaces.window_states()
        with self.assertRaises(AssertionError):
            spaces.auto_swoosh()
        with self.assertRaises(AssertionError):
            spaces.app_pids("iTerm")


if __name__ == "__main__":
    unittest.main()
