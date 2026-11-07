"""Claude desktop app sessions: the store lookup and `focus` opening them there.

Every id and path here is made up; the desktop store lives under the fake
HOME that AgentBoardTestCase sets up."""

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentboard import cli, desktop, spaces
from tests import test_cli as tc
from tests.helpers import AgentBoardTestCase

CLI_ID = "11111111-2222-4333-8444-555555555555"
LOCAL_ID = "local_aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def _completed(returncode=0, args=None):
    return subprocess.CompletedProcess(args or ["open"], returncode, "", "")


class _StoreMixin:
    def _store(self):
        return desktop.store_dir(self.fake_home) / "org-1" / "acct-1"

    def _write_record(self, name=None, mtime=None, **fields):
        record = {"sessionId": LOCAL_ID, "cliSessionId": CLI_ID, "isArchived": False,
                  "cwd": "/tmp/made-up", "title": "made-up title"}
        record.update(fields)
        folder = self._store()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (name or f"{record['sessionId']}.json")
        path.write_text(json.dumps(record))
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path


class FindLocalSessionTests(_StoreMixin, AgentBoardTestCase):
    def test_store_dir_is_under_the_home_directory(self):
        self.assertEqual(
            desktop.store_dir(Path("/h")),
            Path("/h/Library/Application Support/Claude/claude-code-sessions"),
        )
        # Default: Path.home(), which the test case points at the fake HOME.
        self.assertEqual(desktop.store_dir(), desktop.store_dir(self.fake_home))

    def test_finds_the_record_by_cli_session_id(self):
        self._write_record()
        self._write_record(sessionId="local_other", cliSessionId="99999999-0000-4000-8000-000000000000")
        self.assertEqual(desktop.find_local_session(CLI_ID), (LOCAL_ID, "found"))

    def test_matches_the_last_assistant_cli_session_id_too(self):
        self._write_record(cliSessionId="99999999-0000-4000-8000-000000000000",
                           lastAssistantCliSessionId=CLI_ID)
        self.assertEqual(desktop.find_local_session(CLI_ID), (LOCAL_ID, "found"))

    def test_no_store(self):
        self.assertEqual(desktop.find_local_session(CLI_ID), (None, "no_store"))

    def test_not_found(self):
        self._write_record(cliSessionId="99999999-0000-4000-8000-000000000000")
        self.assertEqual(desktop.find_local_session(CLI_ID), (None, "not_found"))

    def test_an_id_only_mentioned_in_the_text_is_not_a_match(self):
        self._write_record(cliSessionId="99999999-0000-4000-8000-000000000000",
                           title=f"about {CLI_ID}")
        self.assertEqual(desktop.find_local_session(CLI_ID), (None, "not_found"))

    def test_archived_session_is_not_opened(self):
        self._write_record(isArchived=True)
        self.assertEqual(desktop.find_local_session(CLI_ID), (None, "archived"))

    def test_bad_ids_are_refused_before_any_scan(self):
        self._write_record()
        for bad in (None, "", "short", "../../etc", "a" * 65, 42):
            with self.subTest(bad=bad):
                self.assertEqual(desktop.find_local_session(bad), (None, "bad_id"))

    def test_unexpected_local_ids_are_skipped(self):
        for bad in ("session_x", "local_", "local_a/b", "local_" + "a" * 65, 7):
            with self.subTest(bad=bad):
                path = self._write_record(name="local_x.json", sessionId=bad)
                self.assertEqual(desktop.find_local_session(CLI_ID), (None, "not_found"))
                path.unlink()

    def test_corrupt_and_odd_files_are_skipped(self):
        folder = self._store()
        folder.mkdir(parents=True)
        (folder / "local_bad.json").write_text("{not json " + CLI_ID)
        (folder / "local_list.json").write_text(json.dumps([CLI_ID]))
        (folder / "notes.txt").write_text(CLI_ID)
        (folder / "local_dir.json").mkdir()
        self.assertEqual(desktop.find_local_session(CLI_ID), (None, "not_found"))
        self._write_record()
        self.assertEqual(desktop.find_local_session(CLI_ID), (LOCAL_ID, "found"))

    def test_newest_record_wins(self):
        self._write_record(name="local_old.json", sessionId="local_old", mtime=1_000_000)
        self._write_record(name="local_new.json", sessionId="local_new", mtime=2_000_000)
        self.assertEqual(desktop.find_local_session(CLI_ID), ("local_new", "found"))

    def test_oversized_file_is_skipped(self):
        self._write_record()
        with mock.patch.object(desktop, "MAX_FILE_BYTES", 10):
            self.assertEqual(desktop.find_local_session(CLI_ID), (None, "not_found"))

    def test_read_count_bound(self):
        for n in range(3):
            self._write_record(name=f"local_{n}.json", sessionId=f"local_{n}",
                               cliSessionId=f"99999999-0000-4000-8000-00000000000{n}",
                               mtime=2_000_000 + n)
        self._write_record(mtime=1_000_000)  # the oldest, read last
        with mock.patch.object(desktop, "MAX_READ_FILES", 2):
            self.assertEqual(desktop.find_local_session(CLI_ID), (None, "scan_limit"))
        self.assertEqual(desktop.find_local_session(CLI_ID), (LOCAL_ID, "found"))

    def test_total_bytes_bound(self):
        sizes = [
            self._write_record(name=f"local_{n}.json", sessionId=f"local_{n}",
                               cliSessionId=f"99999999-0000-4000-8000-00000000000{n}",
                               mtime=2_000_000 + n).stat().st_size
            for n in range(2)
        ]
        self._write_record(mtime=1_000_000)  # the oldest, read last
        # Room for the two newer records but not the third.
        with mock.patch.object(desktop, "MAX_TOTAL_BYTES", sum(sizes) + 1):
            self.assertEqual(desktop.find_local_session(CLI_ID), (None, "scan_limit"))
        self.assertEqual(desktop.find_local_session(CLI_ID), (LOCAL_ID, "found"))

    def test_deadline_bound(self):
        self._write_record()
        ticks = iter([0.0, 100.0])
        self.assertEqual(desktop.find_local_session(CLI_ID, clock=lambda: next(ticks)),
                         (None, "scan_limit"))

    def test_unreadable_store_never_raises(self):
        with mock.patch.object(desktop, "_session_files", side_effect=RuntimeError("boom")):
            self._store().mkdir(parents=True)
            self.assertEqual(desktop.find_local_session(CLI_ID), (None, "error"))

    def test_deep_link(self):
        self.assertEqual(desktop.deep_link(LOCAL_ID),
                         f"claude://code/continue?session={LOCAL_ID}")


class FocusDesktopTests(_StoreMixin, AgentBoardTestCase):
    def setUp(self):
        super().setUp()
        for name in ("window_states", "app_pids"):
            patcher = mock.patch.object(spaces, name, side_effect=AssertionError(name))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.stderr = io.StringIO()
        patcher = mock.patch("sys.stderr", self.stderr)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _focus(self, run):
        args = cli.build_parser().parse_args(["focus", "claude", CLI_ID])
        return cli.cmd_focus(args, run_osascript=run)

    def _focus_line(self):
        text = (self.agentboard_home / "agentboard.log").read_text()
        lines = [line for line in text.splitlines() if " cli.focus " in line]
        self.assertEqual(len(lines), 1, text)
        return lines[0]

    def test_opens_the_exact_session_by_deep_link(self):
        tc.insert_session(self.conn, session_id=CLI_ID, entrypoint="claude-desktop")
        self._write_record()
        run = mock.Mock(return_value=_completed())
        self.assertEqual(self._focus(run), 0)
        self.assertEqual(run.call_args_list[0][0][0],
                         ["open", f"claude://code/continue?session={LOCAL_ID}"])
        self.assertEqual(run.call_count, 1)
        line = self._focus_line()
        self.assertIn("outcome=desktop_session", line)
        self.assertIn("strategy=open_url", line)
        self.assertIn("entrypoint=claude-desktop", line)
        self.assertIn("desktop_lookup=found", line)

    def test_falls_back_to_activating_the_app_without_a_record(self):
        tc.insert_session(self.conn, session_id=CLI_ID, entrypoint="claude-desktop")
        run = mock.Mock(return_value=_completed())
        self.assertEqual(self._focus(run), 0)
        self.assertEqual(run.call_args_list, [mock.call(
            ["open", "-b", "com.anthropic.claudefordesktop"],
            capture_output=True, text=True, timeout=cli.OSASCRIPT_TIMEOUT_SECS,
        )])
        line = self._focus_line()
        self.assertIn("outcome=desktop_app_only", line)
        self.assertIn("desktop_lookup=no_store", line)

    def test_falls_back_when_the_deep_link_fails(self):
        tc.insert_session(self.conn, session_id=CLI_ID, entrypoint="claude-desktop")
        self._write_record()
        run = mock.Mock(side_effect=[_completed(1), _completed()])
        self.assertEqual(self._focus(run), 0)
        self.assertEqual(run.call_args_list[1][0][0],
                         ["open", "-b", "com.anthropic.claudefordesktop"])
        line = self._focus_line()
        self.assertIn("strategy=open_url,open_b", line)
        self.assertIn("outcome=desktop_app_only", line)

    def test_missing_app_is_a_failure(self):
        tc.insert_session(self.conn, session_id=CLI_ID, entrypoint="claude-desktop")
        run = mock.Mock(return_value=_completed(1))
        self.assertEqual(self._focus(run), 1)
        self.assertIn("outcome=desktop_app_missing", self._focus_line())
        self.assertIn("Claude desktop app", self.stderr.getvalue())

    def test_timeout(self):
        tc.insert_session(self.conn, session_id=CLI_ID, entrypoint="claude-desktop")
        run = mock.Mock(side_effect=subprocess.TimeoutExpired(["open"], 10))
        self.assertEqual(self._focus(run), 1)
        self.assertIn("outcome=timeout", self._focus_line())

    def test_desktop_wins_over_an_inherited_terminal_and_tmux(self):
        tc.insert_session(self.conn, session_id=CLI_ID, entrypoint="claude-desktop",
                          term_program="iTerm.app", iterm_session_id="w0t0p0:X",
                          tmux_socket="/tmp/made-up/sock", tmux_pane="%1")
        run = mock.Mock(return_value=_completed())
        self.assertEqual(self._focus(run), 0)
        for call in run.call_args_list:
            self.assertEqual(call[0][0][0], "open")
            self.assertNotIn("osascript", call[0][0])

    def test_cli_sessions_keep_terminal_focus(self):
        tc.insert_session(self.conn, session_id=CLI_ID, entrypoint="cli")
        run = mock.Mock(return_value=_completed())
        self.assertEqual(self._focus(run), 1)  # no terminal recorded
        run.assert_not_called()
        self.assertIn("outcome=no_terminal", self._focus_line())
