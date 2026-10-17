"""diag.py: the capped, single-rotation diagnostic log."""

import fcntl
import json
import os
import stat
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentboard import diag  # noqa: E402
from tests.helpers import AgentBoardTestCase  # noqa: E402


class DiagTestCase(AgentBoardTestCase):
    def setUp(self):
        super().setUp()
        self._old_level = os.environ.pop("AGENTBOARD_LOG_LEVEL", None)
        diag._config_cache = None

    def tearDown(self):
        if self._old_level is None:
            os.environ.pop("AGENTBOARD_LOG_LEVEL", None)
        else:
            os.environ["AGENTBOARD_LOG_LEVEL"] = self._old_level
        diag._config_cache = None
        super().tearDown()

    @property
    def log_file(self):
        return self.agentboard_home / diag.LOG_FILENAME

    @property
    def rotated_file(self):
        return self.agentboard_home / diag.ROTATED_FILENAME

    def lines(self, path=None):
        path = path or self.log_file
        return path.read_text().splitlines() if path.exists() else []


class FormatTests(DiagTestCase):
    def test_line_shape(self):
        diag.info("cli", "focus", key="claude:s1", outcome="matched_tty", ms=412)
        [line] = self.lines()
        ts, level, name, *fields = line.split(" ")
        self.assertRegex(ts, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
        self.assertEqual(level, "INFO")
        self.assertEqual(name, "cli.focus")
        self.assertEqual(fields, ["key=claude:s1", "outcome=matched_tty", "ms=412"])

    def test_values_with_spaces_quotes_and_newlines_are_quoted_on_one_line(self):
        diag.info("t", "e", msg='a "b"\nc', empty="", none=None, flag=True, args=["a b", "c"])
        [line] = self.lines()
        self.assertIn(r'msg="a \"b\"\nc"', line)
        self.assertIn('empty=""', line)
        self.assertIn("none=-", line)
        self.assertIn("flag=true", line)
        self.assertIn("args=\"'a b' c\"", line)

    def test_fields_may_be_named_like_the_positional_arguments(self):
        diag.info("hook", "event", event="Stop", level="x", component="y")
        [line] = self.lines()
        self.assertIn("hook.event event=Stop level=x component=y", line)

    def test_creates_the_home_directory_if_missing(self):
        nested = self.tmp_path / "not-yet" / "home"
        os.environ["AGENTBOARD_HOME"] = str(nested)
        diag.info("t", "e")
        self.assertTrue((nested / diag.LOG_FILENAME).is_file())


class LevelTests(DiagTestCase):
    def test_default_level_is_info(self):
        diag.debug("t", "hidden")
        diag.info("t", "shown")
        diag.warn("t", "warned")
        diag.error("t", "failed")
        text = self.log_file.read_text()
        self.assertNotIn("hidden", text)
        self.assertIn("INFO t.shown", text)
        self.assertIn("WARN t.warned", text)
        self.assertIn("ERROR t.failed", text)

    def test_env_level_debug_lets_debug_through(self):
        os.environ["AGENTBOARD_LOG_LEVEL"] = "debug"
        diag.debug("t", "visible")
        self.assertIn("DEBUG t.visible", self.log_file.read_text())

    def test_env_level_error_filters_info_and_warn(self):
        os.environ["AGENTBOARD_LOG_LEVEL"] = "ERROR"
        diag.info("t", "a")
        diag.warn("t", "b")
        diag.error("t", "c")
        self.assertEqual(len(self.lines()), 1)

    def test_config_json_log_level_is_used_without_env(self):
        (self.agentboard_home / "config.json").write_text(json.dumps({"log_level": "debug"}))
        self.assertEqual(diag.current_level(), "debug")
        diag.debug("t", "from-config")
        self.assertIn("t.from-config", self.log_file.read_text())

    def test_env_beats_config(self):
        (self.agentboard_home / "config.json").write_text(json.dumps({"log_level": "debug"}))
        os.environ["AGENTBOARD_LOG_LEVEL"] = "warn"
        self.assertEqual(diag.current_level(), "warn")

    def test_invalid_values_fall_back(self):
        os.environ["AGENTBOARD_LOG_LEVEL"] = "chatty"
        (self.agentboard_home / "config.json").write_text("{not json")
        self.assertEqual(diag.current_level(), "info")
        (self.agentboard_home / "config.json").write_text(json.dumps({"log_level": 3}))
        self.assertEqual(diag.current_level(), "info")

    def test_config_change_is_picked_up(self):
        config = self.agentboard_home / "config.json"
        config.write_text(json.dumps({"log_level": "error"}))
        self.assertEqual(diag.current_level(), "error")
        config.write_text(json.dumps({"log_level": "debug", "pad": "x"}))
        self.assertEqual(diag.current_level(), "debug")


class NeverRaisesTests(DiagTestCase):
    def test_unwritable_home_is_swallowed(self):
        locked = self.tmp_path / "locked"
        locked.mkdir()
        locked.chmod(stat.S_IRUSR | stat.S_IXUSR)
        try:
            os.environ["AGENTBOARD_HOME"] = str(locked)
            diag.error("t", "nowhere", msg="x")  # must not raise
            self.assertFalse((locked / diag.LOG_FILENAME).exists())
        finally:
            locked.chmod(stat.S_IRWXU)

    def test_home_that_is_a_file_is_swallowed(self):
        not_a_dir = self.tmp_path / "file"
        not_a_dir.write_text("x")
        os.environ["AGENTBOARD_HOME"] = str(not_a_dir)
        diag.info("t", "e")

    def test_unformattable_value_is_swallowed(self):
        class Boom:
            def __str__(self):
                raise RuntimeError("no")

        diag.info("t", "e", value=Boom())

    def test_redact_never_raises(self):
        self.assertEqual(diag.redact(None), [])
        self.assertEqual(diag.redact([1, 2]), ["1", "2"])


class RedactTests(DiagTestCase):
    def test_each_flag_in_both_forms(self):
        for flag in ("--note", "--text", "--title"):
            with self.subTest(flag=flag):
                self.assertEqual(
                    diag.redact(["mark", flag, "hello world"]),
                    ["mark", flag, "<redacted:11 chars>"],
                )
                self.assertEqual(
                    diag.redact(["mark", f"{flag}=secret"]),
                    ["mark", f"{flag}=<redacted:6 chars>"],
                )

    def test_ids_paths_and_flags_are_verbatim(self):
        argv = ["focus", "claude", "0f7c-abc", "--hours", "4", "/Users/me/x y", "--json"]
        self.assertEqual(diag.redact(argv), argv)

    def test_long_arguments_are_truncated(self):
        self.assertEqual(diag.redact(["a" * 200]), ["a" * 200])
        self.assertEqual(diag.redact(["b" * 201]), ["b" * 80 + "..."])

    def test_trailing_flag_without_value(self):
        self.assertEqual(diag.redact(["status", "--text"]), ["status", "--text"])


class RotationTests(DiagTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(diag, "MAX_LOG_BYTES", 1000)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_rotates_before_the_cap_is_exceeded(self):
        for i in range(30):
            diag.info("t", "fill", i=i, pad="x" * 50)
        self.assertTrue(self.rotated_file.exists())
        self.assertLessEqual(self.log_file.stat().st_size, 1000)
        self.assertLessEqual(self.rotated_file.stat().st_size, 1000)
        # Nothing lost across the boundary: .1 then .log is one ordered run.
        numbers = [int(l.split("i=")[1].split()[0]) for l in self.lines(self.rotated_file) + self.lines()]
        self.assertEqual(numbers, list(range(numbers[0], 30)))

    def test_second_rotation_replaces_dot_one(self):
        for i in range(60):
            diag.info("t", "fill", i=i, pad="x" * 50)
        first_in_rotated = self.lines(self.rotated_file)[0]
        self.assertNotIn("i=0 ", first_in_rotated)
        self.assertEqual(
            sorted(p.name for p in self.agentboard_home.iterdir() if p.name.startswith("agentboard.log")),
            ["agentboard.log", "agentboard.log.1"],
        )

    def test_line_larger_than_cap_is_truncated(self):
        diag.info("t", "small")
        diag.info("t", "huge", pad="y" * 5000)
        self.assertLessEqual(self.log_file.stat().st_size, 1000)
        last = self.lines()[-1]
        self.assertTrue(last.endswith("..."), last[-20:])
        self.assertIn("t.huge", last)

    def test_default_line_limit_keeps_appends_small(self):
        with mock.patch.object(diag, "MAX_LOG_BYTES", 10 * 1024 * 1024):
            diag.info("t", "huge", pad="z" * 20000)
        self.assertLessEqual(self.log_file.stat().st_size, diag.MAX_LINE_BYTES)

    def test_bounded_growth_never_more_than_two_files_or_two_caps(self):
        """User requirement (2026-10-17): log growth must be bounded. Write
        5x the cap and check after every single write: only .log and .log.1
        exist, no .2 ever, and together they stay within 2x cap + one line."""
        cap = diag.MAX_LOG_BYTES
        line_bytes = None
        written = 0
        i = 0
        while written < 5 * cap:
            diag.info("t", "fill", i=i, pad="x" * 40)
            if line_bytes is None:
                line_bytes = self.log_file.stat().st_size
            written += line_bytes
            i += 1
            names = sorted(
                p.name for p in self.agentboard_home.iterdir() if p.name.startswith("agentboard.log")
            )
            self.assertLessEqual(set(names), {"agentboard.log", "agentboard.log.1"}, names)
            self.assertNotIn("agentboard.log.2", names)
            total = sum((self.agentboard_home / n).stat().st_size for n in names)
            self.assertLessEqual(total, 2 * cap + line_bytes, f"after {i} writes: {total}")
        self.assertTrue(self.rotated_file.exists(), "5x the cap must have rotated")

    def test_racing_rotation_leaves_dot_one_intact(self):
        """Writer B opens and stats an over-cap log; before B can lock and
        rename, writer A rotates. B must notice the path is a new inode and
        write to the live file, never rename it over A's fresh .1."""
        for i in range(15):
            diag.info("t", "fill", i=i, pad="x" * 50)
        # Push the live file to just under the cap so the next write rotates.
        while self.log_file.stat().st_size + 100 <= 1000:
            diag.info("t", "top-up", pad="x" * 20)
        real_flock = fcntl.flock
        state = {"raced": False}

        def racing_flock(fd, op):
            if not state["raced"]:
                state["raced"] = True
                # A's line alone pushes the file past the cap, so A rotates
                # first (on its own fd).
                diag.info("t", "writer-a", pad="x" * 120)
            return real_flock(fd, op)

        with mock.patch.object(fcntl, "flock", side_effect=racing_flock):
            diag.info("t", "writer-b", pad="x" * 60)

        rotated = self.rotated_file.read_text()
        live = self.log_file.read_text()
        self.assertTrue(state["raced"])
        self.assertNotIn("writer-a", rotated, "B must not rotate A's fresh log over .1")
        self.assertIn("t.top-up", rotated, "A's rotation result must survive in .1")
        self.assertIn("writer-a", live)
        self.assertIn("writer-b", live)

    def test_busy_lock_skips_rotation_and_still_writes(self):
        for i in range(15):
            diag.info("t", "fill", i=i, pad="x" * 50)
        with mock.patch.object(fcntl, "flock", side_effect=BlockingIOError):
            diag.info("t", "while-locked", pad="x" * 400)
        self.assertIn("while-locked", self.log_file.read_text())


if __name__ == "__main__":
    unittest.main()
