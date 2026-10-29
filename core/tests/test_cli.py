import contextlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentboard import cli, hook, reconcile
from tests.helpers import AgentBoardTestCase

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# `codex --version` is never run for real here: the Codex hooks-flag
# messages would otherwise depend on whichever Codex the machine has.
# Unknown by default; CodexFeatureFlagTests sets a version per test.
_REAL_CODEX_VERSION = cli._codex_version
_CODEX_VERSION_PATCH = mock.patch.object(cli, "_codex_version", return_value=None)


def setUpModule():
    _CODEX_VERSION_PATCH.start()


def tearDownModule():
    _CODEX_VERSION_PATCH.stop()


def insert_session(conn, tool="claude", session_id="s1", **overrides):
    row = {
        "tool": tool, "session_id": session_id, "cwd": "/tmp/proj", "branch": "main",
        "title": None, "first_prompt": "do a thing", "last_prompt": None,
        "last_assistant": None, "started_at": "2026-06-15T10:00:00.000000Z",
        "updated_at": "2026-06-15T10:00:00.000000Z", "last_event": "Stop",
        "last_event_at": "2026-06-15T10:00:00.000000Z", "waiting_reason": None,
        "pid": None, "iterm_session_id": None, "term_program": None, "tty": None,
        "transcript_path": None, "origin": "hook", "interactive": 1, "ended_at": None,
    }
    row.update(overrides)
    cols = ",".join(row.keys())
    placeholders = ",".join("?" for _ in row)
    conn.execute(f"INSERT INTO sessions({cols}) VALUES ({placeholders})", list(row.values()))


def _python3_stub_env(bindir: Path) -> dict:
    """A `python3` stub on PATH that execs this exact interpreter, so a
    test that runs the shim by its `#!/usr/bin/env python3` shebang (or
    through a shell, the way Claude/Codex run hook commands) is pinned to
    the interpreter the suite itself runs under -- never whatever a bare
    `python3` happens to resolve to on the machine (e.g. an old system
    3.9 that can't import the package at all)."""
    bindir.mkdir(parents=True, exist_ok=True)
    stub = bindir / "python3"
    stub.write_text(f'#!/bin/bash\nexec "{sys.executable}" "$@"\n')
    stub.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    return env


class SnapshotTests(AgentBoardTestCase):
    def test_contract_shape(self):
        insert_session(self.conn, session_id="s1", last_event="Stop")
        snapshot = cli.build_snapshot(self.conn, include_all=True)
        self.assertIn("generated_at", snapshot)
        self.assertIn("counts", snapshot)
        self.assertEqual(set(snapshot["counts"].keys()), set(cli.STATES) | {"notes"})
        entry = snapshot["sessions"][0]
        for key in (
            "key", "tool", "session_id", "state", "waiting_reason", "title", "cwd",
            "project", "branch", "first_prompt", "last_prompt", "last_assistant",
            "started_at", "updated_at", "ended_at", "alive", "interactive",
            "can_focus", "resume_command", "status_text", "links", "triage",
            "notes", "notes_count", "model", "context_tokens", "context_at",
            "permission_mode",
        ):
            self.assertIn(key, entry)
        self.assertEqual(entry["key"], "claude:s1")
        self.assertEqual(entry["project"], "proj")
        self.assertIn("usage", snapshot)

    def test_session_model_and_context_fields(self):
        insert_session(self.conn, session_id="s1", model="claude-opus-5-5",
                       context_tokens=308011, context_at="2026-06-15T10:00:00.000Z")
        insert_session(self.conn, session_id="s2")
        entries = {e["session_id"]: e
                   for e in cli.build_snapshot(self.conn, include_all=True)["sessions"]}
        self.assertEqual(
            (entries["s1"]["model"], entries["s1"]["context_tokens"],
             entries["s1"]["context_at"]),
            ("claude-opus-5-5", 308011, "2026-06-15T10:00:00.000Z"))
        self.assertEqual(
            (entries["s2"]["model"], entries["s2"]["context_tokens"],
             entries["s2"]["context_at"]), (None, None, None))

    def test_session_permission_mode_field(self):
        insert_session(self.conn, session_id="s1", permission_mode="plan")
        insert_session(self.conn, session_id="s2")
        entries = {e["session_id"]: e
                   for e in cli.build_snapshot(self.conn, include_all=True)["sessions"]}
        self.assertEqual(entries["s1"]["permission_mode"], "plan")
        self.assertIsNone(entries["s2"]["permission_mode"])

    def test_usage_is_empty_without_any_source(self):
        insert_session(self.conn, session_id="s1")
        self.assertEqual(cli.build_snapshot(self.conn, include_all=True)["usage"], [])

    def test_usage_reads_the_rate_limit_file_under_home(self):
        (self.fake_home / ".claude" / "rate-limits.json").write_text(json.dumps({
            "cached_at": 1792841643,
            "five_hour": {"used_percentage": 57, "resets_at": 1792848600},
            "seven_day": {"used_percentage": 70, "resets_at": 1793120400},
        }))
        before_reset = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
        snapshot = cli.build_snapshot(self.conn, include_all=True, now=before_reset)
        (claude,) = snapshot["usage"]
        self.assertEqual((claude["source"], claude["label"], claude["updated_at"]),
                         ("claude", "Claude", "2026-10-24T11:34:03Z"))
        self.assertEqual(claude["windows"][0],
                         {"label": "5h", "used_percentage": 57, "resets_at": "2026-10-24T13:30:00Z"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli._print_snapshot_table(snapshot, now=before_reset)
        self.assertIn("usage: Claude 5h 57% \u00b7 7d 70%", out.getvalue())

    def test_usage_reads_drop_files_from_the_usage_directory(self):
        directory = self.agentboard_home / "usage"
        directory.mkdir()
        (directory / "acme.json").write_text(json.dumps({
            "label": "Acme", "updated_at": "2026-10-24T11:00:00Z",
            "windows": [{"label": "daily", "used_percentage": 20}]}))
        now = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
        snapshot = cli.build_snapshot(self.conn, include_all=True, now=now)
        self.assertEqual([(s["source"], s["label"]) for s in snapshot["usage"]], [("acme", "Acme")])

    def test_malformed_usage_files_never_break_the_snapshot(self):
        insert_session(self.conn, session_id="s1")
        directory = self.agentboard_home / "usage"
        directory.mkdir()
        (directory / "deep.json").write_text("[" * 20000 + "]" * 20000)
        (directory / "edge.json").write_text(json.dumps({
            "updated_at": "9999-12-31T23:59:59-05:00",
            "windows": [{"label": "5h", "used_percentage": 3,
                         "resets_at": "9999-12-31T23:59:59-05:00"}]}))
        (directory / "ok.json").write_text(json.dumps({
            "updated_at": "2026-10-24T11:00:00Z",
            "windows": [{"label": "daily", "used_percentage": 20}]}))
        now = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
        snapshot = cli.build_snapshot(self.conn, include_all=True, now=now)
        self.assertEqual([s["source"] for s in snapshot["usage"]], ["edge", "ok"])
        self.assertEqual([s["session_id"] for s in snapshot["sessions"]], ["s1"])

    def test_any_usage_reader_failure_yields_an_empty_list(self):
        insert_session(self.conn, session_id="s1")
        with mock.patch.object(cli.usage_mod, "read_usage_sources",
                               side_effect=RuntimeError("boom")), \
                mock.patch.object(cli.diag, "debug") as debug:
            snapshot = cli.build_snapshot(self.conn, include_all=True)
        self.assertEqual(snapshot["usage"], [])
        self.assertEqual([s["session_id"] for s in snapshot["sessions"]], ["s1"])
        debug.assert_called_once_with("usage", "sources_failed", exc="RuntimeError")

    def test_usage_table_line_drops_reset_windows_like_the_app(self):
        usage = [
            {"source": "claude", "label": "Claude", "windows": [
                {"label": "5h", "used_percentage": 57, "resets_at": "2026-10-24T13:30:00Z"},
                {"label": "7d", "used_percentage": 70, "resets_at": "2026-10-27T17:00:00Z"},
                {"label": "zeta", "used_percentage": 5},
            ]},
            {"source": "codex", "label": "Codex", "windows": [
                {"label": "5h", "used_percentage": 12.5, "resets_at": "2026-10-24T13:00:00Z"},
            ]},
        ]
        before = datetime(2026, 10, 24, 12, 59, tzinfo=timezone.utc)
        after = datetime(2026, 10, 24, 13, 30, tzinfo=timezone.utc)
        self.assertEqual(cli._usage_table_line(usage, now=before),
                         "usage: Claude 5h 57% \u00b7 7d 70% \u00b7 zeta 5%; Codex 5h 12.5%")
        self.assertEqual(cli._usage_table_line(usage, now=after),
                         "usage: Claude 7d 70% \u00b7 zeta 5%")
        late = datetime(2026, 10, 28, tzinfo=timezone.utc)
        self.assertEqual(cli._usage_table_line(usage, now=late), "usage: Claude zeta 5%")
        del usage[0]["windows"][2]
        self.assertIsNone(cli._usage_table_line(usage, now=late))
        self.assertIsNone(cli._usage_table_line([], now=late))
        self.assertIsNone(cli._usage_table_line(None, now=late))

    def test_snapshot_via_shim_stays_quiet_with_bad_usage_files(self):
        (self.fake_home / ".claude" / "rate-limits.json").write_text("{not json")
        (self.agentboard_home / "usage").mkdir()
        (self.agentboard_home / "usage" / "x.json").write_text("[]")
        env = _python3_stub_env(self.tmp_path / "stub-bin")
        env["HOME"] = str(self.fake_home)
        env["AGENTBOARD_HOME"] = str(self.agentboard_home)
        shim = Path(cli.__file__).resolve().parent.parent / "bin" / "agentboard"
        result = subprocess.run([str(shim), "snapshot", "--json"], env=env,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout)["usage"], [])
        log = self.agentboard_home / "agentboard.log"
        text = log.read_text() if log.exists() else ""
        for line in text.splitlines():
            if "usage" in line:
                self.assertIn("debug", line.lower())

    def test_registry_consulted_when_pid_known(self):
        # A live session whose last_event is a background agent's late
        # SubagentStop; its ~/.claude/sessions/<pid>.json (under the test's
        # fake HOME) says idle, written after the latest prompt.
        now = datetime(2026, 10, 24, 6, 0, tzinfo=timezone.utc)
        pid = os.getpid()
        insert_session(
            self.conn, session_id="s1", pid=pid, last_event="SubagentStop",
            last_event_at="2026-10-24T05:47:22.900000Z",
            updated_at="2026-10-24T05:47:22.900000Z", last_assistant="All set.",
        )
        self.conn.execute(
            "INSERT INTO events(ts, tool, session_id, event, text) VALUES "
            "('2026-10-24T05:42:46.990000Z', 'claude', 's1', 'UserPromptSubmit', 'go')"
        )
        sessions_dir = self.fake_home / ".claude" / "sessions"
        sessions_dir.mkdir()
        idle_at = datetime(2026, 10, 24, 5, 44, 21, 15000, tzinfo=timezone.utc)
        registry_file = sessions_dir / f"{pid}.json"
        registry_file.write_text(json.dumps({
            "pid": pid, "sessionId": "s1", "status": "idle",
            "statusUpdatedAt": int(idle_at.timestamp() * 1000),
        }))
        entry = cli.build_snapshot(self.conn, include_all=True, now=now)["sessions"][0]
        self.assertEqual((entry["state"], entry["waiting_reason"]),
                         ("review", "Your turn: All set."))
        # A file another process wrote (a stale pid) is not this session's.
        registry_file.rename(sessions_dir / "1.json")
        (sessions_dir / "1.json").write_text(json.dumps({
            "pid": 1, "sessionId": "s1", "status": "idle",
            "statusUpdatedAt": int(idle_at.timestamp() * 1000),
        }))
        entry = cli.build_snapshot(self.conn, include_all=True, now=now)["sessions"][0]
        self.assertEqual(entry["state"], "running")

    def test_registry_file_for_another_pid_is_ignored_and_the_real_one_wins(self):
        # The session's pid is known; a <otherpid>.json for the same session
        # id (a crashed earlier process) says busy, fresh: not this
        # process's news, so the finished turn stays Review.
        now = datetime(2026, 10, 24, 6, 0, tzinfo=timezone.utc)
        pid = os.getpid()
        dead_pid = 2**30
        insert_session(
            self.conn, session_id="s1", pid=pid, last_event="Stop",
            last_event_at="2026-10-24T05:44:20.010000Z",
            updated_at="2026-10-24T05:44:20.010000Z", last_assistant="All set.",
        )
        sessions_dir = self.fake_home / ".claude" / "sessions"
        sessions_dir.mkdir()

        def write(file_pid, status, at):
            (sessions_dir / f"{file_pid}.json").write_text(json.dumps({
                "pid": file_pid, "sessionId": "s1", "status": status,
                "statusUpdatedAt": int(at.timestamp() * 1000),
            }))

        write(dead_pid, "busy", datetime(2026, 10, 24, 5, 59, tzinfo=timezone.utc))
        entry = cli.build_snapshot(self.conn, include_all=True, now=now)["sessions"][0]
        self.assertEqual((entry["state"], entry["waiting_reason"]),
                         ("review", "Your turn: All set."))
        # The live pid's own file is the one consulted, even though the dead
        # one's statusUpdatedAt is newer and directory order is arbitrary.
        write(dead_pid, "idle", datetime(2026, 10, 24, 5, 59, tzinfo=timezone.utc))
        write(pid, "busy", datetime(2026, 10, 24, 5, 50, tzinfo=timezone.utc))
        entry = cli.build_snapshot(self.conn, include_all=True, now=now)["sessions"][0]
        self.assertEqual((entry["state"], entry["waiting_reason"]), ("running", None))

    def test_default_excludes_noninteractive(self):
        insert_session(self.conn, session_id="s1", interactive=0)
        snapshot = cli.build_snapshot(self.conn, include_all=False)
        self.assertEqual(snapshot["sessions"], [])
        snapshot_all = cli.build_snapshot(self.conn, include_all=True)
        self.assertEqual(len(snapshot_all["sessions"]), 1)

    def test_default_excludes_old_done(self):
        old = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        insert_session(self.conn, session_id="s1", updated_at=old)
        self.conn.execute(
            "INSERT INTO triage(tool, session_id, status, until, note, updated_at) "
            "VALUES ('claude','s1','done',NULL,NULL,?)", (old,),
        )
        snapshot = cli.build_snapshot(self.conn, include_all=False)
        self.assertEqual(snapshot["sessions"], [])

    def test_default_excludes_sessions_with_no_prompt_at_all(self):
        insert_session(
            self.conn, session_id="s1", first_prompt=None, last_prompt=None,
            last_event="SessionStart",
        )
        snapshot = cli.build_snapshot(self.conn, include_all=False)
        self.assertEqual(snapshot["sessions"], [])
        snapshot_all = cli.build_snapshot(self.conn, include_all=True)
        self.assertEqual(len(snapshot_all["sessions"]), 1)

    def test_no_prompt_but_needs_you_is_still_shown_by_default(self):
        insert_session(
            self.conn, session_id="s1", first_prompt=None, last_prompt=None,
            last_event="PermissionRequest", waiting_reason="Bash",
        )
        snapshot = cli.build_snapshot(self.conn, include_all=False)
        self.assertEqual(len(snapshot["sessions"]), 1)

    def test_sort_order_state_priority_then_recency(self):
        insert_session(self.conn, session_id="old-review", last_event="Stop",
                        updated_at="2026-06-15T08:00:00.000000Z")
        insert_session(self.conn, session_id="needs-you", last_event="PermissionRequest",
                        waiting_reason="Bash", updated_at="2026-06-15T09:00:00.000000Z")
        insert_session(self.conn, session_id="new-review", last_event="Stop",
                        updated_at="2026-06-15T11:00:00.000000Z")
        snapshot = cli.build_snapshot(self.conn, include_all=True)
        keys = [s["key"] for s in snapshot["sessions"]]
        self.assertEqual(keys, ["claude:needs-you", "claude:new-review", "claude:old-review"])

    def test_pr_merged_marks_done_in_snapshot(self):
        insert_session(self.conn, session_id="s1", branch="feature-x",
                        last_event="Stop", updated_at="2026-06-15T10:00:00.000000Z")
        insert_session(self.conn, session_id="legacy", branch="feature-x",
                        last_event="Stop", updated_at="2026-06-15T10:00:00.000000Z")
        self.conn.execute(
            "INSERT INTO links(tool, session_id, kind, ref, url, state, title, checked_at, branch) "
            "VALUES ('claude','s1','pr','#1','u','MERGED','t','2026-06-15T10:00:00.000000Z',"
            "'feature-x')"
        )
        # Counter-case: a legacy link with no recorded branch never retires.
        self.conn.execute(
            "INSERT INTO links(tool, session_id, kind, ref, url, state, title, checked_at) "
            "VALUES ('claude','legacy','pr','#1','u','MERGED','t','2026-06-15T10:00:00.000000Z')"
        )
        snapshot = cli.build_snapshot(self.conn, include_all=True)
        by_key = {e["key"]: e for e in snapshot["sessions"]}
        self.assertEqual(by_key["claude:s1"]["state"], "done")
        self.assertEqual(by_key["claude:s1"]["done_reason"], "pr merged")
        self.assertNotEqual(by_key["claude:legacy"]["state"], "done")

    def test_old_dead_session_is_unconcluded_not_interrupted(self):
        old = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        insert_session(
            self.conn, session_id="s1", last_event="PreToolUse", pid=2**30,
            last_event_at=old, updated_at=old,
        )
        snapshot = cli.build_snapshot(self.conn, include_all=True)
        entry = snapshot["sessions"][0]
        self.assertEqual(entry["state"], "unconcluded")
        self.assertEqual(entry["waiting_reason"], "interrupted")

    def test_snapshot_includes_auto_done_fields_for_every_session(self):
        insert_session(self.conn, session_id="s1")
        snapshot = cli.build_snapshot(self.conn, include_all=True)
        entry = snapshot["sessions"][0]
        self.assertIn("auto_done", entry)
        self.assertIn("done_reason", entry)
        self.assertFalse(entry["auto_done"])
        self.assertIsNone(entry["done_reason"])

    def test_clean_exit_auto_done_in_snapshot(self):
        ended = "2026-06-15T09:55:00.000000Z"
        insert_session(
            self.conn, session_id="s1", last_event="SessionEnd", end_reason="prompt_input_exit",
            ended_at=ended, updated_at=ended, todos_open=0,
        )
        self.conn.execute(
            "INSERT INTO events(ts, tool, session_id, event) VALUES "
            "('2026-06-15T09:50:00.000000Z','claude','s1','UserPromptSubmit'),"
            "('2026-06-15T09:51:00.000000Z','claude','s1','Stop'),"
            "('2026-06-15T09:55:00.000000Z','claude','s1','SessionEnd')"
        )
        snapshot = cli.build_snapshot(self.conn, include_all=True)
        entry = snapshot["sessions"][0]
        self.assertEqual(entry["state"], "done")
        self.assertTrue(entry["auto_done"])
        self.assertEqual(entry["done_reason"], "clean exit")

    def test_status_text_field_name_and_expiry(self):
        future = (datetime.now(timezone.utc) + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        past = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        insert_session(self.conn, session_id="s1", status_text="working", status_until=future)
        insert_session(self.conn, session_id="s2", status_text="stale", status_until=past)
        snapshot = cli.build_snapshot(self.conn, include_all=True)
        by_key = {s["key"]: s for s in snapshot["sessions"]}
        self.assertEqual(by_key["claude:s1"]["status_text"], "working")
        self.assertIsNone(by_key["claude:s2"]["status_text"])

    def test_snapshot_skips_turn_scan_for_claude(self):
        ended = "2026-06-15T09:55:00.000000Z"
        insert_session(
            self.conn, session_id="c1", last_event="SessionEnd", end_reason="prompt_input_exit",
            ended_at=ended, updated_at=ended,
        )
        insert_session(
            self.conn, tool="codex", session_id="x1", last_event="SessionEnd",
            end_reason="exit", ended_at=ended, updated_at=ended,
        )
        # An ended Codex row with no end_reason can never be a clean exit.
        insert_session(
            self.conn, tool="codex", session_id="x2", last_event="SessionEnd",
            end_reason=None, ended_at=ended, updated_at=ended,
        )
        with mock.patch.object(cli.reconcile, "last_turn_finished",
                               return_value=True) as scan:
            cli.build_snapshot(self.conn, include_all=True)
        self.assertEqual(scan.call_args_list, [mock.call(self.conn, "codex", "x1")])

    def test_codex_clean_exit_state_survives_compaction(self):
        old = datetime.now(timezone.utc) - timedelta(days=30)

        def ts(minutes):
            return (old + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        insert_session(
            self.conn, tool="codex", session_id="x1", last_event="SessionEnd",
            end_reason="exit", ended_at=ts(9), updated_at=ts(9), started_at=ts(0), todos_open=0,
        )
        for minutes, event in enumerate(
            ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "SubagentStart",
             "SubagentStop", "PreCompact", "PostCompact", "Stop", "SessionEnd"]
        ):
            _insert_event(self.conn, event, tool="codex", session_id="x1", ts=ts(minutes))
        before = cli.build_snapshot(self.conn, include_all=True)["sessions"][0]
        self.assertEqual(before["state"], "done")
        rc, out, _ = _run_cmd(["prune", "--days", "0", "--compact-days", "7"])
        self.assertEqual(rc, 0)
        self.assertIn("compacted 6 event(s)", out)
        left = [r["event"] for r in self.conn.execute("SELECT event FROM events ORDER BY id")]
        self.assertEqual(left, ["SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"])
        after = cli.build_snapshot(self.conn, include_all=True)["sessions"][0]
        self.assertEqual((after["state"], after["done_reason"]),
                         (before["state"], before["done_reason"]))


class SnapshotNotesTests(AgentBoardTestCase):
    def setUp(self):
        super().setUp()
        self.now = datetime.now(timezone.utc)

    def stamp(self, minutes_ago):
        return (self.now - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def session(self, session_id="s1", **overrides):
        overrides.setdefault("updated_at", self.stamp(30))
        overrides.setdefault("last_event_at", self.stamp(30))
        insert_session(self.conn, session_id=session_id, **overrides)

    def note(self, text, kind="needs_you", session_id="s1", minutes_ago=5):
        from agentboard import notes

        return notes.record_note(self.conn, "claude", session_id, kind, text, "cli",
                                 self.stamp(minutes_ago))

    def prompt(self, minutes_ago, session_id="s1", agent_id=None):
        _insert_event(self.conn, "UserPromptSubmit", "hi", session_id=session_id,
                      ts=self.stamp(minutes_ago), agent_id=agent_id)

    def entry(self, include_all=True, key="claude:s1"):
        snapshot = cli.build_snapshot(self.conn, include_all=include_all, now=self.now)
        return snapshot, next((e for e in snapshot["sessions"] if e["key"] == key), None)

    def test_open_needs_you_note_raises_state_with_first_line(self):
        self.session()
        self.note("USER_ACTION: [proj] approve the plan\nextra detail")
        _, entry = self.entry()
        self.assertEqual(entry["state"], "needs_you")
        self.assertEqual(entry["waiting_reason"], "USER_ACTION: [proj] approve the plan")
        self.assertEqual(entry["notes_count"], 1)
        self.assertEqual(
            set(entry["notes"][0]), {"id", "kind", "text", "ts", "last_ts", "count", "source"}
        )

    def test_marker_line_is_the_waiting_reason_even_when_not_first(self):
        self.session()
        self.note("Tick summary.\nUSER_ACTION: approve the plan")
        self.assertEqual(self.entry()[1]["waiting_reason"], "USER_ACTION: approve the plan")

    def test_hook_needs_you_reason_wins_over_note(self):
        self.session(last_event="PermissionRequest", waiting_reason="Bash")
        self.note("USER_ACTION: approve")
        _, entry = self.entry()
        self.assertEqual((entry["state"], entry["waiting_reason"]), ("needs_you", "Bash"))

    def test_newer_parent_prompt_releases_older_does_not(self):
        self.session()
        self.note("USER_ACTION: approve", minutes_ago=5)
        self.prompt(minutes_ago=10)
        self.assertEqual(self.entry()[1]["state"], "needs_you")
        self.prompt(minutes_ago=1)
        _, entry = self.entry()
        self.assertEqual((entry["state"], entry["waiting_reason"]), ("review", "Your turn"))
        self.assertEqual(entry["notes_count"], 1, "a released note stays listed")

    def test_subagent_prompt_does_not_release(self):
        self.session()
        self.note("USER_ACTION: approve", minutes_ago=5)
        self.prompt(minutes_ago=1, agent_id="sub-1")
        self.assertEqual(self.entry()[1]["state"], "needs_you")

    def test_dismissed_note_returns_prior_state(self):
        self.session()
        note_id = self.note("USER_ACTION: approve")
        self.conn.execute("UPDATE notes SET dismissed_at=? WHERE id=?", (self.stamp(0), note_id))
        _, entry = self.entry()
        self.assertEqual((entry["state"], entry["waiting_reason"]), ("review", "Your turn"))
        self.assertEqual((entry["notes"], entry["notes_count"]), ([], 0))

    def test_update_note_does_not_raise(self):
        # An update note never raises needs_you; on a live session it is
        # the Review reason while it is newer than the last prompt.
        self.session()
        self.note("**PR #4 merged**\nall checks green", kind="update")
        _, entry = self.entry()
        self.assertEqual((entry["state"], entry["waiting_reason"]), ("review", "PR #4 merged"))

    def test_snooze_wins(self):
        self.session()
        self.note("USER_ACTION: approve")
        self.conn.execute(
            "INSERT INTO triage(tool, session_id, status, until, note, updated_at) "
            "VALUES ('claude','s1','snoozed',?,NULL,?)",
            ((self.now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"), self.stamp(0)),
        )
        self.assertEqual(self.entry()[1]["state"], "snoozed")

    def test_cap_20_newest_first_with_uncapped_count(self):
        self.session()
        for i in range(25):
            self.note(f"PR #{i} merged", kind="update", minutes_ago=100 - i)
        snapshot, entry = self.entry()
        self.assertEqual(len(entry["notes"]), cli.SNAPSHOT_NOTES_PER_SESSION)
        self.assertEqual(entry["notes_count"], 25)
        self.assertEqual(entry["notes"][0]["text"], "PR #24 merged")
        self.assertEqual(entry["notes"][-1]["text"], "PR #5 merged")
        self.assertEqual(snapshot["counts"]["notes"], 25)

    def test_cap_never_drops_an_open_needs_you_note(self):
        self.session()
        self.note("USER_ACTION: approve the plan", minutes_ago=200)
        for i in range(25):
            self.note(f"PR #{i} merged", kind="update", minutes_ago=100 - i)
        _, entry = self.entry()
        self.assertEqual(entry["notes_count"], 26)
        self.assertEqual(len(entry["notes"]), cli.SNAPSHOT_NOTES_PER_SESSION + 1)
        self.assertEqual(entry["notes"][-1]["text"], "USER_ACTION: approve the plan")
        self.assertEqual([n["kind"] for n in entry["notes"][:-1]], ["update"] * 20)

    def test_ordering_follows_last_ts_not_insertion(self):
        self.session()
        self.note("PR #1 merged", kind="update", minutes_ago=50)
        self.note("PR #2 merged", kind="update", minutes_ago=40)
        self.note("PR #1 merged", kind="update", minutes_ago=2)  # repeat bumps last_ts
        _, entry = self.entry()
        self.assertEqual([n["text"] for n in entry["notes"]], ["PR #1 merged", "PR #2 merged"])
        self.assertEqual(entry["notes"][0]["count"], 2)

    def test_counts_notes_only_cover_emitted_sessions(self):
        self.session("shown")
        self.session("hidden", interactive=0)
        self.note("PR #1 merged", kind="update", session_id="shown")
        self.note("PR #2 merged", kind="update", session_id="hidden")
        self.note("PR #3 merged", kind="update", session_id="hidden")
        snapshot = cli.build_snapshot(self.conn, include_all=False, now=self.now)
        self.assertEqual([e["key"] for e in snapshot["sessions"]], ["claude:shown"])
        self.assertEqual(snapshot["counts"]["notes"], 1)
        snapshot_all = cli.build_snapshot(self.conn, include_all=True, now=self.now)
        self.assertEqual(snapshot_all["counts"]["notes"], 3)

    def test_session_without_notes_has_empty_list(self):
        self.session()
        snapshot, entry = self.entry()
        self.assertEqual((entry["notes"], entry["notes_count"]), ([], 0))
        self.assertEqual(snapshot["counts"]["notes"], 0)


class SnapshotLifecycleTests(AgentBoardTestCase):
    """The A/B/C cases of the session-lifecycle rules, end to end through
    `agentboard snapshot --json --all`."""

    def setUp(self):
        super().setUp()
        self.now = datetime.now(timezone.utc)

    def stamp(self, minutes_ago):
        return (self.now - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def snapshot(self):
        rc, out, _err = _run_cmd(["snapshot", "--json", "--all"])
        self.assertEqual(rc, 0)
        return {e["session_id"]: e for e in json.loads(out)["sessions"]}

    def link(self, session_id, ref, state, branch, source, created_minutes_ago=None,
             closed_minutes_ago=None, responded=False, url=True):
        self.conn.execute(
            "INSERT INTO links(tool, session_id, kind, ref, url, state, title, checked_at, "
            "branch, closed_at, source, created_at, responded_at) "
            "VALUES ('claude',?, 'pr', ?, ?, ?, 't', ?, ?, ?, ?, ?, ?)",
            (session_id, ref,
             f"https://github.com/acme/widgets/pull/{ref.lstrip('#')}" if url else None,
             state, self.stamp(1), branch,
             self.stamp(closed_minutes_ago) if closed_minutes_ago is not None else None,
             source,
             self.stamp(created_minutes_ago) if created_minutes_ago is not None else None,
             self.stamp(2) if responded else None),
        )

    def clean_exit(self, session_id, branch="feature-x", **overrides):
        overrides.setdefault("end_reason", "prompt_input_exit")
        insert_session(
            self.conn, session_id=session_id, branch=branch, last_event="SessionEnd",
            started_at=self.stamp(120),
            ended_at=self.stamp(5), updated_at=self.stamp(5), last_event_at=self.stamp(5),
            **overrides,
        )
        _insert_event(self.conn, "SessionEnd", session_id=session_id, ts=self.stamp(5))

    def test_a_merged_pr_on_an_old_branch_does_not_hide_a_live_session(self):
        # cd48bb3b: PR #1 merged long ago on feature-1; the session now
        # sits on main reporting PRs #3/#4 the user has already answered.
        insert_session(self.conn, session_id="handler", branch="main", started_at=self.stamp(600),
                       updated_at=self.stamp(1), last_event_at=self.stamp(1),
                       last_assistant="Waiting for the next tick.")
        _insert_event(self.conn, "UserPromptSubmit", "go", session_id="handler", ts=self.stamp(3))
        self.link("handler", "#1", "MERGED", "feature-1", "cli", 500, closed_minutes_ago=400)
        self.link("handler", "#3", "OPEN", "handler-3", "cli", 100, responded=True)
        entry = self.snapshot()["handler"]
        self.assertNotEqual(entry["state"], "done")
        self.assertEqual((entry["state"], entry["waiting_reason"]),
                         ("review", "Your turn: Waiting for the next tick."))
        by_ref = {link["ref"]: link for link in entry["links"]}
        self.assertEqual(by_ref["#1"]["branch"], "feature-1")
        self.assertTrue(by_ref["#3"]["owned"])
        self.assertFalse(by_ref["#3"]["awaits_you"])

    def test_a_new_prompt_after_the_merge_reopens(self):
        insert_session(self.conn, session_id="s1", branch="feature-1", started_at=self.stamp(600),
                       updated_at=self.stamp(1), last_event_at=self.stamp(1))
        self.link("s1", "#1", "MERGED", "feature-1", "gh", 500, closed_minutes_ago=60)
        self.assertEqual(self.snapshot()["s1"]["state"], "done")
        _insert_event(self.conn, "UserPromptSubmit", "more", session_id="s1", ts=self.stamp(30))
        self.assertNotEqual(self.snapshot()["s1"]["state"], "done")

    def test_b_clean_exit_with_an_inherited_pr_is_done(self):
        # 3647203a: PR #2 was on the branch before the session started.
        self.clean_exit("inherit", branch="claude/units")
        self.link("inherit", "#2", "OPEN", "claude/units", "gh", created_minutes_ago=3000)
        entry = self.snapshot()["inherit"]
        self.assertEqual((entry["state"], entry["done_reason"]), ("done", "clean exit"))
        [link] = entry["links"]
        self.assertEqual((link["owned"], link["awaits_you"]), (False, False))

    def test_b_clean_exit_with_only_a_session_end_event_is_done(self):
        # b263c21f: hooks installed mid-session, one SessionEnd event.
        self.clean_exit("late-hooks")
        entry = self.snapshot()["late-hooks"]
        self.assertEqual((entry["state"], entry["done_reason"]), ("done", "clean exit"))

    def test_b_unclean_end_and_open_todos_are_unconcluded_with_a_reason(self):
        self.clean_exit("cleared", end_reason="clear")
        self.clean_exit("todos", todos_open=2)
        snap = self.snapshot()
        self.assertEqual((snap["cleared"]["state"], snap["cleared"]["waiting_reason"]),
                         ("unconcluded", "Ended (clear)"))
        self.assertEqual((snap["todos"]["state"], snap["todos"]["waiting_reason"]),
                         ("unconcluded", "2 todos left open"))

    def test_c_owned_awaiting_pr_keeps_a_clean_exit_in_review(self):
        self.clean_exit("owner")
        self.link("owner", "#5", "OPEN", "feature-x", "gh", created_minutes_ago=60)
        entry = self.snapshot()["owner"]
        self.assertEqual((entry["state"], entry["waiting_reason"]),
                         ("review", "PR #5 awaits your review"))
        [link] = entry["links"]
        self.assertEqual((link["owned"], link["awaits_you"], link["branch"]),
                         (True, True, "feature-x"))
        self.conn.execute("UPDATE links SET responded_at=?", (self.stamp(1),))
        entry = self.snapshot()["owner"]
        self.assertEqual((entry["state"], entry["done_reason"]), ("done", "clean exit"))

    def test_c_urlless_reported_pr_never_awaits_you(self):
        self.clean_exit("nourl")
        self.link("nourl", "#6", "OPEN", "feature-x", "cli", url=False)
        entry = self.snapshot()["nourl"]
        self.assertEqual(entry["state"], "done")
        self.assertEqual((entry["links"][0]["owned"], entry["links"][0]["awaits_you"]),
                         (True, False))

    def test_update_note_newer_than_last_prompt_on_a_live_session_is_review(self):
        from agentboard import notes

        insert_session(self.conn, session_id="live", updated_at=self.stamp(10),
                       last_event_at=self.stamp(10), last_assistant="All done.")
        _insert_event(self.conn, "UserPromptSubmit", "hi", session_id="live", ts=self.stamp(20))
        notes.record_note(self.conn, "claude", "live", "update",
                          "Tick summary\n**CI went red on PR #9**", "cli", self.stamp(10))
        entry = self.snapshot()["live"]
        self.assertEqual((entry["state"], entry["waiting_reason"]), ("review", "Tick summary"))
        _insert_event(self.conn, "UserPromptSubmit", "ok", session_id="live", ts=self.stamp(5))
        entry = self.snapshot()["live"]
        self.assertEqual((entry["state"], entry["waiting_reason"]), ("review", "Your turn: All done."))

    def test_unattended_loop_idle_is_running_while_alive(self):
        insert_session(self.conn, session_id="loop", pid=os.getpid(), updated_at=self.stamp(2),
                       last_event_at=self.stamp(2), last_prompt="# Autonomous loop tick")
        entry = self.snapshot()["loop"]
        self.assertEqual((entry["state"], entry["waiting_reason"]),
                         ("running", "Loop idle between ticks"))
        self.conn.execute("UPDATE sessions SET pid=? WHERE session_id='loop'", (2**30,))
        self.assertEqual(self.snapshot()["loop"]["state"], "unconcluded")

    def test_unattended_prefixes_come_from_config(self):
        (self.agentboard_home / "config.json").write_text(
            json.dumps({"note_unattended_prefixes": ["[cron]"]})
        )
        insert_session(self.conn, session_id="cron", pid=os.getpid(), updated_at=self.stamp(2),
                       last_event_at=self.stamp(2), last_prompt="[cron] check")
        self.assertEqual(self.snapshot()["cron"]["state"], "running")


class MarkTests(AgentBoardTestCase):
    def test_mark_done(self):
        insert_session(self.conn, session_id="s1")
        args = cli.build_parser().parse_args(["mark", "claude", "s1", "done"])
        cli.cmd_mark(args)
        row = self.conn.execute("SELECT * FROM triage WHERE tool='claude' AND session_id='s1'").fetchone()
        self.assertEqual(row["status"], "done")

    def test_mark_snooze_with_hours(self):
        insert_session(self.conn, session_id="s1")
        args = cli.build_parser().parse_args(["mark", "claude", "s1", "snooze", "--hours", "2"])
        cli.cmd_mark(args)
        row = self.conn.execute("SELECT * FROM triage WHERE tool='claude' AND session_id='s1'").fetchone()
        self.assertEqual(row["status"], "snoozed")
        self.assertIsNotNone(row["until"])

    def test_mark_open_clears_snooze(self):
        insert_session(self.conn, session_id="s1")
        cli.cmd_mark(cli.build_parser().parse_args(["mark", "claude", "s1", "snooze"]))
        cli.cmd_mark(cli.build_parser().parse_args(["mark", "claude", "s1", "open"]))
        row = self.conn.execute("SELECT * FROM triage WHERE tool='claude' AND session_id='s1'").fetchone()
        self.assertEqual(row["status"], "open")
        self.assertIsNone(row["until"])


def _no_shellouts(*args, **kwargs):
    raise AssertionError("subprocess.run should not be called here")


class AutoDoneEndToEndTests(AgentBoardTestCase):
    def _clean_exit_lifecycle(self, session_id="s1"):
        with mock.patch("agentboard.hook.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="")
            hook.process_payload(
                self.conn, "claude",
                {"session_id": session_id, "cwd": "/tmp", "hook_event_name": "SessionStart"},
            )
        with mock.patch("agentboard.hook.subprocess.run", side_effect=_no_shellouts):
            hook.process_payload(
                self.conn, "claude",
                {
                    "session_id": session_id, "cwd": "/tmp",
                    "hook_event_name": "UserPromptSubmit", "prompt": "do the thing",
                },
            )
            hook.process_payload(
                self.conn, "claude",
                {
                    "session_id": session_id, "cwd": "/tmp",
                    "hook_event_name": "Stop", "last_assistant_message": "done",
                },
            )
            hook.process_payload(
                self.conn, "claude",
                {
                    "session_id": session_id, "cwd": "/tmp",
                    "hook_event_name": "SessionEnd", "reason": "prompt_input_exit",
                },
            )

    def test_full_lifecycle_becomes_auto_done(self):
        self._clean_exit_lifecycle()
        snapshot = cli.build_snapshot(self.conn, include_all=True)
        entry = snapshot["sessions"][0]
        self.assertEqual(entry["state"], "done")
        self.assertTrue(entry["auto_done"])
        self.assertEqual(entry["done_reason"], "clean exit")

    def test_mark_open_after_clean_exit_suppresses_auto_done(self):
        self._clean_exit_lifecycle()
        cli.cmd_mark(cli.build_parser().parse_args(["mark", "claude", "s1", "open"]))
        snapshot = cli.build_snapshot(self.conn, include_all=True)
        entry = snapshot["sessions"][0]
        self.assertEqual((entry["state"], entry["waiting_reason"]), ("unconcluded", "Reopened"))
        self.assertFalse(entry["auto_done"])

    def test_resume_after_clean_exit_clears_ended_at_and_reopens(self):
        self._clean_exit_lifecycle()
        row = self.conn.execute(
            "SELECT ended_at FROM sessions WHERE tool='claude' AND session_id='s1'"
        ).fetchone()
        self.assertIsNotNone(row["ended_at"])

        with mock.patch("agentboard.hook.subprocess.run", side_effect=_no_shellouts):
            hook.process_payload(
                self.conn, "claude",
                {
                    "session_id": "s1", "cwd": "/tmp",
                    "hook_event_name": "UserPromptSubmit", "prompt": "one more thing",
                },
            )
        row = self.conn.execute(
            "SELECT ended_at, end_reason FROM sessions WHERE tool='claude' AND session_id='s1'"
        ).fetchone()
        self.assertIsNone(row["ended_at"])
        self.assertIsNone(row["end_reason"])

        snapshot = cli.build_snapshot(self.conn, include_all=True)
        entry = snapshot["sessions"][0]
        self.assertNotEqual(entry["state"], "done")
        self.assertFalse(entry["auto_done"])


class LinkStatusCommandTests(AgentBoardTestCase):
    def test_link_command_resolves_by_cwd_and_stores(self):
        insert_session(self.conn, session_id="s1", cwd="/tmp/proj")
        args = cli.build_parser().parse_args([
            "link", "--kind", "pr", "--ref", "#5", "--url", "https://x/5", "--state", "OPEN",
        ])
        with mock.patch("agentboard.links.os.getcwd", return_value="/tmp/proj"):
            with mock.patch("agentboard.links.resolve_session_from_env", return_value=None):
                with mock.patch("agentboard.links.resolve_session_from_ancestors", return_value=None):
                    cli.cmd_link(args)
        row = self.conn.execute(
            "SELECT * FROM links WHERE tool='claude' AND session_id='s1'"
        ).fetchone()
        self.assertEqual(row["ref"], "#5")

    def test_status_command_sets_status_text(self):
        insert_session(self.conn, session_id="s1", cwd="/tmp/proj")
        args = cli.build_parser().parse_args(["status", "--text", "hacking"])
        with mock.patch("agentboard.links.os.getcwd", return_value="/tmp/proj"):
            with mock.patch("agentboard.links.resolve_session_from_env", return_value=None):
                with mock.patch("agentboard.links.resolve_session_from_ancestors", return_value=None):
                    cli.cmd_status(args)
        row = self.conn.execute(
            "SELECT status_text FROM sessions WHERE tool='claude' AND session_id='s1'"
        ).fetchone()
        self.assertEqual(row["status_text"], "hacking")

    def test_status_reports_model_and_context_without_touching_the_text(self):
        insert_session(self.conn, session_id="s1", status_text="kept")
        with _no_auto_resolution():
            rc, _, err = _run_cmd(["status", "--session", "claude:s1", "--model",
                                   "acme-large-2", "--context-tokens", "51200"])
        self.assertEqual((rc, err), (0, ""))
        row = self.conn.execute(
            "SELECT model, context_tokens, context_at, status_text FROM sessions "
            "WHERE session_id='s1'").fetchone()
        self.assertEqual((row["model"], row["context_tokens"], row["status_text"]),
                         ("acme-large-2", 51200, "kept"))
        self.assertIsNotNone(reconcile.parse_ts(row["context_at"]))

    def test_status_context_replaces_a_stored_future_time(self):
        # status measures "now", so only a future stored time could be
        # newer; that one is bogus and must not freeze the size.
        insert_session(self.conn, session_id="s1", context_tokens=900,
                       context_at="2999-01-01T00:00:00.000Z")
        with _no_auto_resolution():
            _run_cmd(["status", "--session", "claude:s1", "--context-tokens", "5"])
        row = self.conn.execute(
            "SELECT context_tokens, context_at FROM sessions WHERE session_id='s1'").fetchone()
        self.assertEqual(row["context_tokens"], 5)
        self.assertLess(reconcile.parse_ts(row["context_at"]),
                        datetime.now(timezone.utc) + timedelta(seconds=5))

    def test_inbox_future_context_at_is_clamped_and_status_replaces_it(self):
        insert_session(self.conn, session_id="s1")
        inbox = self.agentboard_home / "inbox"
        inbox.mkdir()
        (inbox / "m.json").write_text(json.dumps({
            "session": "claude:s1", "context_tokens": 51200,
            "context_at": "2099-01-01T00:00:00Z"}))
        now = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
        cli.build_snapshot(self.conn, include_all=True, now=now)
        row = self.conn.execute(
            "SELECT context_tokens, context_at FROM sessions WHERE session_id='s1'").fetchone()
        self.assertEqual((row["context_tokens"], row["context_at"]),
                         (51200, "2026-10-24T12:00:00.000000Z"))
        with _no_auto_resolution():
            _run_cmd(["status", "--session", "claude:s1", "--context-tokens", "7000"])
        row = self.conn.execute(
            "SELECT context_tokens FROM sessions WHERE session_id='s1'").fetchone()
        self.assertEqual(row["context_tokens"], 7000)

    def test_status_rejects_a_context_size_beyond_sqlite_integer(self):
        insert_session(self.conn, session_id="s1")
        err = io.StringIO()
        with _no_auto_resolution(), contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit) as raised:
            cli.build_parser().parse_args(
                ["status", "--session", "claude:s1", "--context-tokens", str(2 ** 63)])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("below 2**63", err.getvalue())

    def test_status_needs_something_to_set(self):
        insert_session(self.conn, session_id="s1")
        with _no_auto_resolution():
            rc, _, err = _run_cmd(["status", "--session", "claude:s1"])
        self.assertEqual(rc, 2)
        self.assertIn("--text, --model or --context-tokens", err)
        with _no_auto_resolution():
            rc, _, err = _run_cmd(["status", "--session", "claude:s1", "--model", " "])
        self.assertEqual(rc, 2)
        for bad in ("-1", "12k"):
            with self.subTest(bad=bad), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    cli.build_parser().parse_args(["status", "--context-tokens", bad])

    def test_status_ttl_without_text_is_rejected(self):
        insert_session(self.conn, session_id="s1", status_text="kept")
        with _no_auto_resolution():
            rc, _, err = _run_cmd(["status", "--session", "claude:s1", "--model", "m",
                                   "--ttl", "5"])
        self.assertEqual(rc, 2)
        self.assertIn("--ttl applies to --text", err)
        row = self.conn.execute(
            "SELECT model, status_text FROM sessions WHERE session_id='s1'").fetchone()
        self.assertEqual((row["model"], row["status_text"]), (None, "kept"))
        with _no_auto_resolution():
            rc, _, err = _run_cmd(["status", "--session", "claude:s1", "--text", "busy",
                                   "--ttl", "5"])
        self.assertEqual((rc, err), (0, ""))
        row = self.conn.execute(
            "SELECT status_text, status_until FROM sessions WHERE session_id='s1'").fetchone()
        self.assertEqual(row["status_text"], "busy")
        self.assertIsNotNone(row["status_until"])

    def test_link_unresolvable_exits_zero(self):
        args = cli.build_parser().parse_args(["link", "--kind", "pr", "--ref", "#5"])
        with mock.patch("agentboard.links.os.getcwd", return_value="/no/such/dir"):
            with mock.patch("agentboard.links.resolve_session_from_env", return_value=None):
                with mock.patch("agentboard.links.resolve_session_from_ancestors", return_value=None):
                    result = cli.cmd_link(args)
        self.assertEqual(result, 0)


def _no_auto_resolution():
    """Context that disables env/ancestor session resolution so a test
    resolves only by the patched cwd."""
    stack = contextlib.ExitStack()
    stack.enter_context(mock.patch("agentboard.links.resolve_session_from_env", return_value=None))
    stack.enter_context(
        mock.patch("agentboard.links.resolve_session_from_ancestors", return_value=None)
    )
    return stack


def _run_cmd(argv):
    args = cli.build_parser().parse_args(argv)
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = args.func(args)
    return rc, out.getvalue(), err.getvalue()


def _insert_event(conn, event, text=None, session_id="s1", tool="claude", ts=None,
                  agent_id=None):
    ts = ts or "2026-06-15T10:00:00.000000Z"
    cur = conn.execute(
        "INSERT INTO events(ts, tool, session_id, event, agent_id, text) VALUES (?,?,?,?,?,?)",
        (ts, tool, session_id, event, agent_id, text),
    )
    return cur.lastrowid


def _set_hook_from(conn, value=None):
    """Simulate migration 5 running over the events inserted so far."""
    if value is None:
        value = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
    conn.execute("UPDATE notes_meta SET value=? WHERE key='hook_from'", (value,))


def _notes_rows(conn, where="1=1"):
    return conn.execute(f"SELECT * FROM notes WHERE {where} ORDER BY id").fetchall()


TICK = "# Autonomous loop tick (dynamic pacing)"
MILESTONE = "PR #23 is merged as 61cffa88 on main; the task is now in Completed."


class NoteCommandTests(AgentBoardTestCase):
    def test_note_resolves_session_like_link(self):
        insert_session(self.conn, session_id="s1", cwd="/tmp/proj")
        with mock.patch("agentboard.links.os.getcwd", return_value="/tmp/proj"), _no_auto_resolution():
            rc, out, _ = _run_cmd(["note", "--kind", "needs_you", "--text", "approve the plan"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        (row,) = _notes_rows(self.conn)
        self.assertEqual((row["tool"], row["session_id"]), ("claude", "s1"))
        self.assertEqual(row["kind"], "needs_you")
        self.assertEqual(row["source"], "cli")
        self.assertIsNone(row["event_id"])

    def test_note_explicit_session(self):
        rc, _, _ = _run_cmd(["note", "--kind", "update", "--text", "PR #4 merged",
                             "--session", "codex:abc"])
        self.assertEqual(rc, 0)
        (row,) = _notes_rows(self.conn)
        self.assertEqual((row["tool"], row["session_id"]), ("codex", "abc"))

    def test_note_unresolved_warns_and_exits_zero(self):
        with mock.patch("agentboard.links.os.getcwd", return_value="/no/such/dir"), _no_auto_resolution():
            rc, out, err = _run_cmd(["note", "--kind", "update", "--text", "x"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertIn("could not resolve a session", err)
        self.assertEqual(_notes_rows(self.conn), [])

    def test_note_empty_text_is_an_error(self):
        rc, _, err = _run_cmd(["note", "--kind", "update", "--text", "  ", "--session", "claude:s1"])
        self.assertEqual(rc, 1)
        self.assertIn("--text is empty", err)

    def test_note_kind_is_validated(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["note", "--kind", "chatter", "--text", "x"])

    def test_note_flags_do_not_abbreviate(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["note", "--kind", "update", "--te", "x"])

    def test_repeat_cli_note_bumps_count(self):
        for _ in range(2):
            _run_cmd(["note", "--kind", "update", "--text", "PR #4 merged", "--session", "claude:s1"])
        (row,) = _notes_rows(self.conn)
        self.assertEqual(row["count"], 2)


class NotesCommandTests(AgentBoardTestCase):
    def _add(self, text, session="claude:s1", kind="update"):
        _run_cmd(["note", "--kind", kind, "--text", text, "--session", session])

    def test_notes_json_shape(self):
        self._add("USER_ACTION: approve", kind="needs_you")
        rc, out, _ = _run_cmd(["notes", "--json"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        (note,) = data["notes"]
        self.assertEqual(
            set(note),
            {"id", "key", "tool", "session_id", "kind", "text", "ts", "last_ts", "count",
             "source", "dismissed_at"},
        )
        self.assertEqual(note["key"], "claude:s1")
        self.assertEqual(note["kind"], "needs_you")
        self.assertIsNone(note["dismissed_at"])

    def test_notes_empty_json(self):
        rc, out, _ = _run_cmd(["notes", "--json"])
        self.assertEqual((rc, json.loads(out)), (0, {"notes": []}))

    def test_notes_newest_first_and_session_filter(self):
        self._add("first", session="claude:a")
        self._add("second", session="claude:b")
        data = json.loads(_run_cmd(["notes", "--json"])[1])
        self.assertEqual([n["text"] for n in data["notes"]], ["second", "first"])
        data = json.loads(_run_cmd(["notes", "--json", "--session", "claude:a"])[1])
        self.assertEqual([n["text"] for n in data["notes"]], ["first"])

    def test_dismiss_hides_note_unless_all(self):
        self._add("one")
        self._add("two")
        ids = [n["id"] for n in json.loads(_run_cmd(["notes", "--json"])[1])["notes"]]
        rc, out, _ = _run_cmd(["notes", "--dismiss", str(ids[0])])
        self.assertEqual(rc, 0)
        self.assertIn("dismissed 1 note(s)", out)
        remaining = json.loads(_run_cmd(["notes", "--json"])[1])["notes"]
        self.assertEqual([n["id"] for n in remaining], [ids[1]])
        everything = json.loads(_run_cmd(["notes", "--json", "--all"])[1])["notes"]
        self.assertEqual(len(everything), 2)
        self.assertIsNotNone(
            next(n for n in everything if n["id"] == ids[0])["dismissed_at"]
        )

    def test_dismiss_unknown_or_already_dismissed_counts_zero(self):
        self._add("one")
        note_id = json.loads(_run_cmd(["notes", "--json"])[1])["notes"][0]["id"]
        _run_cmd(["notes", "--dismiss", str(note_id)])
        rc, out, _ = _run_cmd(["notes", "--dismiss", str(note_id), "999", "--json"])
        self.assertEqual((rc, json.loads(out)), (0, {"dismissed": 0}))

    def test_dismiss_all_scoped_to_session(self):
        self._add("a1", session="claude:a")
        self._add("a2", session="claude:a")
        self._add("b1", session="claude:b")
        rc, out, _ = _run_cmd(["notes", "--dismiss-all", "--session", "claude:a"])
        self.assertIn("dismissed 2 note(s)", out)
        remaining = json.loads(_run_cmd(["notes", "--json"])[1])["notes"]
        self.assertEqual([n["text"] for n in remaining], ["b1"])
        _run_cmd(["notes", "--dismiss-all"])
        self.assertEqual(json.loads(_run_cmd(["notes", "--json"])[1])["notes"], [])

    def test_dismiss_all_global_reaches_every_session_and_past_any_cap(self):
        for i in range(25):
            self._add(f"a{i}", session="claude:a")
        self._add("b1", session="codex:b", kind="needs_you")
        rc, out, _ = _run_cmd(["notes", "--dismiss-all", "--json"])
        self.assertEqual((rc, json.loads(out)), (0, {"dismissed": 26}))
        self.assertEqual(json.loads(_run_cmd(["notes", "--json"])[1])["notes"], [])

    def test_dismiss_all_kind_scoped(self):
        self._add("a-update", session="claude:a")
        self._add("a-error", session="claude:a", kind="error")
        self._add("a-ask", session="claude:a", kind="needs_you")
        self._add("b-update", session="claude:b")
        rc, out, _ = _run_cmd(["notes", "--dismiss-all", "--session", "claude:a",
                               "--kind", "update", "error", "--json"])
        self.assertEqual((rc, json.loads(out)), (0, {"dismissed": 2}))
        remaining = json.loads(_run_cmd(["notes", "--json"])[1])["notes"]
        self.assertEqual(sorted(n["text"] for n in remaining), ["a-ask", "b-update"])
        # Without --session the kind scope covers every session.
        rc, out, _ = _run_cmd(["notes", "--dismiss-all", "--kind", "update", "--json"])
        self.assertEqual(json.loads(out), {"dismissed": 1})
        remaining = json.loads(_run_cmd(["notes", "--json"])[1])["notes"]
        self.assertEqual([n["text"] for n in remaining], ["a-ask"])

    def test_dismiss_all_leaves_dismissed_notes_untouched(self):
        self._add("old")
        old_id = json.loads(_run_cmd(["notes", "--json"])[1])["notes"][0]["id"]
        _run_cmd(["notes", "--dismiss", str(old_id)])
        before = _notes_rows(self.conn, f"id={old_id}")[0]["dismissed_at"]
        self._add("new")
        rc, out, _ = _run_cmd(["notes", "--dismiss-all", "--json"])
        self.assertEqual(json.loads(out), {"dismissed": 1})
        self.assertEqual(_notes_rows(self.conn, f"id={old_id}")[0]["dismissed_at"], before)

    def test_kind_filters_the_listing(self):
        self._add("u1")
        self._add("ask", kind="needs_you")
        data = json.loads(_run_cmd(["notes", "--json", "--kind", "needs_you"])[1])
        self.assertEqual([n["text"] for n in data["notes"]], ["ask"])

    def test_kind_with_dismiss_ids_or_rebuild_is_an_error(self):
        for argv in (["notes", "--dismiss", "1", "--kind", "update"],
                     ["notes", "--rebuild", "--kind", "update"]):
            with self.subTest(argv=argv):
                rc, _, err = _run_cmd(argv)
                self.assertEqual(rc, 2)
                self.assertIn("--kind", err)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["notes", "--dismiss-all", "--kind", "chatter"])

    def test_dismissed_note_recurs_as_new_row(self):
        self._add("PR #4 merged")
        _run_cmd(["notes", "--dismiss-all"])
        self._add("PR #4 merged")
        rows = _notes_rows(self.conn)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["count"], 1)

    def test_days_without_rebuild_is_an_error(self):
        rc, _, err = _run_cmd(["notes", "--days", "3"])
        self.assertEqual(rc, 2)
        self.assertIn("need --rebuild", err)

    def test_dismiss_and_rebuild_are_exclusive(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["notes", "--dismiss", "1", "--rebuild"])

    def test_text_listing(self):
        self._add("PR #4 merged\nmore detail")
        self._add("PR #4 merged\nmore detail")
        rc, out, _ = _run_cmd(["notes"])
        self.assertEqual(rc, 0)
        self.assertIn("claude:s1", out)
        self.assertIn("x2", out)
        self.assertIn("PR #4 merged", out)
        self.assertNotIn("more detail", out)


class NotesRebuildTests(AgentBoardTestCase):
    def _historic_corpus(self):
        _insert_event(self.conn, "UserPromptSubmit", TICK, ts="2026-06-15T09:00:00.000000Z")
        _insert_event(self.conn, "Stop", "USER_ACTION: approve the plan",
                      ts="2026-06-15T09:01:00.000000Z")
        _insert_event(self.conn, "UserPromptSubmit", TICK, ts="2026-06-15T09:10:00.000000Z")
        _insert_event(self.conn, "Stop", "Quiet tick, backoff window, nothing changed.",
                      ts="2026-06-15T09:11:00.000000Z")
        _insert_event(self.conn, "UserPromptSubmit", TICK, ts="2026-06-15T09:20:00.000000Z")
        _insert_event(self.conn, "Stop", "USER_ACTION: approve the plan",
                      ts="2026-06-15T09:21:00.000000Z")
        _insert_event(self.conn, "Stop", MILESTONE, ts="2026-06-15T09:22:00.000000Z")
        # A subagent Stop and an attended milestone never become notes.
        _insert_event(self.conn, "Stop", "USER_ACTION: sub", agent_id="a1",
                      ts="2026-06-15T09:23:00.000000Z")
        _insert_event(self.conn, "UserPromptSubmit", "please merge it", session_id="s2",
                      ts="2026-06-15T09:30:00.000000Z")
        _insert_event(self.conn, "Stop", MILESTONE, session_id="s2",
                      ts="2026-06-15T09:31:00.000000Z")
        _set_hook_from(self.conn)

    def _counts(self):
        return {(r["session_id"], r["kind"]): r["count"] for r in _notes_rows(self.conn)}

    def test_rebuild_imports_historic_stop_events(self):
        self._historic_corpus()
        rc, out, _ = _run_cmd(["notes", "--rebuild"])
        self.assertEqual(rc, 0)
        self.assertIn("recorded=3", out)
        self.assertEqual(self._counts(), {("s1", "needs_you"): 2, ("s1", "update"): 1})
        needs_you = _notes_rows(self.conn, "kind='needs_you'")[0]
        self.assertEqual(needs_you["source"], "backfill")
        self.assertEqual(needs_you["ts"], "2026-06-15T09:01:00.000000Z")
        self.assertEqual(needs_you["last_ts"], "2026-06-15T09:21:00.000000Z")
        self.assertEqual((needs_you["event_id"], needs_you["last_event_id"]), (2, 6))

    def test_rebuild_twice_leaves_counts_unchanged(self):
        self._historic_corpus()
        _run_cmd(["notes", "--rebuild"])
        first = self._counts()
        rc, out, _ = _run_cmd(["notes", "--rebuild"])
        self.assertIn("recorded=0", out)
        self.assertEqual(self._counts(), first)

    def test_hook_written_note_does_not_block_older_events(self):
        self._historic_corpus()
        # The hook classifies an event above hook_from first.
        with mock.patch("agentboard.hook.subprocess.run") as run, \
             mock.patch("agentboard.hook.subprocess.Popen"):
            run.return_value = mock.Mock(returncode=0, stdout="")
            hook.process_payload(self.conn, "claude", {
                "session_id": "s1", "cwd": "/tmp", "hook_event_name": "Stop",
                "last_assistant_message": "USER_ACTION: approve the plan",
            })
        (hook_note,) = _notes_rows(self.conn)
        self.assertGreater(hook_note["event_id"], 10)
        _run_cmd(["notes", "--rebuild"])
        note = _notes_rows(self.conn, "kind='needs_you'")[0]
        self.assertEqual(note["count"], 3)
        self.assertEqual(note["event_id"], 2)
        self.assertEqual(note["last_event_id"], hook_note["event_id"])
        # Replaying the older events never moved last_ts backwards.
        self.assertEqual(note["last_ts"], hook_note["last_ts"])

    def test_reset_keeps_dismissed_and_cli_notes_and_does_not_reopen(self):
        self._historic_corpus()
        _run_cmd(["notes", "--rebuild"])
        _run_cmd(["note", "--kind", "update", "--text", "manual note", "--session", "claude:s1"])
        needs_you_id = _notes_rows(self.conn, "kind='needs_you'")[0]["id"]
        _run_cmd(["notes", "--dismiss", str(needs_you_id)])
        rc, out, _ = _run_cmd(["notes", "--rebuild", "--reset"])
        self.assertEqual(rc, 0)
        self.assertIn("deleted=1", out)
        self.assertIn("skipped_dismissed=2", out)
        rows = _notes_rows(self.conn)
        open_rows = [r for r in rows if r["dismissed_at"] is None]
        self.assertEqual(sorted(r["text"] for r in open_rows), sorted([MILESTONE, "manual note"]))
        dismissed = [r for r in rows if r["dismissed_at"] is not None]
        self.assertEqual([r["id"] for r in dismissed], [needs_you_id])
        # A second reset is stable.
        _run_cmd(["notes", "--rebuild", "--reset"])
        self.assertEqual(len([r for r in _notes_rows(self.conn) if r["dismissed_at"] is None]), 2)

    def test_reset_then_plain_rebuild_is_noop(self):
        self._historic_corpus()
        _run_cmd(["notes", "--rebuild", "--reset"])
        before = self._counts()
        rc, out, _ = _run_cmd(["notes", "--rebuild"])
        self.assertIn("recorded=0", out)
        self.assertEqual(self._counts(), before)

    def test_rebuild_days_narrows_window(self):
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        recent = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        _insert_event(self.conn, "Stop", "USER_ACTION: old request", ts=old)
        _insert_event(self.conn, "Stop", "USER_ACTION: new request", ts=recent)
        _set_hook_from(self.conn)
        _run_cmd(["notes", "--rebuild", "--days", "3"])
        self.assertEqual([r["text"] for r in _notes_rows(self.conn)], ["USER_ACTION: new request"])

    def test_rebuild_json_stats(self):
        self._historic_corpus()
        rc, out, _ = _run_cmd(["notes", "--rebuild", "--json"])
        stats = json.loads(out)
        self.assertEqual(stats["recorded"], 3)
        self.assertEqual(stats["upper"], 10)

    def test_rebuild_seen_refs_follow_event_order(self):
        _insert_event(self.conn, "UserPromptSubmit", TICK, ts="2026-06-15T09:00:00.000000Z")
        _insert_event(self.conn, "Stop", "The builder is still fixing PR #4's findings.",
                      ts="2026-06-15T09:01:00.000000Z")
        _insert_event(self.conn, "Stop", "The builder is still fixing PR #4's findings.",
                      ts="2026-06-15T09:02:00.000000Z")
        _insert_event(self.conn, "Stop", "Tick: PR #4 is in hand; PR #5 is ready for your review.",
                      ts="2026-06-15T09:03:00.000000Z")
        _insert_event(self.conn, "Stop", "PR #5 merged as abc1234.",
                      ts="2026-06-15T09:04:00.000000Z")
        _set_hook_from(self.conn)
        _run_cmd(["notes", "--rebuild"])
        rows = _notes_rows(self.conn)
        self.assertEqual(
            [(r["text"], r["count"]) for r in rows],
            [("The builder is still fixing PR #4's findings.", 1),
             ("Tick: PR #4 is in hand; PR #5 is ready for your review.", 1),
             ("PR #5 merged as abc1234.", 1)],
        )
        # A reset replays the same way: a kept note first seen at a later
        # event does not make an earlier first mention look repeated.
        _run_cmd(["notes", "--dismiss", str(rows[2]["id"])])
        rc, out, _ = _run_cmd(["notes", "--rebuild", "--reset"])
        self.assertEqual(rc, 0)
        open_texts = [r["text"] for r in _notes_rows(self.conn, "dismissed_at IS NULL")]
        self.assertEqual(
            open_texts,
            ["The builder is still fixing PR #4's findings.",
             "Tick: PR #4 is in hand; PR #5 is ready for your review."],
        )

    def test_rebuild_on_empty_db(self):
        rc, out, _ = _run_cmd(["notes", "--rebuild"])
        self.assertEqual(rc, 0)
        self.assertIn("recorded=0", out)

    def _hook(self, payload):
        with mock.patch("agentboard.hook.subprocess.run") as run, \
             mock.patch("agentboard.hook.subprocess.Popen"):
            run.return_value = mock.Mock(returncode=0, stdout="")
            hook.process_payload(self.conn, "claude", {"cwd": "/tmp", **payload})

    def test_reset_recreates_stop_failure_error_notes(self):
        self._hook({"session_id": "s1", "hook_event_name": "UserPromptSubmit", "prompt": "go"})
        self._hook({"session_id": "s1", "hook_event_name": "StopFailure", "error": "rate_limit",
                    "last_assistant_message": "API Error: Rate limit reached"})
        self._hook({"session_id": "s2", "hook_event_name": "StopFailure",
                    "error": "authentication_failed",
                    "last_assistant_message": "Invalid API key"})
        # A subagent's failure records no note, before or after the rebuild.
        self._hook({"session_id": "s1", "hook_event_name": "StopFailure", "agent_id": "a1",
                    "error": "overloaded", "last_assistant_message": "API Error: Overloaded"})
        written = {r["session_id"]: r["text"] for r in _notes_rows(self.conn)}
        self.assertEqual(written, {"s1": "rate_limit: API Error: Rate limit reached",
                                   "s2": "authentication_failed: Invalid API key"})
        s2_note = _notes_rows(self.conn, "session_id='s2'")[0]["id"]
        _run_cmd(["notes", "--dismiss", str(s2_note)])
        rc, out, _ = _run_cmd(["notes", "--rebuild", "--reset"])
        self.assertEqual(rc, 0)
        self.assertIn("deleted=1", out)
        self.assertIn("skipped_dismissed=1", out)
        open_rows = _notes_rows(self.conn, "dismissed_at IS NULL")
        self.assertEqual(
            [(r["session_id"], r["kind"], r["text"], r["source"]) for r in open_rows],
            [("s1", "error", "rate_limit: API Error: Rate limit reached", "backfill")],
        )


class InstallHooksTests(AgentBoardTestCase):
    def setUp(self):
        super().setUp()
        self.claude_dir = self.fake_home / ".claude"
        self.codex_dir = self.fake_home / ".codex"
        self.settings_path = self.claude_dir / "settings.json"
        self.hooks_path = self.codex_dir / "hooks.json"
        shutil.copy(FIXTURES / "claude_settings.json", self.settings_path)
        shutil.copy(FIXTURES / "codex_hooks.json", self.hooks_path)

    def _settings(self):
        return json.loads(self.settings_path.read_text())

    def _hooks(self):
        return json.loads(self.hooks_path.read_text())

    def test_dry_run_makes_no_changes(self):
        before = self.settings_path.read_text()
        args = cli.build_parser().parse_args(["install-hooks", "--claude", "--dry-run"])
        cli.cmd_install_hooks(args)
        self.assertEqual(self.settings_path.read_text(), before)
        self.assertEqual(list(self.claude_dir.glob("*.agentboard-bak-*")), [])

    def test_install_preserves_existing_hooks_and_keys(self):
        args = cli.build_parser().parse_args(["install-hooks", "--claude"])
        cli.cmd_install_hooks(args)
        data = self._settings()
        self.assertEqual(data["model"], "opus")
        self.assertEqual(data["env"]["SOME_EXISTING_VAR"], "1")
        self.assertEqual(data["statusLine"]["command"], "~/.claude/statusline.py")
        notif_commands = [
            h["command"] for group in data["hooks"]["Notification"] for h in group["hooks"]
        ]
        self.assertIn("~/.claude/notify.py 'Claude needs input' 'Claude Code'", notif_commands)
        self.assertTrue(any("agentboard hook --tool claude" in c for c in notif_commands))

    def test_install_creates_backup(self):
        args = cli.build_parser().parse_args(["install-hooks", "--claude"])
        cli.cmd_install_hooks(args)
        backups = list(self.claude_dir.glob("settings.json.agentboard-bak-*"))
        self.assertEqual(len(backups), 1)

    def test_non_ascii_values_are_not_escaped(self):
        # json.dumps(..., ensure_ascii=False): a gratuitous \uXXXX diff in a
        # user-owned file is exactly the kind of noise install-hooks must
        # not introduce.
        data = self._settings()
        data["env"]["GREETING"] = "héllo ✓"
        self.settings_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        args = cli.build_parser().parse_args(["install-hooks", "--claude"])
        cli.cmd_install_hooks(args)
        text = self.settings_path.read_text(encoding="utf-8")
        self.assertIn("héllo ✓", text)
        self.assertNotIn("\\u00e9", text)

    def test_install_is_idempotent(self):
        args = cli.build_parser().parse_args(["install-hooks", "--claude"])
        cli.cmd_install_hooks(args)
        first = self._settings()
        cli.cmd_install_hooks(args)
        second = self._settings()
        self.assertEqual(first, second)
        for event, groups in second["hooks"].items():
            agentboard_groups = [
                g for g in groups
                if any("agentboard hook --tool claude" in h.get("command", "") for h in g["hooks"])
            ]
            self.assertLessEqual(len(agentboard_groups), 1, f"duplicate agentboard hook in {event}")

    def test_migrates_old_repo_path_entry_in_place(self):
        old_command = "/Users/someone/src/agentboard/core/bin/agentboard hook --tool claude"
        new_command = ("/Users/someone/Library/Application Support/AgentBoard/"
                        "core/bin/agentboard hook --tool claude")
        data = self._settings()
        data["hooks"]["Stop"].append(
            {"hooks": [{"type": "command", "command": old_command, "timeout": 5}]}
        )
        self.settings_path.write_text(json.dumps(data, indent=2))

        _, _, changed = cli.install_claude_hooks(self.settings_path, new_command, False)
        self.assertTrue(changed)

        after = self._settings()
        stop_commands = [h["command"] for g in after["hooks"]["Stop"] for h in g["hooks"]]
        self.assertIn(new_command, stop_commands)
        self.assertNotIn(old_command, stop_commands)
        agentboard_groups = [
            g for g in after["hooks"]["Stop"]
            if any(
                cli._agentboard_hook_suffix(h["command"]) == cli._agentboard_hook_suffix(new_command)
                for h in g["hooks"]
            )
        ]
        self.assertEqual(len(agentboard_groups), 1, "migration must not duplicate the entry")

    def test_migrates_unquoted_space_path_entry(self):
        # The actual live bug: an entry written with an unquoted install
        # path that happens to contain a space (the old Application Support
        # default) is invalid shell syntax, but install-hooks must still
        # recognize and migrate it rather than leaving it in place as a
        # duplicate alongside a newly added, correctly quoted entry.
        old_command = (
            "/Users/someone/Library/Application Support/AgentBoard/core/bin/agentboard "
            "hook --tool claude"
        )
        new_command = "/Users/someone/.local/share/agentboard/core/bin/agentboard hook --tool claude"
        data = self._settings()
        data["hooks"]["Stop"].append(
            {"hooks": [{"type": "command", "command": old_command, "timeout": 5}]}
        )
        self.settings_path.write_text(json.dumps(data, indent=2))

        _, _, changed = cli.install_claude_hooks(self.settings_path, new_command, False)
        self.assertTrue(changed)

        after = self._settings()
        stop_commands = [h["command"] for g in after["hooks"]["Stop"] for h in g["hooks"]]
        self.assertIn(new_command, stop_commands)
        self.assertNotIn(old_command, stop_commands)
        agentboard_groups = [
            g for g in after["hooks"]["Stop"]
            if any(
                cli._agentboard_hook_suffix(h["command"]) == cli._agentboard_hook_suffix(new_command)
                for h in g["hooks"]
            )
        ]
        self.assertEqual(len(agentboard_groups), 1, "migration must not duplicate the entry")

    def test_agentboard_hook_suffix_is_quote_and_space_independent(self):
        plain = "/Users/x/.local/share/agentboard/core/bin/agentboard hook --tool claude"
        quoted = "'/Users/x/Library/Application Support/AgentBoard/core/bin/agentboard' hook --tool claude"
        unquoted_with_space = (
            "/Users/x/Library/Application Support/AgentBoard/core/bin/agentboard hook --tool claude"
        )
        suffix = cli._agentboard_hook_suffix(plain)
        self.assertEqual(suffix, "bin/agentboard hook --tool claude")
        self.assertEqual(cli._agentboard_hook_suffix(quoted), suffix)
        self.assertEqual(cli._agentboard_hook_suffix(unquoted_with_space), suffix)
        self.assertNotEqual(cli._agentboard_hook_suffix("~/.claude/notify.py 'done' 'Claude'"), suffix)

    def test_migrates_quoted_application_support_path_entry(self):
        # An old entry whose install path was shell-quoted (as cli.py does
        # for any path containing spaces, e.g. the old Application Support
        # default) must still be recognized by suffix and migrated in place.
        old_command = (
            "'/Users/someone/Library/Application Support/AgentBoard/core/bin/agentboard' "
            "hook --tool claude"
        )
        new_command = "/Users/someone/.local/share/agentboard/core/bin/agentboard hook --tool claude"
        data = self._settings()
        data["hooks"]["Stop"].append(
            {"hooks": [{"type": "command", "command": old_command, "timeout": 5}]}
        )
        self.settings_path.write_text(json.dumps(data, indent=2))

        _, _, changed = cli.install_claude_hooks(self.settings_path, new_command, False)
        self.assertTrue(changed)

        after = self._settings()
        stop_commands = [h["command"] for g in after["hooks"]["Stop"] for h in g["hooks"]]
        self.assertIn(new_command, stop_commands)
        self.assertNotIn(old_command, stop_commands)
        agentboard_groups = [
            g for g in after["hooks"]["Stop"]
            if any(
                cli._agentboard_hook_suffix(h["command"]) == cli._agentboard_hook_suffix(new_command)
                for h in g["hooks"]
            )
        ]
        self.assertEqual(len(agentboard_groups), 1, "migration must not duplicate the entry")

    def test_migration_preserves_other_hooks_in_the_same_group(self):
        old_command = "/Users/someone/src/agentboard/core/bin/agentboard hook --tool claude"
        new_command = "/Users/someone/Library/Application Support/AgentBoard/core/bin/agentboard hook --tool claude"
        data = self._settings()
        # a mixed group: someone else's hook plus our old-path entry
        data["hooks"]["Stop"].append({
            "hooks": [
                {"type": "command", "command": "/usr/local/bin/other-tool", "timeout": 10},
                {"type": "command", "command": old_command, "timeout": 5},
            ]
        })
        self.settings_path.write_text(json.dumps(data, indent=2))

        cli.install_claude_hooks(self.settings_path, new_command, False)

        after = self._settings()
        mixed_group = next(
            g for g in after["hooks"]["Stop"]
            if any(h["command"] == "/usr/local/bin/other-tool" for h in g["hooks"])
        )
        self.assertEqual(len(mixed_group["hooks"]), 2)
        commands = [h["command"] for h in mixed_group["hooks"]]
        self.assertIn("/usr/local/bin/other-tool", commands)
        self.assertIn(new_command, commands)

    def test_rerunning_after_migration_is_a_true_noop(self):
        old_command = "/Users/someone/src/agentboard/core/bin/agentboard hook --tool claude"
        new_command = "/Users/someone/Library/Application Support/AgentBoard/core/bin/agentboard hook --tool claude"
        data = self._settings()
        data["hooks"]["Stop"].append(
            {"hooks": [{"type": "command", "command": old_command, "timeout": 5}]}
        )
        self.settings_path.write_text(json.dumps(data, indent=2))
        cli.install_claude_hooks(self.settings_path, new_command, False)

        backups_after_migration = list(self.claude_dir.glob("settings.json.agentboard-bak-*"))
        text_after_migration = self.settings_path.read_text()
        mtime_after_migration = self.settings_path.stat().st_mtime_ns

        _, _, changed = cli.install_claude_hooks(self.settings_path, new_command, False)

        self.assertFalse(changed)
        self.assertEqual(
            list(self.claude_dir.glob("settings.json.agentboard-bak-*")), backups_after_migration
        )
        self.assertEqual(self.settings_path.read_text(), text_after_migration)
        self.assertEqual(self.settings_path.stat().st_mtime_ns, mtime_after_migration)

    def test_second_install_writes_no_second_backup_or_file_touch(self):
        args = cli.build_parser().parse_args(["install-hooks", "--claude"])
        cli.cmd_install_hooks(args)
        backups_after_first = list(self.claude_dir.glob("settings.json.agentboard-bak-*"))
        self.assertEqual(len(backups_after_first), 1)
        text_after_first = self.settings_path.read_text()
        mtime_after_first = self.settings_path.stat().st_mtime_ns

        cli.cmd_install_hooks(args)

        backups_after_second = list(self.claude_dir.glob("settings.json.agentboard-bak-*"))
        self.assertEqual(
            len(backups_after_second), 1,
            "a no-op re-install must not create a second backup",
        )
        self.assertEqual(self.settings_path.read_text(), text_after_first)
        self.assertEqual(
            self.settings_path.stat().st_mtime_ns, mtime_after_first,
            "a no-op re-install must not touch the file at all",
        )

    def test_uninstall_after_already_uninstalled_touches_nothing(self):
        cli.cmd_install_hooks(cli.build_parser().parse_args(["install-hooks", "--claude"]))
        cli.cmd_uninstall_hooks(cli.build_parser().parse_args(["uninstall-hooks", "--claude"]))
        backup_count_after_uninstall = len(
            list(self.claude_dir.glob("settings.json.agentboard-bak-*"))
        )
        self.assertGreaterEqual(backup_count_after_uninstall, 1)
        text_after_uninstall = self.settings_path.read_text()
        mtime_after_uninstall = self.settings_path.stat().st_mtime_ns

        cli.cmd_uninstall_hooks(cli.build_parser().parse_args(["uninstall-hooks", "--claude"]))

        backup_count_after_second = len(
            list(self.claude_dir.glob("settings.json.agentboard-bak-*"))
        )
        self.assertEqual(
            backup_count_after_second, backup_count_after_uninstall,
            "uninstalling when already uninstalled must not create another backup",
        )
        self.assertEqual(self.settings_path.read_text(), text_after_uninstall)
        self.assertEqual(self.settings_path.stat().st_mtime_ns, mtime_after_uninstall)

    def test_install_adds_missing_events(self):
        args = cli.build_parser().parse_args(["install-hooks", "--claude"])
        cli.cmd_install_hooks(args)
        data = self._settings()
        for event in cli.CLAUDE_EVENTS:
            self.assertIn(event, data["hooks"])

    def test_install_codex_preserves_existing(self):
        args = cli.build_parser().parse_args(["install-hooks", "--codex"])
        cli.cmd_install_hooks(args)
        data = self._hooks()
        stop_commands = [h["command"] for group in data["hooks"]["Stop"] for h in group["hooks"]]
        self.assertIn("~/.claude/notify.py 'Codex is done' 'Codex'", stop_commands)
        self.assertTrue(any("agentboard hook --tool codex" in c for c in stop_commands))

    def test_migrates_old_repo_path_entry_codex(self):
        old_command = "/Users/someone/src/agentboard/core/bin/agentboard hook --tool codex"
        new_command = "/Users/someone/Library/Application Support/AgentBoard/core/bin/agentboard hook --tool codex"
        data = self._hooks()
        data["hooks"]["Stop"].append(
            {"hooks": [{"type": "command", "command": old_command, "timeout": 5}]}
        )
        self.hooks_path.write_text(json.dumps(data, indent=2))

        _, _, changed = cli.install_codex_hooks(self.hooks_path, new_command, False)
        self.assertTrue(changed)

        after = self._hooks()
        stop_commands = [h["command"] for g in after["hooks"]["Stop"] for h in g["hooks"]]
        self.assertIn(new_command, stop_commands)
        self.assertNotIn(old_command, stop_commands)

    def test_uninstall_removes_only_agentboard_entries(self):
        cli.cmd_install_hooks(cli.build_parser().parse_args(["install-hooks", "--claude"]))
        cli.cmd_uninstall_hooks(cli.build_parser().parse_args(["uninstall-hooks", "--claude"]))
        data = self._settings()
        notif_commands = [
            h["command"] for group in data["hooks"]["Notification"] for h in group["hooks"]
        ]
        self.assertIn("~/.claude/notify.py 'Claude needs input' 'Claude Code'", notif_commands)
        self.assertFalse(any("agentboard hook" in c for c in notif_commands))

    def test_uninstall_dry_run_makes_no_changes(self):
        cli.cmd_install_hooks(cli.build_parser().parse_args(["install-hooks", "--claude"]))
        before = self.settings_path.read_text()
        cli.cmd_uninstall_hooks(cli.build_parser().parse_args(["uninstall-hooks", "--claude", "--dry-run"]))
        self.assertEqual(self.settings_path.read_text(), before)

    def test_installed_hook_command_survives_shell_quoting_from_a_home_with_a_space(self):
        # Claude/Codex both run the hook command through a shell, so a
        # command built from an unquoted path with a space in it (e.g. one
        # under a HOME with a space, or the old Application Support
        # default) would split into multiple words and fail. Stage a real
        # core/ copy under such a HOME, build the command the way
        # install-hooks does, and run that exact string through /bin/sh -c
        # with a sample hook payload -- it must still record the session.
        space_home = self.tmp_path / "fake home with space"
        core_dir = space_home / ".local" / "share" / "agentboard" / "core"
        repo_core = Path(__file__).resolve().parent.parent
        shutil.copytree(repo_core / "bin", core_dir / "bin")
        shutil.copytree(
            repo_core / "agentboard", core_dir / "agentboard",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        bin_path = core_dir / "bin" / "agentboard"
        bin_path.chmod(0o755)

        command = f"{shlex.quote(str(bin_path))} hook --tool claude"
        payload = json.dumps(
            {"session_id": "space-e2e", "cwd": "/tmp/proj", "hook_event_name": "SessionStart"}
        )

        result = subprocess.run(
            ["/bin/sh", "-c", command],
            input=payload, text=True, capture_output=True,
            env=_python3_stub_env(self.tmp_path / "stub-bin"),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE tool='claude' AND session_id='space-e2e'"
        ).fetchone()
        self.assertIsNotNone(row, "hook invocation through the shell must still record the session")

    def test_bin_path_fallback_does_not_resolve_symlinks(self):
        # F: install.sh's --dry-run preview passes --bin-path as-is, with
        # no symlink resolution. _bin_path() (used when --bin-path isn't
        # given, i.e. a real install) must match that -- otherwise a real
        # install run from a path that goes through a symlink (e.g.
        # macOS's /tmp -> /private/tmp) registers a different command
        # string than a dry-run preview of that same install would have
        # shown, and a dry-run run right afterwards shows a spurious diff
        # rewriting every hook command.
        real_tmp = Path("/tmp").resolve()
        if real_tmp == Path("/tmp"):
            self.skipTest("/tmp is not a symlink on this system")

        staging = tempfile.mkdtemp(prefix="agentboard-symlink-", dir="/tmp")
        self.addCleanup(shutil.rmtree, staging, ignore_errors=True)
        fake_cli_file = str(Path(staging) / "core" / "agentboard" / "cli.py")
        unresolved_bin = Path(staging) / "core" / "bin" / "agentboard"
        self.assertNotEqual(
            str(unresolved_bin), str(unresolved_bin.resolve()),
            "test setup must actually route through a symlink",
        )

        with mock.patch.object(cli, "__file__", fake_cli_file):
            bin_path = cli._bin_path()
        self.assertEqual(str(bin_path), str(unresolved_bin))

        # The same, through the actual command-building code path a real
        # install (no --bin-path) uses.
        with mock.patch.object(cli, "__file__", fake_cli_file):
            cli.cmd_install_hooks(cli.build_parser().parse_args(["install-hooks", "--claude"]))
        data = json.loads(self.settings_path.read_text())
        commands = [h["command"] for g in data["hooks"]["SessionStart"] for h in g["hooks"]]
        registered = next(c for c in commands if "agentboard hook" in c)
        self.assertIn(str(unresolved_bin), registered)
        self.assertNotIn(str(unresolved_bin.resolve()), registered)

    def test_shim_leaves_no_pycache_next_to_the_package(self):
        # Item 9: this exact copy of bin/agentboard is pinned into both
        # the installed core dir and the signed .app bundle (unchanged by
        # build-app.sh/install.sh); a __pycache__/ written into either
        # one at first run invalidates the bundle's code signature.
        staging = tempfile.mkdtemp(prefix="agentboard-pycache-")
        self.addCleanup(shutil.rmtree, staging, ignore_errors=True)
        core_dir = Path(staging) / "core"
        repo_core = Path(cli.__file__).resolve().parent.parent
        shutil.copytree(repo_core / "bin", core_dir / "bin")
        shutil.copytree(
            repo_core / "agentboard", core_dir / "agentboard",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        bin_path = core_dir / "bin" / "agentboard"
        bin_path.chmod(0o755)
        self.assertFalse((core_dir / "agentboard" / "__pycache__").exists())

        env = _python3_stub_env(self.tmp_path / "stub-bin")
        env["HOME"] = str(self.fake_home)
        env["AGENTBOARD_HOME"] = str(self.agentboard_home)
        env.pop("PYTHONDONTWRITEBYTECODE", None)

        for subcmd in (["paths", "--json"], ["hook", "--tool", "claude"]):
            result = subprocess.run(
                [str(bin_path)] + subcmd,
                input="{}", env=env, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

        self.assertFalse(
            (core_dir / "agentboard" / "__pycache__").exists(),
            "running the shim must not write __pycache__ next to the package",
        )
        self.assertFalse((core_dir / "bin" / "__pycache__").exists())


class MalformedHookConfigTests(AgentBoardTestCase):
    """A settings.json/hooks.json that isn't valid JSON must name the file,
    skip only that tool, still process the other tool (and, in install.sh,
    the backfill step), and leave the bad file completely untouched."""

    def setUp(self):
        super().setUp()
        self.claude_dir = self.fake_home / ".claude"
        self.codex_dir = self.fake_home / ".codex"
        self.settings_path = self.claude_dir / "settings.json"
        self.hooks_path = self.codex_dir / "hooks.json"

    def test_install_skips_bad_file_installs_the_other_and_exits_nonzero(self):
        bad_text = '{\n  "model": "opus",\n  "hooks": {,}\n'
        self.settings_path.write_text(bad_text)

        args = cli.build_parser().parse_args(["install-hooks", "--claude", "--codex"])
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = cli.cmd_install_hooks(args)

        self.assertEqual(rc, 1)
        self.assertIn(str(self.settings_path), buf.getvalue())
        self.assertIn("not valid JSON", buf.getvalue())
        # Untouched: no backup, same bytes.
        self.assertEqual(self.settings_path.read_text(), bad_text)
        self.assertEqual(list(self.claude_dir.glob("*.agentboard-bak-*")), [])
        # The other tool still gets installed.
        hooks = json.loads(self.hooks_path.read_text())
        stop_commands = [h["command"] for g in hooks["hooks"]["Stop"] for h in g["hooks"]]
        self.assertTrue(any("agentboard hook --tool codex" in c for c in stop_commands))

    def test_uninstall_skips_bad_file_removes_the_other_and_exits_nonzero(self):
        # hooks.json pre-exists with its own, non-agentboard hook so that
        # uninstalling it is actually observable (an install-created file
        # with nothing else in it would be deleted entirely -- see
        # UninstallGroupAndEmptyArrayTests for that case).
        shutil.copy(FIXTURES / "codex_hooks.json", self.hooks_path)
        cli.cmd_install_hooks(cli.build_parser().parse_args(["install-hooks", "--claude", "--codex"]))
        self.settings_path.write_text("{not valid json")

        args = cli.build_parser().parse_args(["uninstall-hooks", "--claude", "--codex"])
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = cli.cmd_uninstall_hooks(args)

        self.assertEqual(rc, 1)
        self.assertIn(str(self.settings_path), buf.getvalue())
        self.assertEqual(self.settings_path.read_text(), "{not valid json")

        hooks = json.loads(self.hooks_path.read_text())
        all_commands = [
            h["command"] for groups in hooks.get("hooks", {}).values() for g in groups for h in g["hooks"]
        ]
        self.assertFalse(any("agentboard hook" in c for c in all_commands))
        self.assertTrue(any("Codex is done" in c for c in all_commands), "the pre-existing hook must survive")


class UninstallGroupAndEmptyArrayTests(AgentBoardTestCase):
    """uninstall-hooks must touch only AgentBoard's own hook entries: never
    drop a user's hook sharing the same matcher group, never leave behind
    empty "Event": [] placeholders, and never leave behind a settings.json
    install-hooks itself created from nothing."""

    def setUp(self):
        super().setUp()
        self.claude_dir = self.fake_home / ".claude"
        self.settings_path = self.claude_dir / "settings.json"

    def test_uninstall_preserves_a_user_hook_in_the_same_matcher_group(self):
        # Exact review repro: a user hook (e.g. added later via Claude's
        # /hooks UI) shares SessionStart's one matcher group with
        # agentboard's own entry.
        cli.cmd_install_hooks(cli.build_parser().parse_args(["install-hooks", "--claude"]))
        data = json.loads(self.settings_path.read_text())
        data["hooks"]["SessionStart"][0]["hooks"].append(
            {"type": "command", "command": "~/bin/my-session-start.sh"}
        )
        self.settings_path.write_text(json.dumps(data, indent=2))

        cli.cmd_uninstall_hooks(cli.build_parser().parse_args(["uninstall-hooks", "--claude"]))

        after = json.loads(self.settings_path.read_text())
        session_start_commands = [
            h["command"] for g in after["hooks"].get("SessionStart", []) for h in g["hooks"]
        ]
        self.assertIn("~/bin/my-session-start.sh", session_start_commands)
        self.assertFalse(any("agentboard hook" in c for c in session_start_commands))

    def test_uninstall_leaves_no_empty_event_arrays_and_restores_original(self):
        original = {
            "hooks": {
                "PreToolUse": [{"hooks": [{"type": "command", "command": "/bin/true"}]}],
                "Stop": [{"hooks": [{"type": "command", "command": "/bin/true"}]}],
            }
        }
        self.settings_path.write_text(json.dumps(original, indent=2) + "\n")

        cli.cmd_install_hooks(cli.build_parser().parse_args(["install-hooks", "--claude"]))
        cli.cmd_uninstall_hooks(cli.build_parser().parse_args(["uninstall-hooks", "--claude"]))

        after = json.loads(self.settings_path.read_text())
        self.assertEqual(after, original, "uninstall must restore the exact pre-install state")
        for event, groups in after["hooks"].items():
            self.assertTrue(groups, f"{event} must not be left as an empty array")

    def test_uninstall_deletes_a_file_install_created_from_scratch(self):
        self.assertFalse(self.settings_path.exists())
        cli.cmd_install_hooks(cli.build_parser().parse_args(["install-hooks", "--claude"]))
        self.assertTrue(self.settings_path.exists())

        cli.cmd_uninstall_hooks(cli.build_parser().parse_args(["uninstall-hooks", "--claude"]))

        self.assertFalse(
            self.settings_path.exists(),
            "a settings.json install created from nothing must be removed, not left as {}",
        )
        self.assertFalse(
            cli._file_was_created_by_install(self.settings_path),
            "the manifest must forget the file once it's removed",
        )

    def test_uninstall_keeps_a_preexisting_file_even_if_now_empty(self):
        # The file existed before install -- never delete something that
        # predates AgentBoard, even if stripping leaves it with no content.
        self.settings_path.write_text("{}\n")
        cli.cmd_install_hooks(cli.build_parser().parse_args(["install-hooks", "--claude"]))

        cli.cmd_uninstall_hooks(cli.build_parser().parse_args(["uninstall-hooks", "--claude"]))

        self.assertTrue(self.settings_path.is_file())
        self.assertEqual(json.loads(self.settings_path.read_text()), {})

    def test_uninstall_restores_a_preexisting_empty_hooks_object_exactly(self):
        # Round-6 nit: a pre-existing {"hooks": {}} is a different file
        # from a pre-existing {} -- stripping agentboard's own (now empty)
        # hooks back out must restore the former's "hooks" key, not pop
        # it and collapse the two shapes together.
        original_bytes = b'{"hooks": {}}'
        self.settings_path.write_bytes(original_bytes)
        cli.cmd_install_hooks(cli.build_parser().parse_args(["install-hooks", "--claude"]))

        cli.cmd_uninstall_hooks(cli.build_parser().parse_args(["uninstall-hooks", "--claude"]))

        self.assertEqual(
            self.settings_path.read_bytes(), original_bytes,
            "a pre-existing {\"hooks\": {}} must come back byte for byte, not as {}",
        )

    # -- E: byte-for-byte restore of a pre-existing file -----------------------

    def test_uninstall_restores_original_bytes_exactly(self):
        # One-line hand-formatted hooks, non-ASCII, no trailing newline --
        # none of this must survive a json.dumps re-serialization.
        original_bytes = (
            b'{"hooks": {"Stop": [{"hooks": [{"type": "command", '
            b'"command": "~/bin/x.sh \xc3\xa9"}]}]}}'
        )
        self.settings_path.write_bytes(original_bytes)

        cli.cmd_install_hooks(cli.build_parser().parse_args(["install-hooks", "--claude"]))
        cli.cmd_uninstall_hooks(cli.build_parser().parse_args(["uninstall-hooks", "--claude"]))

        self.assertEqual(
            self.settings_path.read_bytes(), original_bytes,
            "uninstall must restore the exact original bytes, not a re-serialized copy",
        )

    def test_uninstall_restore_adds_only_one_extra_backup(self):
        self.settings_path.write_text('{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/bin/true"}]}]}}')
        cli.cmd_install_hooks(cli.build_parser().parse_args(["install-hooks", "--claude"]))
        backups_after_install = list(self.claude_dir.glob("settings.json.agentboard-bak-*"))
        self.assertEqual(len(backups_after_install), 1)

        cli.cmd_uninstall_hooks(cli.build_parser().parse_args(["uninstall-hooks", "--claude"]))

        backups_after_uninstall = list(self.claude_dir.glob("settings.json.agentboard-bak-*"))
        self.assertEqual(len(backups_after_uninstall), 2)

    def test_backup_filenames_never_collide_within_the_same_second(self):
        # Regression: backup names used to be second-resolution
        # (%Y%m%d%H%M%S). Two backups of the same file in the same
        # second used to collide onto one filename, silently overwriting
        # the first backup's bytes -- a real data-loss bug if that first
        # backup was the one `original_backups` points at for a later
        # byte-for-byte restore.
        target = self.claude_dir / "settings.json"
        target.write_text("first")
        first_backup = cli._backup_file(target)

        target.write_text("second")
        second_backup = cli._backup_file(target)

        self.assertNotEqual(first_backup, second_backup)
        self.assertTrue(first_backup.is_file())
        self.assertTrue(second_backup.is_file())
        self.assertEqual(first_backup.read_text(), "first")
        self.assertEqual(second_backup.read_text(), "second")

    def test_unique_backup_path_appends_a_counter_on_a_real_collision(self):
        # Force the timestamp-based name to already exist, simulating two
        # backups landing in the same microsecond (or two processes
        # racing) -- _unique_backup_path must not reuse that filename.
        target = self.claude_dir / "settings.json"
        target.write_text("content")
        fixed_now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with mock.patch("agentboard.cli.datetime") as mock_datetime:
            mock_datetime.now.return_value = fixed_now
            first = cli._unique_backup_path(target)
            first.write_text("placeholder")
            second = cli._unique_backup_path(target)
        self.assertNotEqual(first, second)
        self.assertTrue(str(second).startswith(str(first)))

    def test_uninstall_dry_run_does_not_touch_bytes_or_create_backup(self):
        original_bytes = b'{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/bin/true"}]}]}}'
        self.settings_path.write_bytes(original_bytes)
        cli.cmd_install_hooks(cli.build_parser().parse_args(["install-hooks", "--claude"]))
        backups_before = list(self.claude_dir.glob("settings.json.agentboard-bak-*"))

        cli.cmd_uninstall_hooks(
            cli.build_parser().parse_args(["uninstall-hooks", "--claude", "--dry-run"])
        )

        self.assertNotEqual(self.settings_path.read_bytes(), original_bytes, "still installed")
        self.assertEqual(list(self.claude_dir.glob("settings.json.agentboard-bak-*")), backups_before)

    def test_uninstall_falls_back_to_reserializing_when_result_differs_from_original(self):
        # The user hand-edited the file (independent of agentboard) after
        # install -- the stripped result no longer matches the first
        # backup, so byte-for-byte restore must not kick in; the existing
        # re-serialize-and-write behavior still applies.
        self.settings_path.write_text('{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/bin/true"}]}]}}')
        cli.cmd_install_hooks(cli.build_parser().parse_args(["install-hooks", "--claude"]))
        data = json.loads(self.settings_path.read_text())
        data["model"] = "opus"
        self.settings_path.write_text(json.dumps(data, indent=2))

        cli.cmd_uninstall_hooks(cli.build_parser().parse_args(["uninstall-hooks", "--claude"]))

        after = json.loads(self.settings_path.read_text())
        self.assertEqual(after["model"], "opus")
        stop_commands = [h["command"] for g in after["hooks"]["Stop"] for h in g["hooks"]]
        self.assertEqual(stop_commands, ["/bin/true"])

    # -- I: ~/.claude or ~/.codex dir created by install-hooks -----------------

    def test_uninstall_removes_a_dir_install_created_once_empty(self):
        # AgentBoardTestCase.setUp pre-creates an empty ~/.claude for every
        # test; remove it first to simulate the real "doesn't exist yet"
        # case install-hooks itself has to create the dir for.
        self.claude_dir.rmdir()
        self.assertFalse(self.claude_dir.exists())
        cli.cmd_install_hooks(cli.build_parser().parse_args(["install-hooks", "--claude"]))
        self.assertTrue(self.claude_dir.is_dir())
        self.assertTrue(cli._dir_was_created_by_install(self.claude_dir))

        cli.cmd_uninstall_hooks(cli.build_parser().parse_args(["uninstall-hooks", "--claude"]))

        self.assertFalse(self.claude_dir.exists(), "an empty dir install-hooks created must be removed too")
        self.assertFalse(cli._dir_was_created_by_install(self.claude_dir))

    def test_uninstall_keeps_a_created_dir_with_other_content_in_it(self):
        self.claude_dir.rmdir()
        cli.cmd_install_hooks(cli.build_parser().parse_args(["install-hooks", "--claude"]))
        (self.claude_dir / "projects.json").write_text("{}")

        cli.cmd_uninstall_hooks(cli.build_parser().parse_args(["uninstall-hooks", "--claude"]))

        self.assertTrue(self.claude_dir.is_dir(), "a dir with other content must not be removed")
        self.assertTrue((self.claude_dir / "projects.json").exists())

    def test_uninstall_never_removes_a_preexisting_dir(self):
        # setUp already created self.claude_dir -- install-hooks did not.
        cli.cmd_install_hooks(cli.build_parser().parse_args(["install-hooks", "--claude"]))
        self.assertFalse(cli._dir_was_created_by_install(self.claude_dir))

        cli.cmd_uninstall_hooks(cli.build_parser().parse_args(["uninstall-hooks", "--claude"]))

        self.assertTrue(self.claude_dir.is_dir(), "a dir that predates install must survive")


class CodexFeatureFlagTests(AgentBoardTestCase):
    """Codex silently ignores hooks.json without `[features]` / `hooks =
    true` in config.toml. install-hooks must warn (never auto-edit
    config.toml by default); enable-codex-hooks is the explicit opt-in that
    patches it with a minimal, non-round-tripping textual edit."""

    def setUp(self):
        super().setUp()
        self.codex_dir = self.fake_home / ".codex"
        self.config_path = self.codex_dir / "config.toml"
        self.hooks_path = self.codex_dir / "hooks.json"
        # A Codex old enough to still need [features] hooks = true, unless a
        # test sets another version on self.codex_version.
        version_patch = mock.patch.object(cli, "_codex_version", return_value=(0, 123, 0))
        self.codex_version = version_patch.start()
        self.addCleanup(version_patch.stop)

    def _install_codex_output(self):
        self.hooks_path.write_text(json.dumps({}))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_install_hooks(cli.build_parser().parse_args(["install-hooks", "--codex"]))
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def _enable_output(self, *extra):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_enable_codex_hooks(
                cli.build_parser().parse_args(["enable-codex-hooks", *extra]))
        self.assertEqual(rc, 0)
        return buf.getvalue()

    # -- codex --version and the 0.124 default ---------------------------------

    def test_codex_version_parses_the_cli_output(self):
        def run(stdout="", returncode=0, exc=None):
            def fake(argv, **kwargs):
                self.assertEqual(argv, ["codex", "--version"])
                self.assertEqual(kwargs.get("timeout"), cli.CODEX_VERSION_TIMEOUT_SECS)
                if exc:
                    raise exc
                return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")
            return fake

        self.assertEqual(_REAL_CODEX_VERSION(run("codex-cli 0.124.0\n")), (0, 124, 0))
        self.assertEqual(_REAL_CODEX_VERSION(run("codex-cli 1.2\n")), (1, 2, 0))
        self.assertIsNone(_REAL_CODEX_VERSION(run("codex-cli\n")))
        self.assertIsNone(_REAL_CODEX_VERSION(run("codex-cli 0.130.0\n", returncode=1)))
        self.assertIsNone(_REAL_CODEX_VERSION(run(exc=FileNotFoundError("codex"))))
        self.assertIsNone(_REAL_CODEX_VERSION(run(exc=subprocess.TimeoutExpired("codex", 2))))

    def test_no_warning_on_codex_0_124(self):
        self.codex_version.return_value = (0, 124, 0)
        self.config_path.write_text('model = "gpt-5"\n')
        out = self._install_codex_output()
        self.assertIn("Run /hooks in Codex to trust the new hooks.", out)
        self.assertNotIn("warning", out)
        self.assertNotIn("[features]", out)

    def test_warning_on_older_codex(self):
        self.codex_version.return_value = (0, 123, 9)
        self.config_path.write_text('model = "gpt-5"\n')
        out = self._install_codex_output()
        self.assertIn("warning: Codex 0.123.9 will ignore", out)
        self.assertIn("hooks = true is set under [features]", out)
        self.assertIn("install.sh --enable-codex-hooks", out)

    def test_soft_note_when_version_unknown(self):
        self.codex_version.return_value = None
        self.config_path.write_text('model = "gpt-5"\n')
        out = self._install_codex_output()
        self.assertIn("Codex 0.124 and newer enable hooks by default; an older Codex needs "
                      "hooks = true under [features]", out)
        self.assertNotIn("warning", out)

    def test_explicit_false_warns_on_any_version(self):
        self.config_path.write_text("[features]\nhooks = false\n")
        for version in ((0, 200, 0), (0, 100, 0), None):
            self.codex_version.return_value = version
            out = self._install_codex_output()
            self.assertIn(f"warning: hooks = false under [features] in {self.config_path} "
                          "disables hooks; remove it or run install.sh --enable-codex-hooks", out)

    def test_flag_true_never_warns(self):
        self.config_path.write_text("[features]\nhooks = true\n")
        for version in ((0, 200, 0), (0, 100, 0), None):
            self.codex_version.return_value = version
            out = self._install_codex_output()
            self.assertNotIn("warning", out)
            self.assertNotIn("0.124", out)

    def test_enable_is_noop_on_new_codex(self):
        self.codex_version.return_value = (0, 130, 1)
        self.config_path.write_text('model = "gpt-5"\n')
        out = self._enable_output()
        self.assertIn(f"Codex 0.130.1 enables hooks by default; {self.config_path} left unchanged",
                      out)
        self.assertEqual(self.config_path.read_text(), 'model = "gpt-5"\n')
        self.assertEqual(list(self.codex_dir.glob("*.agentboard-bak-*")), [])
        self.assertFalse((self.agentboard_home / "install-manifest.json").exists())
        out = self._enable_output("--dry-run")
        self.assertIn("left unchanged", out)

    def test_enable_is_noop_on_new_codex_without_a_config_file(self):
        self.codex_version.return_value = (0, 124, 0)
        self._enable_output()
        self.assertFalse(self.config_path.exists())

    def test_enable_still_patches_explicit_false(self):
        self.codex_version.return_value = (0, 130, 0)
        self.config_path.write_text("[features]\nhooks = false\n")
        self._enable_output()
        self.assertEqual(tomllib.loads(self.config_path.read_text()), {"features": {"hooks": True}})
        self.assertEqual(len(list(self.codex_dir.glob("*.agentboard-bak-*"))), 1)

    def test_enable_patches_when_version_unknown(self):
        self.codex_version.return_value = None
        self.config_path.write_text('model = "gpt-5"\n')
        self._enable_output()
        self.assertIs(tomllib.loads(self.config_path.read_text())["features"]["hooks"], True)

    def test_codex_hooks_alias_is_read(self):
        self.config_path.write_text("[features]\ncodex_hooks = true\n")
        self.assertIs(cli._codex_hooks_feature_enabled(self.config_path), True)
        self.config_path.write_text("[features]\ncodex_hooks = false\n")
        self.assertIs(cli._codex_hooks_feature_enabled(self.config_path), False)
        # `hooks` wins when both are set.
        self.config_path.write_text("[features]\ncodex_hooks = false\nhooks = true\n")
        self.assertIs(cli._codex_hooks_feature_enabled(self.config_path), True)
        self.config_path.write_text("[features]\ncodex_hooks = true\nhooks = false\n")
        self.assertIs(cli._codex_hooks_feature_enabled(self.config_path), False)

    def test_alias_false_warns_by_its_own_name_and_enable_refuses(self):
        text = "[features]\ncodex_hooks = false\n"
        self.config_path.write_text(text)
        out = self._install_codex_output()
        self.assertIn("warning: codex_hooks = false under [features]", out)
        self.assertIn("remove that line", out)
        for argv in (["enable-codex-hooks"], ["enable-codex-hooks", "--dry-run"]):
            rc, out, err = _run_cmd(argv)
            self.assertEqual(rc, 1)
            self.assertIn("codex_hooks = false under [features]", err)
            self.assertIn("remove that line by hand", err)
        self.assertEqual(self.config_path.read_text(), text)
        self.assertEqual(list(self.config_path.parent.glob("config.toml.*")), [])

    def test_alias_false_beside_hooks_true_still_warns_and_refuses(self):
        text = "[features]\nhooks = true\ncodex_hooks = false\n"
        self.config_path.write_text(text)
        out = self._install_codex_output()
        self.assertIn("warning: codex_hooks = false under [features]", out)
        rc, _, err = _run_cmd(["enable-codex-hooks"])
        self.assertEqual(rc, 1)
        self.assertIn("remove that line by hand", err)
        self.assertEqual(self.config_path.read_text(), text)

    def test_enable_leaves_an_alias_true_alone(self):
        self.config_path.write_text("[features]\ncodex_hooks = true\n")
        out = self._enable_output()
        self.assertIn("codex_hooks = true already set under [features]; left unchanged", out)
        self.assertEqual(self.config_path.read_text(), "[features]\ncodex_hooks = true\n")

    # -- _codex_hooks_feature_enabled ----------------------------------------

    def test_feature_flag_missing_file_is_unknown(self):
        self.assertIsNone(cli._codex_hooks_feature_enabled(self.config_path))

    def test_feature_flag_true(self):
        self.config_path.write_text("[features]\nhooks = true\n")
        self.assertIs(cli._codex_hooks_feature_enabled(self.config_path), True)

    def test_feature_flag_false(self):
        self.config_path.write_text("[features]\nhooks = false\n")
        self.assertIs(cli._codex_hooks_feature_enabled(self.config_path), False)

    def test_feature_flag_section_present_without_the_key_is_unset(self):
        self.config_path.write_text("[features]\njs_repl = true\n")
        self.assertIsNone(cli._codex_hooks_feature_enabled(self.config_path))

    def test_feature_flag_no_features_section_at_all_is_unset(self):
        self.config_path.write_text('model = "gpt-5"\n')
        self.assertIsNone(cli._codex_hooks_feature_enabled(self.config_path))

    def test_feature_flag_features_defined_as_a_non_table_does_not_crash(self):
        self.config_path.write_text('features = "on"\n')
        self.assertIsNone(cli._codex_hooks_feature_enabled(self.config_path))

    # -- _patch_codex_hooks_feature: the exact matrix the review asked for --

    def test_patch_no_features_section(self):
        before = 'model = "gpt-5"\n'
        after, changed = cli._patch_codex_hooks_feature(before)
        self.assertTrue(changed)
        self.assertIn(before.rstrip("\n"), after)
        self.assertIn("[features]", after)
        self.assertIn("hooks = true", after)

    def test_patch_section_without_the_key(self):
        before = "[features]\njs_repl = false\n"
        after, changed = cli._patch_codex_hooks_feature(before)
        self.assertTrue(changed)
        self.assertIn("js_repl = false", after)
        self.assertIn("hooks = true", after)
        self.assertEqual(after.count("[features]"), 1)

    def test_patch_hooks_false_flips_to_true(self):
        before = "[features]\nhooks = false\n"
        after, changed = cli._patch_codex_hooks_feature(before)
        self.assertTrue(changed)
        self.assertNotIn("hooks = false", after)
        self.assertIn("hooks = true", after)

    def test_patch_preserves_comments_and_unrelated_sections(self):
        before = (
            "# user config\n"
            'model = "gpt-5"  # pinned\n'
            "\n"
            "[features]\n"
            "# hooks = true  (left disabled on purpose)\n"
            "js_repl = true\n"
            "\n"
            "[other]\n"
            "x = 1\n"
        )
        after, changed = cli._patch_codex_hooks_feature(before)
        self.assertTrue(changed)
        self.assertIn("# user config", after)
        self.assertIn("# pinned", after)
        self.assertIn(
            "# hooks = true  (left disabled on purpose)", after,
            "a commented-out line must be left alone, not mistaken for the real key",
        )
        self.assertIn("[other]", after)
        self.assertIn("x = 1", after)
        self.assertIn("hooks = true", after)

    def test_patch_already_true_is_a_noop(self):
        before = "[features]\nhooks = true\n"
        after, changed = cli._patch_codex_hooks_feature(before)
        self.assertFalse(changed)
        self.assertEqual(after, before)

    # -- forms other than a bare [features] section ---------------------------

    def test_patch_dotted_key_sets_existing_hooks_key(self):
        before = 'model = "gpt-5"\nfeatures.hooks = false\n'
        after, changed = cli._patch_codex_hooks_feature(before)
        self.assertTrue(changed)
        self.assertIn("features.hooks = true", after)
        self.assertEqual(after.count("[features]"), 0, "must not add a [features] header too")
        self.assertEqual(
            tomllib.loads(after), {"model": "gpt-5", "features": {"hooks": True}}
        )

    def test_patch_dotted_key_adds_hooks_next_to_other_dotted_keys(self):
        before = 'features.web_search = true\nmodel = "gpt-5"\n'
        after, changed = cli._patch_codex_hooks_feature(before)
        self.assertTrue(changed)
        self.assertIn("features.hooks = true", after)
        self.assertEqual(after.count("[features]"), 0)
        self.assertEqual(
            tomllib.loads(after),
            {"model": "gpt-5", "features": {"web_search": True, "hooks": True}},
        )

    def test_patch_dotted_key_inside_a_section_is_not_mistaken_for_top_level(self):
        # features.hooks under [other] is other.features.hooks, not the
        # root table's features.hooks -- a real [features] table is still
        # the only safe place to add it here.
        before = "[other]\nfeatures.hooks = false\n"
        after, changed = cli._patch_codex_hooks_feature(before)
        self.assertTrue(changed)
        data = tomllib.loads(after)
        self.assertIs(data["features"]["hooks"], True)
        self.assertIs(data["other"]["features"]["hooks"], False, "unrelated nested key must survive")

    def test_patch_inline_table_adds_hooks_key(self):
        before = "features = { web_search = true }\n"
        after, changed = cli._patch_codex_hooks_feature(before)
        self.assertTrue(changed)
        self.assertEqual(after.count("[features]"), 0)
        self.assertEqual(
            tomllib.loads(after), {"features": {"web_search": True, "hooks": True}}
        )

    def test_patch_inline_table_empty(self):
        before = "features = {}\n"
        after, changed = cli._patch_codex_hooks_feature(before)
        self.assertTrue(changed)
        self.assertEqual(tomllib.loads(after), {"features": {"hooks": True}})

    def test_patch_inline_table_flips_existing_hooks_key(self):
        before = "features = { hooks = false, web_search = true }\n"
        after, changed = cli._patch_codex_hooks_feature(before)
        self.assertTrue(changed)
        self.assertEqual(
            tomllib.loads(after), {"features": {"hooks": True, "web_search": True}}
        )

    def test_patch_inline_table_with_unsafe_value_refuses(self):
        before = 'features = { note = "a, b" }\n'
        with self.assertRaises(cli.CodexTomlPatchError):
            cli._patch_codex_hooks_feature(before)

    def test_patch_array_of_tables_is_not_treated_as_a_section_boundary_miss(self):
        # Exact review repro: a [[array-of-tables]] header must still end
        # the [features] section, and its own `hooks` key must survive.
        before = (
            "[features]\nweb_search = true\n\n"
            '[[skills.config]]\nname = "x"\nhooks = false\n'
        )
        after, changed = cli._patch_codex_hooks_feature(before)
        self.assertTrue(changed)
        self.assertEqual(after.count("[features]"), 1)
        data = tomllib.loads(after)
        self.assertIs(data["features"]["hooks"], True)
        self.assertIs(data["features"]["web_search"], True)
        self.assertIs(data["skills"]["config"][0]["hooks"], False)
        self.assertEqual(data["skills"]["config"][0]["name"], "x")

    def test_patch_quoted_section_header(self):
        before = '["features"]\n\'hooks\' = false\n'
        after, changed = cli._patch_codex_hooks_feature(before)
        self.assertTrue(changed)
        self.assertEqual(after.count("[features]"), 0, "must preserve the original quoting style")
        self.assertEqual(tomllib.loads(after), {"features": {"hooks": True}})

    def test_patch_crlf_preserves_line_endings(self):
        before = "[features]\r\nweb_search = true\r\n"
        after, changed = cli._patch_codex_hooks_feature(before)
        self.assertTrue(changed)
        self.assertNotIn("\r\n\r\n", after)
        for line in after.splitlines(keepends=True)[:-1]:
            self.assertTrue(line.endswith("\r\n"), repr(line))
        self.assertEqual(tomllib.loads(after.replace("\r\n", "\n")), {"features": {"web_search": True, "hooks": True}})

    def test_patch_no_trailing_newline_in_place_edit_keeps_it_that_way(self):
        before = "[features]\nhooks = false"
        after, changed = cli._patch_codex_hooks_feature(before)
        self.assertTrue(changed)
        self.assertEqual(after, "[features]\nhooks = true")

    def test_patch_new_file_has_no_blank_first_line(self):
        after, changed = cli._patch_codex_hooks_feature("")
        self.assertTrue(changed)
        self.assertEqual(after, "[features]\nhooks = true\n")
        self.assertFalse(after.startswith("\n"))

    def test_patch_malformed_toml_refuses(self):
        before = 'model = "gpt-5"\nfeatures = {\n'
        with self.assertRaises(cli.CodexTomlPatchError):
            cli._patch_codex_hooks_feature(before)

    def test_patch_features_as_array_of_tables_refuses(self):
        before = '[[features]]\nhooks = false\n'
        with self.assertRaises(cli.CodexTomlPatchError):
            cli._patch_codex_hooks_feature(before)

    def test_cmd_enable_codex_hooks_reports_and_leaves_file_untouched_on_refusal(self):
        # features is already an inline table with a value the patcher
        # can't safely rewrite -- the manual hint must fall back to the
        # plain, snippet-free sentence (see the hint tests below for the
        # full matrix), not point at a form it couldn't actually verify.
        self.config_path.write_text('features = { note = "a, b" }\n')
        before = self.config_path.read_text()
        args = cli.build_parser().parse_args(["enable-codex-hooks"])
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = cli.cmd_enable_codex_hooks(args)
        self.assertEqual(rc, 1)
        self.assertIn("error:", buf.getvalue())
        self.assertIn("hooks = true", buf.getvalue())
        self.assertIn("Couldn't safely determine", buf.getvalue())
        self.assertEqual(self.config_path.read_text(), before)
        self.assertEqual(list(self.codex_dir.glob("*.agentboard-bak-*")), [])

    # -- form-aware manual hint (review round 4, finding 4) --------------------
    #
    # _codex_features_manual_hint_lines must never independently re-derive
    # advice from a regex scan of the raw text (a decoy "[features]" or
    # "features.x = ..." inside an unrelated string value could fool
    # that). It delegates entirely to the real patcher and shows exactly
    # the lines its own tomllib round-trip verification already proved
    # safe -- so a concrete snippet is only possible for forms the
    # patcher can positively verify. Everything else gets one plain
    # sentence with no snippet.

    def _hint_and_patch(self, before: str):
        hint = cli._codex_features_manual_hint_lines(before)
        new_text, changed = cli._patch_codex_hooks_feature(before)
        return hint, new_text, changed

    def test_hint_for_undefined_features_suggests_new_section(self):
        before = 'model = "gpt-5"\n'
        hint, new_text, changed = self._hint_and_patch(before)
        self.assertTrue(changed)
        self.assertIn("[features]", "\n".join(hint))
        self.assertIn("hooks = true", "\n".join(hint))
        self.assertEqual(tomllib.loads(new_text)["features"]["hooks"], True)

    def test_hint_for_existing_section_with_hooks_absent(self):
        before = "[features]\nweb_search = true\n"
        hint, new_text, changed = self._hint_and_patch(before)
        self.assertTrue(changed)
        self.assertEqual(hint, ["add this (or change the existing line to match):", "    hooks = true"])
        data = tomllib.loads(new_text)
        self.assertIs(data["features"]["hooks"], True)
        self.assertIs(data["features"]["web_search"], True)

    def test_hint_for_existing_section_with_hooks_false(self):
        before = "[features]\nhooks = false\n"
        hint, new_text, changed = self._hint_and_patch(before)
        self.assertTrue(changed)
        self.assertEqual(hint, ["add this (or change the existing line to match):", "    hooks = true"])
        self.assertIs(tomllib.loads(new_text)["features"]["hooks"], True)

    def test_hint_for_dotted_key_suggests_dotted_assignment(self):
        before = 'features.web_search = true\nmodel = "gpt-5"\n'
        hint, new_text, changed = self._hint_and_patch(before)
        self.assertTrue(changed)
        self.assertEqual(hint, ["add this (or change the existing line to match):", "    features.hooks = true"])
        data = tomllib.loads(new_text)
        self.assertIs(data["features"]["hooks"], True)
        self.assertIs(data["features"]["web_search"], True)

    def test_hint_for_inline_table_adding_hooks_key(self):
        before = "features = { web_search = true }\n"
        hint, new_text, changed = self._hint_and_patch(before)
        self.assertTrue(changed)
        self.assertEqual(len(hint), 2)
        self.assertIn("hooks = true", hint[1])
        data = tomllib.loads(new_text)
        self.assertIs(data["features"]["hooks"], True)
        self.assertIs(data["features"]["web_search"], True)

    def test_hint_matches_the_actual_diff_the_patcher_would_write(self):
        # The requested validation: apply the suggested snippet and parse
        # the result -- here "apply" is exact, because the hint's added
        # lines are, by construction, drawn directly from the patch's own
        # diff, not a separately hand-written template.
        import difflib

        before = "[features]\nweb_search = true\n"
        hint, new_text, changed = self._hint_and_patch(before)
        self.assertTrue(changed)
        diff_added = [
            line[1:]
            for line in difflib.unified_diff(before.splitlines(), new_text.splitlines(), n=0, lineterm="")
            if line.startswith("+") and not line.startswith("+++")
        ]
        self.assertEqual([line.strip() for line in hint[1:]], diff_added)
        self.assertIs(tomllib.loads(new_text)["features"]["hooks"], True)

    # -- the 4 shapes round 4 found producing a bad or misleading hint --------

    def test_hint_for_existing_hooks_as_a_table_is_the_generic_sentence(self):
        # Round-4 repro: hooks already a sub-table via a dotted key inside
        # [features]; blindly adding "hooks = true" would duplicate-declare it.
        before = "[features]\nhooks.x = 1\n"
        with self.assertRaises(cli.CodexTomlPatchError):
            cli._patch_codex_hooks_feature(before)
        hint = cli._codex_features_manual_hint_lines(before)
        self.assertEqual(len(hint), 1)
        self.assertIn("Couldn't safely determine", hint[0])

    def test_hint_for_features_as_a_non_table_value_is_the_generic_sentence(self):
        # Round-4 repro: features already bound to a plain string.
        before = 'features = "on"\n'
        with self.assertRaises(cli.CodexTomlPatchError):
            cli._patch_codex_hooks_feature(before)
        hint = cli._codex_features_manual_hint_lines(before)
        self.assertEqual(len(hint), 1)
        self.assertIn("Couldn't safely determine", hint[0])

    def test_hint_for_decoy_features_text_inside_a_string_is_the_generic_sentence(self):
        # Round-4 repro: "[features]" only ever appears inside an
        # unrelated multi-line string value -- there is no real features
        # table anywhere, so there is nothing safe to point at.
        before = 'notes = """\n[features]\nhooks = false\n"""\n'
        data = tomllib.loads(before)
        self.assertNotIn("features", data)  # confirms the fixture is a pure decoy
        hint = cli._codex_features_manual_hint_lines(before)
        self.assertEqual(len(hint), 1)
        self.assertIn("Couldn't safely determine", hint[0])

    def test_hint_for_nested_only_features_table_is_the_generic_sentence(self):
        before = "[features.sub]\nx = 1\n"
        with self.assertRaises(cli.CodexTomlPatchError):
            cli._patch_codex_hooks_feature(before)
        hint = cli._codex_features_manual_hint_lines(before)
        self.assertEqual(len(hint), 1)
        self.assertIn("Couldn't safely determine", hint[0])

    def test_hint_for_inline_table_with_unsafe_value_is_the_generic_sentence(self):
        before = 'features = { note = "a, b" }\n'
        with self.assertRaises(cli.CodexTomlPatchError):
            cli._patch_codex_hooks_feature(before)
        hint = cli._codex_features_manual_hint_lines(before)
        self.assertEqual(len(hint), 1)
        self.assertIn("Couldn't safely determine", hint[0])

    def test_hint_is_empty_when_already_true(self):
        before = "[features]\nhooks = true\n"
        self.assertEqual(cli._codex_features_manual_hint_lines(before), [])

    def test_install_hooks_reminder_is_form_aware_for_a_dotted_key(self):
        self.hooks_path.write_text(json.dumps({}))
        self.config_path.write_text('features.web_search = true\n')
        cli.cmd_install_hooks(cli.build_parser().parse_args(["install-hooks", "--codex"]))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.cmd_install_hooks(cli.build_parser().parse_args(["install-hooks", "--codex"]))
        out = buf.getvalue()
        self.assertIn("features.hooks = true", out)

    def test_install_hooks_reminder_handles_a_refused_shape_without_crashing(self):
        self.hooks_path.write_text(json.dumps({}))
        self.config_path.write_text('features = "on"\n')
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_install_hooks(cli.build_parser().parse_args(["install-hooks", "--codex"]))
        self.assertEqual(rc, 0)
        self.assertIn("Couldn't safely determine", buf.getvalue())

    # -- non-UTF-8 config.toml must not crash (review round 4) -----------------

    def test_enable_codex_hooks_on_non_utf8_file_prints_clear_message_not_a_crash(self):
        self.config_path.write_bytes(b"model = \xff\xfe not utf8\n")
        args = cli.build_parser().parse_args(["enable-codex-hooks"])
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = cli.cmd_enable_codex_hooks(args)
        self.assertEqual(rc, 1)
        self.assertIn("could not read", buf.getvalue())
        self.assertIn("UTF-8", buf.getvalue())
        self.assertEqual(self.config_path.read_bytes(), b"model = \xff\xfe not utf8\n")
        self.assertEqual(list(self.codex_dir.glob("*.agentboard-bak-*")), [])

    def test_install_hooks_on_non_utf8_config_toml_does_not_crash(self):
        self.config_path.write_bytes(b"model = \xff\xfe not utf8\n")
        args = cli.build_parser().parse_args(["install-hooks", "--codex"])
        buf = io.StringIO()
        rc = None
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_install_hooks(args)
        self.assertEqual(rc, 0)
        self.assertIn("could not read", buf.getvalue())
        self.assertIn("UTF-8", buf.getvalue())

    def test_codex_hooks_feature_enabled_on_non_utf8_file_returns_none_not_a_crash(self):
        self.config_path.write_bytes(b"model = \xff\xfe not utf8\n")
        self.assertIsNone(cli._codex_hooks_feature_enabled(self.config_path))

    # -- cmd_enable_codex_hooks -----------------------------------------------

    def test_enable_codex_hooks_backs_up_and_sets_flag(self):
        self.config_path.write_text('model = "gpt-5"\n')
        args = cli.build_parser().parse_args(["enable-codex-hooks"])
        rc = cli.cmd_enable_codex_hooks(args)
        self.assertEqual(rc, 0)
        text = self.config_path.read_text()
        self.assertIn("[features]", text)
        self.assertIn("hooks = true", text)
        backups = list(self.codex_dir.glob("config.toml.agentboard-bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), 'model = "gpt-5"\n')

    def test_enable_codex_hooks_dry_run_makes_no_changes(self):
        self.config_path.write_text('model = "gpt-5"\n')
        args = cli.build_parser().parse_args(["enable-codex-hooks", "--dry-run"])
        cli.cmd_enable_codex_hooks(args)
        self.assertEqual(self.config_path.read_text(), 'model = "gpt-5"\n')
        self.assertEqual(list(self.codex_dir.glob("*.agentboard-bak-*")), [])

    def test_enable_codex_hooks_already_set_is_a_noop(self):
        self.config_path.write_text("[features]\nhooks = true\n")
        args = cli.build_parser().parse_args(["enable-codex-hooks"])
        cli.cmd_enable_codex_hooks(args)
        self.assertEqual(list(self.codex_dir.glob("*.agentboard-bak-*")), [])

    # -- item 6: CRLF survives the real file write, not just the pure patch ---

    def test_enable_codex_hooks_real_write_preserves_crlf(self):
        # Path.read_text()/write_text() do universal-newline translation,
        # so a CRLF file silently became LF through the real command even
        # though the pure _patch_codex_hooks_feature function (which
        # operates on an in-memory string) handled CRLF correctly.
        raw = b'model = "o3"\r\n[features]\r\nweb_search = true\r\n'
        self.config_path.write_bytes(raw)
        args = cli.build_parser().parse_args(["enable-codex-hooks"])
        rc = cli.cmd_enable_codex_hooks(args)
        self.assertEqual(rc, 0)
        written = self.config_path.read_bytes()
        self.assertIn(b"\r\n", written)
        # Every line must end in \r\n, none bare \n.
        for line in written.split(b"\r\n")[:-1]:
            self.assertNotIn(b"\n", line)
        self.assertEqual(
            tomllib.loads(written.decode().replace("\r\n", "\n")),
            {"model": "o3", "features": {"web_search": True, "hooks": True}},
        )

    def test_enable_codex_hooks_dry_run_preview_matches_the_real_crlf_write(self):
        raw = b'[features]\r\nweb_search = true\r\n'
        self.config_path.write_bytes(raw)

        preview_args = cli.build_parser().parse_args(["enable-codex-hooks", "--dry-run"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.cmd_enable_codex_hooks(preview_args)
        diff = buf.getvalue()
        self.assertIn("hooks = true", diff)
        self.assertNotIn(
            "web_search", "\n".join(l for l in diff.splitlines() if l.startswith(("+", "-"))),
            "a CRLF file's unrelated lines must not show up as changed in the preview",
        )

        cli.cmd_enable_codex_hooks(cli.build_parser().parse_args(["enable-codex-hooks"]))
        self.assertEqual(self.config_path.read_bytes().count(b"\r\n"), raw.count(b"\r\n") + 1)

    def test_enable_codex_hooks_on_a_preexisting_zero_byte_file_is_patched_not_created(self):
        # old_text == "" is true for both "didn't exist" and "exists but
        # is 0 bytes" -- only is_file() tells them apart, and uninstall's
        # created-vs-patched branches behave very differently.
        self.config_path.write_bytes(b"")
        self.assertTrue(self.config_path.is_file())

        cli.cmd_enable_codex_hooks(cli.build_parser().parse_args(["enable-codex-hooks"]))

        action = cli._codex_config_action()
        self.assertEqual(action["action"], "patched")

        cli.cmd_uninstall_hooks(cli.build_parser().parse_args(["uninstall-hooks", "--codex"]))
        self.assertTrue(
            self.config_path.exists(),
            "a file that existed before (even empty) must never be auto-deleted",
        )

    # -- item 8: ~/.codex created by enable-codex-hooks itself -----------------

    def test_enable_codex_hooks_records_the_codex_dir_it_creates(self):
        self.codex_dir.rmdir()
        self.assertFalse(self.codex_dir.exists())

        cli.cmd_enable_codex_hooks(cli.build_parser().parse_args(["enable-codex-hooks"]))

        self.assertTrue(self.codex_dir.is_dir())
        self.assertTrue(cli._dir_was_created_by_install(self.codex_dir))

    def test_uninstall_removes_codex_dir_after_enable_codex_hooks_created_it_first(self):
        # Exact review repro: enable-codex-hooks runs before install-hooks
        # ever touches ~/.codex, so install_codex_hooks sees the dir
        # already there and would never have recorded it itself.
        self.codex_dir.rmdir()
        cli.cmd_enable_codex_hooks(cli.build_parser().parse_args(["enable-codex-hooks"]))
        cli.cmd_install_hooks(cli.build_parser().parse_args(["install-hooks", "--codex"]))

        cli.cmd_uninstall_hooks(cli.build_parser().parse_args(["uninstall-hooks", "--codex"]))

        self.assertFalse(
            self.codex_dir.exists(),
            "~/.codex must be removed once both hooks.json and config.toml are gone",
        )

    # -- the warning/reminder install-hooks prints ----------------------------

    def test_install_hooks_warns_when_feature_flag_missing(self):
        args = cli.build_parser().parse_args(["install-hooks", "--codex"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.cmd_install_hooks(args)
        out = buf.getvalue()
        self.assertIn("Run /hooks in Codex to trust the new hooks.", out)
        self.assertIn("hooks = true", out)
        self.assertIn(str(self.config_path), out)
        self.assertNotIn(
            "\\n", out,
            "the warning must render as actual text, not a literal backslash-n",
        )

    def test_install_hooks_no_warning_once_flag_is_set(self):
        self.config_path.write_text("[features]\nhooks = true\n")
        args = cli.build_parser().parse_args(["install-hooks", "--codex"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.cmd_install_hooks(args)
        out = buf.getvalue()
        self.assertIn("Run /hooks in Codex to trust the new hooks.", out)
        self.assertNotIn("warning:", out)

    def test_reminder_printed_again_on_a_no_op_rerun(self):
        # A user who missed the reminder the first time must still see it
        # on a later re-run, not just when hooks.json actually changes.
        cli.cmd_install_hooks(cli.build_parser().parse_args(["install-hooks", "--codex"]))
        args = cli.build_parser().parse_args(["install-hooks", "--codex"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.cmd_install_hooks(args)
        self.assertIn("Run /hooks in Codex to trust the new hooks.", buf.getvalue())


class UninstallCodexConfigTests(AgentBoardTestCase):
    """D: uninstall-hooks' handling of the config.toml edit
    --enable-codex-hooks made -- recorded in the manifest as either
    `created` (file didn't exist before) or `patched` (it did), and never
    auto-reverted."""

    def setUp(self):
        super().setUp()
        self.codex_dir = self.fake_home / ".codex"
        self.config_path = self.codex_dir / "config.toml"

    def test_uninstall_deletes_a_config_toml_it_created_unmodified(self):
        self.assertFalse(self.config_path.exists())
        cli.cmd_enable_codex_hooks(cli.build_parser().parse_args(["enable-codex-hooks"]))
        self.assertTrue(self.config_path.exists())

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.cmd_uninstall_hooks(cli.build_parser().parse_args(["uninstall-hooks", "--codex"]))

        self.assertFalse(self.config_path.exists(), "a config.toml created from scratch must be removed")
        self.assertIn("Removed", buf.getvalue())
        self.assertIsNone(cli._codex_config_action(), "manifest entry must be forgotten")

    def test_uninstall_keeps_a_created_config_toml_if_user_modified_it(self):
        cli.cmd_enable_codex_hooks(cli.build_parser().parse_args(["enable-codex-hooks"]))
        # The user added their own config after enable-codex-hooks created the file.
        data = self.config_path.read_text() + 'model = "gpt-5"\n'
        self.config_path.write_text(data)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.cmd_uninstall_hooks(cli.build_parser().parse_args(["uninstall-hooks", "--codex"]))

        self.assertTrue(self.config_path.exists(), "a user-modified file must never be auto-deleted")
        self.assertEqual(self.config_path.read_text(), data)
        self.assertIn("leaving", buf.getvalue())

    def test_uninstall_leaves_a_patched_config_toml_enabled_with_a_note(self):
        self.config_path.write_text('model = "gpt-5"\n')
        cli.cmd_enable_codex_hooks(cli.build_parser().parse_args(["enable-codex-hooks"]))
        before = self.config_path.read_text()
        self.assertIn("hooks = true", before)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.cmd_uninstall_hooks(cli.build_parser().parse_args(["uninstall-hooks", "--codex"]))

        self.assertEqual(
            self.config_path.read_text(), before,
            "a pre-existing file's hooks=true edit must never be auto-reverted",
        )
        self.assertIn("hooks = true", buf.getvalue())
        self.assertIn("left", buf.getvalue())

    def test_uninstall_dry_run_does_not_delete_a_created_config_toml(self):
        cli.cmd_enable_codex_hooks(cli.build_parser().parse_args(["enable-codex-hooks"]))
        cli.cmd_uninstall_hooks(cli.build_parser().parse_args(["uninstall-hooks", "--codex", "--dry-run"]))
        self.assertTrue(self.config_path.exists())
        self.assertIsNotNone(cli._codex_config_action())

    def test_uninstall_without_enable_codex_hooks_is_a_noop_for_config_toml(self):
        self.config_path.write_text('model = "gpt-5"\n')
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.cmd_uninstall_hooks(cli.build_parser().parse_args(["uninstall-hooks", "--codex"]))
        self.assertEqual(self.config_path.read_text(), 'model = "gpt-5"\n')


class PathsCommandTests(AgentBoardTestCase):
    """H: `agentboard paths --json` -- the app's contract for finding
    AgentBoard's own directories without duplicating this logic."""

    def test_paths_json_contract_shape(self):
        buf = io.StringIO()
        args = cli.build_parser().parse_args(["paths", "--json"])
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_paths(args)
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(set(data.keys()), {"home", "db", "core"})
        self.assertEqual(data["home"], str(self.agentboard_home))
        self.assertEqual(data["db"], str(self.agentboard_home / "agentboard.db"))
        self.assertTrue(data["core"])
        self.assertTrue(Path(data["core"]).is_dir())

    def test_paths_json_is_a_single_line_of_valid_json(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.cmd_paths(cli.build_parser().parse_args(["paths", "--json"]))
        lines = buf.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        json.loads(lines[0])

    def test_paths_without_json_prints_human_readable_lines(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.cmd_paths(cli.build_parser().parse_args(["paths"]))
        out = buf.getvalue()
        self.assertIn("home:", out)
        self.assertIn("db:", out)
        self.assertIn("core:", out)


def _completed(stdout="", returncode=0, stderr="", args=None):
    """A real CompletedProcess for run_osascript mocks, so cmd_focus's
    branching on returncode and its logging never see a bare Mock."""
    return subprocess.CompletedProcess(args or ["osascript"], returncode, stdout, stderr)


class FocusResumeTests(AgentBoardTestCase):
    def setUp(self):
        super().setUp()
        self._old_level = os.environ.get("AGENTBOARD_LOG_LEVEL")
        os.environ["AGENTBOARD_LOG_LEVEL"] = "debug"

    def tearDown(self):
        if self._old_level is None:
            os.environ.pop("AGENTBOARD_LOG_LEVEL", None)
        else:
            os.environ["AGENTBOARD_LOG_LEVEL"] = self._old_level
        super().tearDown()

    def _log_text(self):
        path = self.agentboard_home / "agentboard.log"
        return path.read_text() if path.exists() else ""

    def _focus_lines(self):
        return [line for line in self._log_text().splitlines() if " cli.focus " in line]

    def _assert_outcome(self, outcome):
        lines = self._focus_lines()
        self.assertEqual(len(lines), 1, self._log_text())
        self.assertIn(" INFO cli.focus ", lines[0])
        self.assertIn(f"outcome={outcome}", lines[0])
        return lines[0]

    def test_focus_iterm_found(self):
        insert_session(self.conn, session_id="s1", iterm_session_id="w0t1p0:ABCD", term_program="iTerm.app")
        fake = mock.Mock(return_value=_completed("found:uuid\n"))
        args = cli.build_parser().parse_args(["focus", "claude", "s1"])
        result = cli.cmd_focus(args, run_osascript=fake)
        self.assertEqual(result, 0)
        called_args = fake.call_args[0][0]
        self.assertIn("ABCD", called_args)
        line = self._assert_outcome("matched_uuid")
        self.assertIn("key=claude:s1", line)
        self.assertIn("term=iTerm.app", line)
        self.assertIn("iterm_id=w0t1p0:ABCD", line)
        self.assertIn("strategy=iterm_script", line)
        self.assertIn("osa_rc=0", line)
        self.assertIn("osa_out=found:uuid", line)

    def test_focus_iterm_not_found_falls_back_to_activating_app(self):
        insert_session(self.conn, session_id="s1", iterm_session_id="w0t1p0:ABCD", term_program="iTerm.app")
        fake = mock.Mock(side_effect=[_completed("not_found\n"), _completed("", args=["open"])])
        args = cli.build_parser().parse_args(["focus", "claude", "s1"])
        result = cli.cmd_focus(args, run_osascript=fake)
        self.assertEqual(result, 0)
        self.assertEqual(fake.call_count, 2)
        self.assertEqual(fake.call_args_list[-1][0][0], ["open", "-a", "iTerm"])
        line = self._assert_outcome("activated_only")
        self.assertIn("strategy=iterm_script,open_a", line)
        self.assertIn("osa_out=not_found", line)

    def test_focus_iterm_script_error_is_logged_and_falls_back(self):
        insert_session(self.conn, session_id="s1", iterm_session_id="w0t1p0:ABCD", term_program="iTerm.app")
        fake = mock.Mock(side_effect=[
            _completed("", returncode=1, stderr="execution error: Not authorized (-1743)"),
            _completed("", args=["open"]),
        ])
        args = cli.build_parser().parse_args(["focus", "claude", "s1"])
        self.assertEqual(cli.cmd_focus(args, run_osascript=fake), 0)
        line = self._assert_outcome("activated_only")
        self.assertIn("osa_rc=1", line)
        self.assertIn("Not authorized", line)

    def test_focus_iterm_falls_back_to_tty_when_unique_id_misses(self):
        insert_session(
            self.conn, session_id="s1", iterm_session_id="w0t1p0:ABCD",
            term_program="iTerm.app", tty="ttys003",
        )
        fake = mock.Mock(return_value=_completed("found:tty\n"))
        args = cli.build_parser().parse_args(["focus", "claude", "s1"])
        result = cli.cmd_focus(args, run_osascript=fake)
        self.assertEqual(result, 0)
        script_args = fake.call_args[0][0]
        self.assertIn("ABCD", script_args)
        self.assertIn("/dev/ttys003", script_args)
        line = self._assert_outcome("matched_tty")
        self.assertIn("tty=ttys003", line)

    def test_focus_iterm_legacy_bare_found_is_matched_unknown(self):
        insert_session(self.conn, session_id="s1", iterm_session_id="w0t1p0:ABCD", term_program="iTerm.app")
        fake = mock.Mock(return_value=_completed("found\n"))
        args = cli.build_parser().parse_args(["focus", "claude", "s1"])
        self.assertEqual(cli.cmd_focus(args, run_osascript=fake), 0)
        fake.assert_called_once()
        self._assert_outcome("matched_unknown")

    def test_focus_apple_terminal(self):
        insert_session(self.conn, session_id="s1", term_program="Apple_Terminal")
        fake = mock.Mock(return_value=_completed(""))
        args = cli.build_parser().parse_args(["focus", "claude", "s1"])
        result = cli.cmd_focus(args, run_osascript=fake)
        self.assertEqual(result, 0)
        fake.assert_called_once()
        # rc 0 with an answer the CLI does not recognise is not a failure.
        line = self._assert_outcome("matched_unknown")
        self.assertIn("osa_rc=0", line)

    def test_focus_apple_terminal_script_error_is_not_found(self):
        insert_session(self.conn, session_id="s1", term_program="Apple_Terminal", tty="ttys009")
        fake = mock.Mock(return_value=_completed("", returncode=1, stderr="execution error"))
        args = cli.build_parser().parse_args(["focus", "claude", "s1"])
        self.assertEqual(cli.cmd_focus(args, run_osascript=fake), 0)
        line = self._assert_outcome("not_found")
        self.assertIn("osa_rc=1", line)

    def test_focus_apple_terminal_found_is_matched_tty(self):
        insert_session(self.conn, session_id="s1", term_program="Apple_Terminal", tty="ttys009")
        fake = mock.Mock(return_value=_completed("found\n"))
        args = cli.build_parser().parse_args(["focus", "claude", "s1"])
        self.assertEqual(cli.cmd_focus(args, run_osascript=fake), 0)
        self.assertIn("/dev/ttys009", fake.call_args[0][0])
        line = self._assert_outcome("matched_tty")
        self.assertIn("strategy=terminal_script", line)

    def test_focus_apple_terminal_activated_is_activated_only(self):
        insert_session(self.conn, session_id="s1", term_program="Apple_Terminal", tty="ttys009")
        fake = mock.Mock(return_value=_completed("activated\n"))
        args = cli.build_parser().parse_args(["focus", "claude", "s1"])
        self.assertEqual(cli.cmd_focus(args, run_osascript=fake), 0)
        self._assert_outcome("activated_only")

    def test_focus_generic_terminal_activates_app(self):
        insert_session(self.conn, session_id="s1", term_program="WarpTerminal")
        fake = mock.Mock(return_value=_completed("", args=["open"]))
        args = cli.build_parser().parse_args(["focus", "claude", "s1"])
        result = cli.cmd_focus(args, run_osascript=fake)
        self.assertEqual(result, 0)
        fake.assert_called_once_with(
            ["open", "-a", "Warp"], capture_output=True, text=True,
            timeout=cli.OSASCRIPT_TIMEOUT_SECS,
        )
        line = self._assert_outcome("activated_only")
        self.assertIn("strategy=open_a", line)
        self.assertIn("open_rc=0", line)

    def test_focus_open_a_failure_is_activate_failed(self):
        insert_session(self.conn, session_id="s1", term_program="WarpTerminal")
        fake = mock.Mock(return_value=_completed("", returncode=1, args=["open"]))
        args = cli.build_parser().parse_args(["focus", "claude", "s1"])
        self.assertEqual(cli.cmd_focus(args, run_osascript=fake), 0)
        line = self._assert_outcome("activate_failed")
        self.assertIn("open_rc=1", line)

    def test_focus_iterm_fallback_failure_is_activate_failed(self):
        insert_session(self.conn, session_id="s1", iterm_session_id="w0t1p0:ABCD", term_program="iTerm.app")
        fake = mock.Mock(side_effect=[
            _completed("", returncode=1, stderr="boom"),
            _completed("", returncode=1, args=["open"]),
        ])
        args = cli.build_parser().parse_args(["focus", "claude", "s1"])
        self.assertEqual(cli.cmd_focus(args, run_osascript=fake), 0)
        self._assert_outcome("activate_failed")

    def test_focus_every_call_is_bounded_by_a_timeout(self):
        insert_session(self.conn, session_id="s1", iterm_session_id="w0t1p0:ABCD", term_program="iTerm.app")
        fake = mock.Mock(side_effect=[_completed("not_found\n"), _completed("", args=["open"])])
        args = cli.build_parser().parse_args(["focus", "claude", "s1"])
        cli.cmd_focus(args, run_osascript=fake)
        for call in fake.call_args_list:
            self.assertEqual(call.kwargs.get("timeout"), cli.OSASCRIPT_TIMEOUT_SECS)

    def test_focus_logs_begin_before_running_any_script(self):
        os.environ["AGENTBOARD_LOG_LEVEL"] = "info"
        insert_session(self.conn, session_id="s1", iterm_session_id="w0t1p0:ABCD",
                       term_program="iTerm.app", tty="ttys004")
        seen = {}

        def run(argv, **kwargs):
            seen["log"] = self._log_text()
            return _completed("found:uuid\n")

        args = cli.build_parser().parse_args(["focus", "claude", "s1"])
        self.assertEqual(cli.cmd_focus(args, run_osascript=run), 0)
        self.assertIn("INFO cli.focus.begin key=claude:s1 term=iTerm.app tty=ttys004", seen["log"])
        self.assertNotIn(" cli.focus key=", seen["log"])
        self._assert_outcome("matched_uuid")

    def test_focus_timeout_is_logged_and_exits_1(self):
        insert_session(self.conn, session_id="s1", iterm_session_id="w0t1p0:ABCD", term_program="iTerm.app")
        fake = mock.Mock(side_effect=subprocess.TimeoutExpired(["osascript"], cli.OSASCRIPT_TIMEOUT_SECS))
        args = cli.build_parser().parse_args(["focus", "claude", "s1"])
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.cmd_focus(args, run_osascript=fake), 1)
        fake.assert_called_once()  # no open -a fallback after a hang
        lines = self._focus_lines()
        self.assertEqual(len(lines), 1, self._log_text())
        self.assertIn(" WARN cli.focus ", lines[0])
        self.assertIn("outcome=timeout", lines[0])
        self.assertIn("timed out after", lines[0])
        self.assertIn("timed out", err.getvalue())

    def test_focus_apple_terminal_timeout(self):
        insert_session(self.conn, session_id="s1", term_program="Apple_Terminal", tty="ttys009")
        fake = mock.Mock(side_effect=subprocess.TimeoutExpired(["osascript"], 10))
        args = cli.build_parser().parse_args(["focus", "claude", "s1"])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.cmd_focus(args, run_osascript=fake), 1)
        self.assertIn("outcome=timeout", self._focus_lines()[0])

    def test_focus_unmapped_term_program_fails(self):
        insert_session(self.conn, session_id="s1", term_program="SomeRandomTerminal")
        fake = mock.Mock()
        args = cli.build_parser().parse_args(["focus", "claude", "s1"])
        result = cli.cmd_focus(args, run_osascript=fake)
        self.assertEqual(result, 1)
        fake.assert_not_called()
        line = self._assert_outcome("unsupported_terminal")
        self.assertIn("term=SomeRandomTerminal", line)

    def test_focus_no_terminal_recorded(self):
        insert_session(self.conn, session_id="s1")
        fake = mock.Mock()
        args = cli.build_parser().parse_args(["focus", "claude", "s1"])
        result = cli.cmd_focus(args, run_osascript=fake)
        self.assertEqual(result, 1)
        fake.assert_not_called()
        self._assert_outcome("no_terminal")

    def test_focus_unknown_session_is_no_terminal(self):
        fake = mock.Mock()
        args = cli.build_parser().parse_args(["focus", "claude", "nope"])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.cmd_focus(args, run_osascript=fake), 1)
        self._assert_outcome("no_terminal")

    def test_focus_logs_each_strategy_at_debug(self):
        insert_session(self.conn, session_id="s1", iterm_session_id="w0t1p0:ABCD", term_program="iTerm.app")
        fake = mock.Mock(side_effect=[_completed("not_found\n"), _completed("", args=["open"])])
        args = cli.build_parser().parse_args(["focus", "claude", "s1"])
        cli.cmd_focus(args, run_osascript=fake)
        tries = [l for l in self._log_text().splitlines() if "DEBUG cli.focus.try" in l]
        self.assertEqual(len(tries), 2)
        self.assertIn("strategy=iterm_script", tries[0])
        self.assertIn("unique_id=ABCD", tries[0])
        self.assertIn("strategy=open_a", tries[1])

    def _ordered_lines(self, script):
        return [line.strip() for line in script.strip().splitlines() if line.strip()]

    def _count_contiguous_runs(self, lines, expected):
        count = 0
        for i in range(len(lines) - len(expected) + 1):
            if lines[i:i + len(expected)] == expected:
                count += 1
        return count

    def test_iterm_focus_reasserts_tab_and_session_after_activate(self):
        # Selecting the window alone (what the old script did last) raises
        # it but activation is async, so a following `select t`/`select s`
        # on the still-background window is lost. The fix: select tab then
        # session (sets the current tab/session even while backgrounded),
        # then the window, activate, delay, and reassert tab+session once
        # the window is actually frontmost. Must apply to both the
        # unique-id match and the tty-fallback match.
        expected = [
            "tell t to select",
            "tell s to select",
            "tell w to select",
            "activate",
            "delay 0.15",
            "tell t to select",
            "tell s to select",
        ]
        lines = self._ordered_lines(cli._ITERM_FOCUS_APPLESCRIPT)
        # Once for the unique-id path and once for the tty fallback, each
        # reporting which strategy matched.
        self.assertEqual(
            self._count_contiguous_runs(lines, expected + ['return "found:uuid"']), 1,
            "expected this exact sequence once for the unique-id path",
        )
        self.assertEqual(
            self._count_contiguous_runs(lines, expected + ['return "found:tty"']), 1,
            "expected this exact sequence once for the tty fallback",
        )
        self.assertNotIn('return "found"', lines)

    def test_apple_terminal_focus_reasserts_selected_after_activate(self):
        expected = [
            "set selected of t to true",
            "set frontmost of w to true",
            "activate",
            "set selected of t to true",
            'return "found"',
        ]
        lines = self._ordered_lines(cli._TERMINAL_FOCUS_APPLESCRIPT)
        self.assertEqual(self._count_contiguous_runs(lines, expected), 1)

    @unittest.skipUnless(shutil.which("osacompile"), "requires macOS osacompile")
    def test_iterm_focus_script_compiles(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "test.scpt")
            proc = subprocess.run(
                ["osacompile", "-o", out],
                input=cli._ITERM_FOCUS_APPLESCRIPT, text=True, capture_output=True,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    @unittest.skipUnless(shutil.which("osacompile"), "requires macOS osacompile")
    def test_apple_terminal_focus_script_compiles(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "test.scpt")
            proc = subprocess.run(
                ["osacompile", "-o", out],
                input=cli._TERMINAL_FOCUS_APPLESCRIPT, text=True, capture_output=True,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_resume_print_outputs_command(self):
        insert_session(self.conn, session_id="s1", cwd="/tmp/proj")
        args = cli.build_parser().parse_args(["resume", "claude", "s1", "--print"])
        fake = mock.Mock()
        with mock.patch("builtins.print") as mock_print:
            cli.cmd_resume(args, run_osascript=fake)
        fake.assert_not_called()
        mock_print.assert_called_once()
        self.assertIn("claude --resume s1", mock_print.call_args[0][0])

    def test_resume_apple_terminal_runs_do_script(self):
        insert_session(self.conn, session_id="s1", cwd="/tmp/proj", term_program="Apple_Terminal")
        args = cli.build_parser().parse_args(["resume", "claude", "s1"])
        fake = mock.Mock(return_value=_completed(""))
        result = cli.cmd_resume(args, run_osascript=fake)
        self.assertEqual(result, 0)
        script_args = fake.call_args[0][0]
        self.assertIn(cli._TERMINAL_RESUME_APPLESCRIPT, script_args)
        self.assertIn("claude --resume s1", script_args[-1])

    def test_resume_generic_terminal_just_activates(self):
        insert_session(self.conn, session_id="s1", cwd="/tmp/proj", term_program="WarpTerminal")
        args = cli.build_parser().parse_args(["resume", "claude", "s1"])
        fake = mock.Mock(return_value=_completed(""))
        result = cli.cmd_resume(args, run_osascript=fake)
        self.assertEqual(result, 0)
        fake.assert_called_once_with(
            ["open", "-a", "Warp"], capture_output=True, text=True,
            timeout=cli.OSASCRIPT_TIMEOUT_SECS,
        )

    def test_resume_timeout_is_logged_and_exits_1(self):
        insert_session(self.conn, session_id="s1", cwd="/tmp/proj", term_program="iTerm.app")
        args = cli.build_parser().parse_args(["resume", "claude", "s1"])
        fake = mock.Mock(side_effect=subprocess.TimeoutExpired(["osascript"], 10))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.cmd_resume(args, run_osascript=fake), 1)
        self.assertEqual(fake.call_args.kwargs.get("timeout"), cli.OSASCRIPT_TIMEOUT_SECS)
        lines = [l for l in self._log_text().splitlines() if " cli.resume " in l]
        self.assertEqual(len(lines), 1)
        self.assertIn(" WARN cli.resume ", lines[0])
        self.assertIn("outcome=timeout", lines[0])
        self.assertIn("timed out", err.getvalue())


class _LogLevelMixin:
    """Run with a given AGENTBOARD_LOG_LEVEL (None = unset) and read back
    $AGENTBOARD_HOME/agentboard.log."""

    log_level = None

    def setUp(self):
        super().setUp()
        self._old_level = os.environ.pop("AGENTBOARD_LOG_LEVEL", None)
        if self.log_level:
            os.environ["AGENTBOARD_LOG_LEVEL"] = self.log_level

    def tearDown(self):
        os.environ.pop("AGENTBOARD_LOG_LEVEL", None)
        if self._old_level is not None:
            os.environ["AGENTBOARD_LOG_LEVEL"] = self._old_level
        super().tearDown()

    def _log_lines(self):
        path = self.agentboard_home / "agentboard.log"
        return path.read_text().splitlines() if path.exists() else []


class MainWrapperTests(_LogLevelMixin, AgentBoardTestCase):
    log_level = "debug"

    def test_start_and_end_lines_with_redacted_argv(self):
        insert_session(self.conn, session_id="s1")
        rc = cli.main(["mark", "claude", "s1", "done", "--note", "private words"])
        self.assertEqual(rc, 0)
        lines = self._log_lines()
        start = [l for l in lines if " cli.start " in l]
        end = [l for l in lines if " cli.end " in l]
        self.assertEqual(len(start), 1)
        self.assertIn("DEBUG cli.start cmd=mark", start[0])
        self.assertIn("<redacted:13 chars>", start[0])
        self.assertNotIn("private words", "\n".join(lines))
        self.assertEqual(len(end), 1)
        self.assertIn("INFO cli.end cmd=mark exit=0 ms=", end[0])
        mark = [l for l in lines if " cli.mark " in l]
        self.assertEqual(len(mark), 1)
        self.assertIn("status=done", mark[0])

    def test_abbreviated_redacted_flag_is_rejected_before_logging(self):
        # `--not` would be argparse's abbreviation of `--note`, which redact()
        # only recognises verbatim; allow_abbrev=False rejects it up front.
        insert_session(self.conn, session_id="s1")
        for flag in ("--not", "--no"):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["mark", "claude", "s1", "done", flag, "private words"])
            self.assertEqual(ctx.exception.code, 2)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.main(["link", "--kind", "pr", "--ref", "1", "--titl", "private words"])
        self.assertNotIn("private words", "\n".join(self._log_lines()))

    def test_full_flags_still_parse_everywhere(self):
        parser = cli.build_parser()
        args = parser.parse_args(["status", "--text", "hi", "--ttl", "5"])
        self.assertEqual(args.text, "hi")

    def test_uncaught_exception_logs_error_and_propagates(self):
        def boom(args):
            raise RuntimeError("database on fire")

        parser = cli.build_parser()
        args = parser.parse_args(["prune"])
        args.func = boom
        with mock.patch.object(cli, "build_parser") as build:
            build.return_value.parse_args.return_value = args
            with self.assertRaises(RuntimeError):
                cli.main(["prune"])
        errors = [l for l in self._log_lines() if " cli.error " in l]
        self.assertEqual(len(errors), 1)
        self.assertIn("ERROR cli.error cmd=prune exc=RuntimeError", errors[0])
        self.assertIn('msg="database on fire"', errors[0])

    def test_nonzero_exit_is_logged_at_info(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = cli.main(["backfill", "--session", "nope"])
        self.assertEqual(rc, 1)
        lines = self._log_lines()
        self.assertTrue(any("ERROR cli.backfill" in l and "--session must be TOOL:ID" in l for l in lines))
        self.assertTrue(any("INFO cli.end cmd=backfill exit=1" in l for l in lines))
        self.assertIn("--session must be TOOL:ID", err.getvalue())


class QuietSnapshotTests(_LogLevelMixin, AgentBoardTestCase):
    log_level = None  # the default, info

    def _run_snapshot(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return cli.main(["snapshot", "--json"])

    def test_fast_snapshot_leaves_no_info_line(self):
        self.assertEqual(self._run_snapshot(), 0)
        self.assertEqual([l for l in self._log_lines() if " INFO " in l], [])

    def test_slow_snapshot_is_logged_at_info(self):
        with mock.patch.object(cli.diag, "ms_since", return_value=1500):
            self._run_snapshot()
        lines = self._log_lines()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("INFO cli.end cmd=snapshot exit=0 ms=1500", lines[0])

    def test_failed_snapshot_is_logged(self):
        with mock.patch.object(cli, "build_snapshot", side_effect=RuntimeError("bad row")):
            with self.assertRaises(RuntimeError):
                self._run_snapshot()
        self.assertTrue(any("ERROR cli.error cmd=snapshot" in l for l in self._log_lines()))

    def test_paths_is_quiet_too(self):
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["paths", "--json"])
        self.assertEqual(self._log_lines(), [])


class HookSpawnedQuietTests(_LogLevelMixin, AgentBoardTestCase):
    """`backfill --session` and `map-codex` run once per assistant turn
    (spawned by hook.py): a routine success adds no info line."""

    log_level = None  # the default, info

    def _transcript(self):
        path = self.tmp_path / "sess1.jsonl"
        path.write_text(json.dumps({
            "type": "user", "message": {"role": "user", "content": "fix it"},
            "timestamp": "2026-01-01T00:00:00.000Z", "cwd": "/tmp", "gitBranch": "main",
        }) + "\n")
        return path

    def test_successful_session_backfill_is_quiet(self):
        path = self._transcript()
        with contextlib.redirect_stdout(io.StringIO()):
            rc = cli.main(["backfill", "--session", "claude:sess1", "--transcript", str(path)])
        self.assertEqual(rc, 0)
        self.assertEqual(self._log_lines(), [])

    def test_failed_session_backfill_is_still_info(self):
        with contextlib.redirect_stderr(io.StringIO()):
            cli.main(["backfill", "--session", "nope"])
        self.assertTrue(any("INFO cli.end cmd=backfill exit=1" in l for l in self._log_lines()))

    def test_slow_session_backfill_is_info(self):
        path = self._transcript()
        with mock.patch.object(cli.diag, "ms_since", return_value=1500):
            with contextlib.redirect_stdout(io.StringIO()):
                cli.main(["backfill", "--session", "claude:sess1", "--transcript", str(path)])
        self.assertTrue(any("INFO cli.end cmd=backfill exit=0 ms=1500" in l for l in self._log_lines()))

    def test_full_backfill_still_ends_at_info(self):
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["backfill", "--days", "1"])
        self.assertTrue(any("INFO cli.end cmd=backfill exit=0" in l for l in self._log_lines()))

    def test_successful_map_codex_is_quiet(self):
        with mock.patch("agentboard.cli.codexmap.map_codex_session", return_value=True):
            with contextlib.redirect_stdout(io.StringIO()):
                cli.main(["map-codex", "--session", "s1", "--cwd", "/tmp/proj"])
        self.assertEqual(self._log_lines(), [])


def _ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


class LogsCommandTests(_LogLevelMixin, AgentBoardTestCase):
    def setUp(self):
        super().setUp()
        self.now = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)

    def _write(self, name, entries):
        path = self.agentboard_home / name
        with path.open("a") as fh:
            for minutes_ago, text in entries:
                if minutes_ago is None:
                    fh.write(text + "\n")
                else:
                    fh.write(f"{_ts(self.now - timedelta(minutes=minutes_ago))} {text}\n")

    def _logs(self, argv, run=None):
        args = cli.build_parser().parse_args(["logs"] + argv)
        out, err = io.StringIO(), io.StringIO()
        run = run or mock.Mock(side_effect=AssertionError("log show must not run"))
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.cmd_logs(args, run=run, now=self.now)
        return rc, out.getvalue().splitlines(), err.getvalue()

    def test_reads_rotated_file_first_then_live(self):
        self._write("agentboard.log.1", [(30, "INFO old.one"), (20, "INFO old.two")])
        self._write("agentboard.log", [(10, "INFO new.one")])
        rc, lines, _ = self._logs([])
        self.assertEqual(rc, 0)
        self.assertEqual([l.split(" ", 2)[2] for l in lines], ["old.one", "old.two", "new.one"])

    def test_since_window_spanning_the_rotation(self):
        self._write("agentboard.log.1", [(90, "INFO too.old"), (45, "INFO in.rotated")])
        self._write("agentboard.log", [(5, "INFO in.live")])
        rc, lines, _ = self._logs(["--since", "1h"])
        self.assertEqual([l.split(" ", 2)[2] for l in lines], ["in.rotated", "in.live"])

    def test_since_iso_timestamp(self):
        self._write("agentboard.log", [(30, "INFO a"), (10, "INFO b")])
        cutoff = _ts(self.now - timedelta(minutes=20))
        _, lines, _ = self._logs(["--since", cutoff])
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].endswith("INFO b"))

    def test_unparseable_lines_pass_the_since_filter(self):
        self._write("agentboard.log", [(90, "INFO old"), (None, "garbage without a timestamp"), (1, "INFO new")])
        _, lines, _ = self._logs(["--since", "30m"])
        self.assertEqual(lines[0], "garbage without a timestamp")
        self.assertTrue(lines[1].endswith("INFO new"))

    def test_grep_is_a_plain_substring(self):
        self._write("agentboard.log", [
            (3, "INFO cli.focus key=claude:a outcome=matched_tty"),
            (2, "INFO cli.end cmd=mark exit=0"),
            (1, "INFO cli.focus key=claude:b outcome=activated_only"),
        ])
        _, lines, _ = self._logs(["--grep", "cli.focus"])
        self.assertEqual(len(lines), 2)
        _, lines, _ = self._logs(["--grep", "outcome=activated_only"])
        self.assertEqual(len(lines), 1)
        _, lines, _ = self._logs(["--grep", "cli.f.cus"])
        self.assertEqual(lines, [])

    def test_default_is_last_200_lines(self):
        self._write("agentboard.log", [(300 - i, f"INFO n.{i}") for i in range(300)])
        _, lines, _ = self._logs([])
        self.assertEqual(len(lines), 200)
        self.assertTrue(lines[0].endswith("INFO n.100"))
        _, lines, _ = self._logs(["--lines", "5"])
        self.assertEqual(len(lines), 5)

    def test_bad_since_is_an_error(self):
        rc, _, err = self._logs(["--since", "yesterday"])
        self.assertEqual(rc, 2)
        self.assertIn("--since", err)

    def test_missing_log_prints_nothing(self):
        rc, lines, err = self._logs([])
        self.assertEqual((rc, lines, err), (0, [], ""))

    def _app_run(self, stdout="", returncode=0, stderr=""):
        return mock.Mock(return_value=subprocess.CompletedProcess(["log"], returncode, stdout, stderr))

    def _start_arg(self, run):
        argv = run.call_args[0][0]
        self.assertEqual(argv[:6], ["log", "show", "--style", "compact", "--info", "--debug"])
        self.assertIn("--predicate", argv)
        self.assertEqual(argv[argv.index("--predicate") + 1], 'subsystem == "io.github.agentboard"')
        self.assertNotIn("--last", argv)
        return argv[argv.index("--start") + 1]

    def _local(self, dt):
        return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")

    def test_app_with_relative_since_uses_absolute_start(self):
        run = self._app_run()
        self._logs(["--app", "--since", "1h"], run=run)
        self.assertEqual(self._start_arg(run), self._local(self.now - timedelta(hours=1)))

    def test_app_with_iso_since_uses_absolute_start(self):
        run = self._app_run()
        self._logs(["--app", "--since", "2026-10-17T11:15:00Z"], run=run)
        self.assertEqual(
            self._start_arg(run), self._local(datetime(2026, 10, 17, 11, 15, tzinfo=timezone.utc))
        )

    def test_app_without_since_starts_at_oldest_shown_line(self):
        self._write("agentboard.log", [(42, "INFO first"), (2, "INFO second")])
        run = self._app_run()
        self._logs(["--app"], run=run)
        oldest = (self.now - timedelta(minutes=42)).replace(microsecond=0)
        self.assertEqual(self._start_arg(run), self._local(oldest))

    def test_app_without_since_or_lines_defaults_to_one_hour(self):
        run = self._app_run()
        self._logs(["--app"], run=run)
        self.assertEqual(self._start_arg(run), self._local(self.now - timedelta(hours=1)))

    def test_app_lines_are_merged_by_timestamp(self):
        self._write("agentboard.log", [(10, "INFO cli.focus outcome=matched_tty"), (1, "INFO later")])
        app_ts = (self.now - timedelta(minutes=5)).astimezone().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        stdout = (
            "Timestamp               Ty Process[PID:TID]\n"
            f"{app_ts} N  AgentBoard[1:2] [io.github.agentboard:cli] args=\"focus claude s1\" exit=0 ms=400\n"
        )
        _, lines, err = self._logs(["--app", "--since", "1h"], run=self._app_run(stdout))
        self.assertEqual(err, "")
        self.assertEqual(len(lines), 3)
        self.assertIn("cli.focus", lines[0])
        self.assertIn(" APP ", lines[1])
        self.assertIn('args="focus claude s1"', lines[1])
        self.assertTrue(lines[1].startswith(_ts(self.now - timedelta(minutes=5))[:19]))
        self.assertTrue(lines[2].endswith("INFO later"))

    def test_app_log_show_failure_warns_and_prints_file_lines(self):
        self._write("agentboard.log", [(1, "INFO still.here")])
        rc, lines, err = self._logs(
            ["--app", "--since", "1h"], run=self._app_run(returncode=64, stderr="log: bad predicate")
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(lines), 1)
        self.assertIn("warning:", err)
        self.assertIn("bad predicate", err)
        self.assertEqual(len(err.strip().splitlines()), 1)

    def test_app_missing_log_binary_warns_and_prints_file_lines(self):
        self._write("agentboard.log", [(1, "INFO still.here")])
        run = mock.Mock(side_effect=FileNotFoundError(2, "No such file", "log"))
        rc, lines, err = self._logs(["--app", "--since", "1h"], run=run)
        self.assertEqual(rc, 0)
        self.assertEqual(len(lines), 1)
        self.assertIn("warning:", err)

    def test_logs_itself_adds_nothing_at_info(self):
        self._write("agentboard.log", [(1, "INFO x")])
        before = self._log_lines()
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["logs"])
        self.assertEqual(self._log_lines(), before)

    def test_logs_output_does_not_include_its_own_run_at_debug(self):
        os.environ["AGENTBOARD_LOG_LEVEL"] = "debug"
        self._write("agentboard.log", [(1, "INFO x")])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli.main(["logs"])
        printed = out.getvalue()
        self.assertNotIn("cmd=logs", printed)
        self.assertFalse(any("cli.start cmd=logs" in l for l in self._log_lines()))

    def test_successful_logs_leaves_no_footprint_at_debug(self):
        os.environ["AGENTBOARD_LOG_LEVEL"] = "debug"
        self._write("agentboard.log", [(1, "INFO x")])
        before = self._log_lines()
        for _ in range(2):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["logs"]), 0)
        self.assertEqual(self._log_lines(), before)

    def test_failing_logs_still_records_its_end_line(self):
        os.environ["AGENTBOARD_LOG_LEVEL"] = "debug"
        self._write("agentboard.log", [(1, "INFO x")])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["logs", "--since", "garbage"]), 2)
        lines = self._log_lines()
        self.assertTrue(any("INFO cli.end cmd=logs exit=2" in l for l in lines), lines)
        self.assertFalse(any("cli.start cmd=logs" in l for l in lines))


class BackfillCommandTests(AgentBoardTestCase):
    def test_single_session_backfill_via_cli(self):
        path = self.tmp_path / "sess1.jsonl"
        path.write_text(
            json.dumps({
                "type": "user", "message": {"role": "user", "content": "fix it"},
                "timestamp": "2026-01-01T00:00:00.000Z", "cwd": "/tmp", "gitBranch": "main",
            }) + "\n"
        )
        args = cli.build_parser().parse_args([
            "backfill", "--session", "claude:sess1", "--transcript", str(path),
        ])
        result = cli.cmd_backfill(args)
        self.assertEqual(result, 0)
        row = self.conn.execute(
            "SELECT first_prompt FROM sessions WHERE tool='claude' AND session_id='sess1'"
        ).fetchone()
        self.assertEqual(row["first_prompt"], "fix it")

    def test_malformed_session_arg(self):
        args = cli.build_parser().parse_args(["backfill", "--session", "not-a-tool-id"])
        result = cli.cmd_backfill(args)
        self.assertEqual(result, 1)


class MapCodexCommandTests(AgentBoardTestCase):
    def test_cli_wiring_passes_args_through(self):
        args = cli.build_parser().parse_args([
            "map-codex", "--session", "sess1", "--cwd", "/tmp/proj", "--at", "2026-06-15T12:00:00Z",
        ])
        with mock.patch("agentboard.cli.codexmap.map_codex_session", return_value=True) as fn:
            result = cli.cmd_map_codex(args)
        self.assertEqual(result, 0)
        fn.assert_called_once()
        call_args, call_kwargs = fn.call_args
        self.assertEqual(call_args[1], "sess1")
        self.assertEqual(call_args[2], "/tmp/proj")
        self.assertEqual(call_kwargs["at"], "2026-06-15T12:00:00Z")

    def test_cli_prints_unmapped_when_no_match(self):
        args = cli.build_parser().parse_args([
            "map-codex", "--session", "sess1", "--cwd", "/tmp/proj",
        ])
        with mock.patch("agentboard.cli.codexmap.map_codex_session", return_value=False):
            with mock.patch("builtins.print") as fake_print:
                cli.cmd_map_codex(args)
        fake_print.assert_called_once_with("unmapped")


class PruneTests(AgentBoardTestCase):
    def test_prune_removes_old_sessions_everywhere(self):
        old = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        recent = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        insert_session(self.conn, session_id="old", updated_at=old)
        insert_session(self.conn, session_id="recent", updated_at=recent)
        self.conn.execute(
            "INSERT INTO events(ts, tool, session_id, event) VALUES (?, 'claude','old','Stop')", (old,)
        )
        self.conn.execute(
            "INSERT INTO triage(tool, session_id, status, until, note, updated_at) "
            "VALUES ('claude','old','done',NULL,NULL,?)", (old,),
        )
        args = cli.build_parser().parse_args(["prune", "--days", "90"])
        cli.cmd_prune(args)

        self.assertIsNone(
            self.conn.execute("SELECT 1 FROM sessions WHERE session_id='old'").fetchone()
        )
        self.assertIsNone(
            self.conn.execute("SELECT 1 FROM events WHERE session_id='old'").fetchone()
        )
        self.assertIsNone(
            self.conn.execute("SELECT 1 FROM triage WHERE session_id='old'").fetchone()
        )
        self.assertIsNotNone(
            self.conn.execute("SELECT 1 FROM sessions WHERE session_id='recent'").fetchone()
        )

    def test_prune_removes_notes_of_pruned_sessions_and_old_dismissed_notes(self):
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=120)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        recent = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        insert_session(self.conn, session_id="old", updated_at=old)
        insert_session(self.conn, session_id="live", updated_at=recent)

        def add(session_id, text, dismissed_at=None):
            self.conn.execute(
                "INSERT INTO notes(tool, session_id, ts, last_ts, kind, text, fingerprint, "
                "source, dismissed_at) VALUES ('claude', ?, ?, ?, 'update', ?, ?, 'cli', ?)",
                (session_id, recent, recent, text, f"update:{text}", dismissed_at),
            )

        add("old", "pruned with its session")
        add("live", "open on a live session")
        add("live", "recently dismissed", dismissed_at=recent)
        add("live", "long dismissed", dismissed_at=old)
        rc, out, _ = _run_cmd(["prune", "--days", "90"])
        self.assertEqual(rc, 0)
        self.assertIn("1 dismissed note(s)", out)
        texts = sorted(r["text"] for r in self.conn.execute("SELECT text FROM notes"))
        self.assertEqual(texts, ["open on a live session", "recently dismissed"])

    # -- retention rules ---------------------------------------------------------

    def _iso(self, **delta):
        return (datetime.now(timezone.utc) - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def _session_ids(self):
        return sorted(r["session_id"] for r in self.conn.execute("SELECT session_id FROM sessions"))

    def _add_note(self, session_id, kind="update", text="t", dismissed_at=None):
        now = self._iso(days=0)
        self.conn.execute(
            "INSERT INTO notes(tool, session_id, ts, last_ts, kind, text, fingerprint, "
            "source, dismissed_at) VALUES ('claude', ?, ?, ?, ?, ?, ?, 'cli', ?)",
            (session_id, now, now, kind, text, f"{kind}:{text}", dismissed_at),
        )

    def test_prune_skips_open_needs_you_note(self):
        old = self._iso(days=120)
        insert_session(self.conn, session_id="asks", updated_at=old)
        insert_session(self.conn, session_id="answered", updated_at=old)
        insert_session(self.conn, session_id="quiet", updated_at=old)
        self._add_note("asks", kind="needs_you", text="approve the deploy")
        self._add_note("answered", kind="needs_you", text="done", dismissed_at=self._iso(days=1))
        self._add_note("quiet", kind="update", text="shipped")
        rc, out, _ = _run_cmd(["prune"])
        self.assertEqual(rc, 0)
        self.assertEqual(self._session_ids(), ["asks"])
        self.assertIn("pruned 2 session(s) (1 skipped)", out)

    def test_prune_skips_awaiting_pr(self):
        old = self._iso(days=120)
        insert_session(self.conn, session_id="awaits", updated_at=old)
        insert_session(self.conn, session_id="merged", updated_at=old)
        insert_session(self.conn, session_id="answered", updated_at=old)
        for session_id, state, responded in (
            ("awaits", "OPEN", None), ("merged", "MERGED", None), ("answered", "OPEN", old),
        ):
            self.conn.execute(
                "INSERT INTO links(tool, session_id, kind, ref, url, state, source, responded_at) "
                "VALUES ('claude', ?, 'pr', '#1', 'https://example.com/pr/1', ?, 'cli', ?)",
                (session_id, state, responded),
            )
        _run_cmd(["prune"])
        self.assertEqual(self._session_ids(), ["awaits"])

    def test_prune_keeps_only_an_owned_gh_pr(self):
        # gh-found PRs are the session's own only when created inside its
        # lifetime; one that predates the session is inherited from the
        # branch and does not hold the session back.
        old, started, ended = self._iso(days=120), self._iso(days=130), self._iso(days=121)
        inside, before = self._iso(days=125), self._iso(days=140)
        for session_id, created in (("owns", inside), ("inherits", before)):
            insert_session(self.conn, session_id=session_id, updated_at=old,
                           started_at=started, ended_at=ended)
            self.conn.execute(
                "INSERT INTO links(tool, session_id, kind, ref, url, state, source, created_at) "
                "VALUES ('claude', ?, 'pr', '#7', 'https://example.com/pr/7', 'OPEN', 'gh', ?)",
                (session_id, created),
            )
        rc, out, _ = _run_cmd(["prune"])
        self.assertEqual(rc, 0)
        self.assertEqual(self._session_ids(), ["owns"])
        self.assertIn("pruned 1 session(s) (1 skipped)", out)

    def test_prune_skips_active_snooze(self):
        old = self._iso(days=120)
        insert_session(self.conn, session_id="snoozed", updated_at=old)
        insert_session(self.conn, session_id="woke", updated_at=old)
        future = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        for session_id, until in (("snoozed", future), ("woke", old)):
            self.conn.execute(
                "INSERT INTO triage(tool, session_id, status, until, note, updated_at) "
                "VALUES ('claude', ?, 'snoozed', ?, NULL, ?)", (session_id, until, old),
            )
        _run_cmd(["prune"])
        self.assertEqual(self._session_ids(), ["snoozed"])

    def _write_registry(self, pid, session_id):
        # HOME is a temp dir (AgentBoardTestCase), never the real ~/.claude.
        sessions_dir = Path(os.environ["HOME"]) / ".claude" / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        (sessions_dir / f"{pid}.json").write_text(
            json.dumps({"sessionId": session_id, "status": "idle"}))

    def test_prune_skips_a_live_process(self):
        old = self._iso(days=120)
        insert_session(self.conn, session_id="alive", updated_at=old, pid=os.getpid())
        insert_session(self.conn, session_id="ended", updated_at=old, pid=os.getpid(),
                       ended_at=old)
        insert_session(self.conn, tool="codex", session_id="codex-alive", updated_at=old,
                       pid=os.getpid())
        self._write_registry(os.getpid(), "alive")
        _run_cmd(["prune"])
        self.assertEqual(self._session_ids(), ["alive", "codex-alive"])

    def test_prune_treats_an_unregistered_claude_pid_as_reused(self):
        old = self._iso(days=120)
        # A crashed session (no SessionEnd) whose pid now belongs to some
        # other process: no registry file, or one naming another session.
        insert_session(self.conn, session_id="crashed", updated_at=old, pid=os.getpid())
        insert_session(self.conn, session_id="other-owner", updated_at=old, pid=os.getpid())
        self._write_registry(os.getpid(), "someone-else")
        rc, out, _ = _run_cmd(["prune"])
        self.assertEqual(rc, 0)
        self.assertEqual(self._session_ids(), [])
        self.assertIn("pruned 2 session(s) (0 skipped)", out)

    def test_compact_deletes_only_tool_events_older_than_window(self):
        insert_session(self.conn, session_id="s1", updated_at=self._iso(days=0))
        old, recent = self._iso(days=10), self._iso(days=1)
        compacted = ["PreToolUse", "PostToolUse", "PostToolUseFailure", "SubagentStart",
                     "SubagentStop", "PreCompact", "PostCompact", "CwdChanged",
                     "ElicitationResult", "PermissionDenied"]
        kept = ["SessionStart", "UserPromptSubmit", "Notification", "PermissionRequest",
                "Stop", "StopFailure", "SessionEnd", "Elicitation", "PostModelSwitch"]
        for event in compacted + kept:
            _insert_event(self.conn, event, ts=old)
            _insert_event(self.conn, event, ts=recent)
        _insert_event(self.conn, "PostToolUse", ts=old, agent_id="sub-1")
        rc, out, _ = _run_cmd(["prune", "--compact-days", "7"])
        self.assertEqual(rc, 0)
        self.assertIn(f"compacted {len(compacted) + 1} event(s)", out)
        rows = self.conn.execute("SELECT event, ts FROM events").fetchall()
        old_left = sorted(r["event"] for r in rows if r["ts"] == old)
        recent_left = sorted(r["event"] for r in rows if r["ts"] == recent)
        self.assertEqual(old_left, sorted(kept))
        self.assertEqual(recent_left, sorted(compacted + kept))
        self.assertEqual(self._session_ids(), ["s1"])

    def test_compact_days_from_config_and_zero_never_compacts(self):
        insert_session(self.conn, session_id="s1", updated_at=self._iso(days=0))
        _insert_event(self.conn, "PreToolUse", ts=self._iso(days=3))
        (self.agentboard_home / "config.json").write_text(json.dumps({"compact_days": 2}))
        _, out, _ = _run_cmd(["prune", "--compact-days", "0"])
        self.assertIn("compacted 0 event(s)", out)
        _, out, _ = _run_cmd(["prune"])
        self.assertIn("compacted 1 event(s)", out)

    def test_prune_days_from_config(self):
        insert_session(self.conn, session_id="s1", updated_at=self._iso(days=20))
        (self.agentboard_home / "config.json").write_text(json.dumps({"prune_days": 30}))
        _run_cmd(["prune"])
        self.assertEqual(self._session_ids(), ["s1"])
        (self.agentboard_home / "config.json").write_text(json.dumps({"prune_days": 10}))
        _run_cmd(["prune"])
        self.assertEqual(self._session_ids(), [])

    def test_prune_auto_is_gated_by_meta(self):
        insert_session(self.conn, session_id="old", updated_at=self._iso(days=120))
        rc, out, _ = _run_cmd(["prune", "--auto"])
        self.assertEqual(rc, 0)
        self.assertIn("pruned 1 session(s)", out)
        stamp = self.conn.execute(
            "SELECT value FROM notes_meta WHERE key='last_prune_at'").fetchone()["value"]
        self.assertAlmostEqual(stamp, int(datetime.now(timezone.utc).timestamp()), delta=60)

        insert_session(self.conn, session_id="old2", updated_at=self._iso(days=120))
        rc, out, _ = _run_cmd(["prune", "--auto"])
        self.assertEqual(rc, 0)
        self.assertIn("skipped", out)
        self.assertEqual(self._session_ids(), ["old2"])

        # A manual run is never gated and leaves the auto stamp alone.
        _run_cmd(["prune"])
        self.assertEqual(self._session_ids(), [])
        self.assertEqual(self.conn.execute(
            "SELECT value FROM notes_meta WHERE key='last_prune_at'").fetchone()["value"], stamp)

        insert_session(self.conn, session_id="old3", updated_at=self._iso(days=120))
        self.conn.execute("UPDATE notes_meta SET value=value - 25 * 3600 WHERE key='last_prune_at'")
        _, out, _ = _run_cmd(["prune", "--auto"])
        self.assertIn("pruned 1 session(s)", out)
        self.assertEqual(self._session_ids(), [])

    def test_prune_auto_keeps_rebuild_meta(self):
        before = {r["key"]: r["value"] for r in self.conn.execute("SELECT * FROM notes_meta")}
        _run_cmd(["prune", "--auto"])
        after = {r["key"]: r["value"] for r in self.conn.execute("SELECT * FROM notes_meta")}
        self.assertEqual({k: after[k] for k in before}, before)
        self.assertIn("last_prune_at", after)

    def test_prune_dry_run_writes_nothing(self):
        insert_session(self.conn, session_id="old", updated_at=self._iso(days=120))
        insert_session(self.conn, session_id="new", updated_at=self._iso(days=0))
        _insert_event(self.conn, "PreToolUse", session_id="new", ts=self._iso(days=10))
        self._add_note("new", text="gone", dismissed_at=self._iso(days=120))
        before = self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        rc, out, _ = _run_cmd(["prune", "--dry-run", "--auto", "--vacuum"])
        self.assertEqual(rc, 0)
        self.assertIn("would prune 1 session(s) (0 skipped), compact 1 event(s), "
                      "1 dismissed note(s)", out)
        self.assertEqual(self._session_ids(), ["new", "old"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], before)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 1)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM notes_meta WHERE key IN ('last_prune_at', 'last_vacuum_at')").fetchone())

    def test_prune_dry_run_previews_past_the_auto_gate(self):
        insert_session(self.conn, session_id="old", updated_at=self._iso(days=120))
        stamp = int(datetime.now(timezone.utc).timestamp()) - 3600
        self.conn.execute(
            "INSERT INTO notes_meta(key, value) VALUES ('last_prune_at', ?)", (stamp,))
        rc, out, _ = _run_cmd(["prune", "--auto", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn("would prune 1 session(s)", out)
        self.assertNotIn("prune skipped", out)
        self.assertEqual(self._session_ids(), ["old"])
        self.assertEqual(self.conn.execute(
            "SELECT value FROM notes_meta WHERE key='last_prune_at'").fetchone()["value"], stamp)

    def test_prune_days_zero_never_deletes_sessions(self):
        insert_session(self.conn, session_id="ancient", updated_at=self._iso(days=4000))
        rc, out, _ = _run_cmd(["prune", "--days", "0"])
        self.assertEqual(rc, 0)
        self.assertIn("pruned 0 session(s)", out)
        self.assertEqual(self._session_ids(), ["ancient"])

    def test_prune_days_zero_keeps_dismissed_notes(self):
        insert_session(self.conn, session_id="s1", updated_at=self._iso(days=0))
        self._add_note("s1", text="long dismissed", dismissed_at=self._iso(days=4000))
        (self.agentboard_home / "config.json").write_text(json.dumps({"prune_days": 0}))
        _, out, _ = _run_cmd(["prune"])
        self.assertIn("0 dismissed note(s)", out)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 1)

    def test_negative_days_are_rejected(self):
        rc, _, err = _run_cmd(["prune", "--days", "-1"])
        self.assertEqual(rc, 2)
        self.assertIn("0 or more", err)

    def test_vacuum_runs_and_stamps(self):
        rc, out, _ = _run_cmd(["prune", "--vacuum"])
        self.assertEqual(rc, 0)
        self.assertTrue(out.rstrip().endswith(", vacuumed"))
        self.assertIsNotNone(self.conn.execute(
            "SELECT value FROM notes_meta WHERE key='last_vacuum_at'").fetchone())

    def test_auto_vacuums_only_past_the_free_page_threshold(self):
        _, out, _ = _run_cmd(["prune", "--auto"])
        self.assertNotIn("vacuumed", out)
        with mock.patch.object(cli, "VACUUM_MIN_FREE_BYTES", -1), \
                mock.patch.object(cli, "VACUUM_MIN_FREE_FRACTION", -1):
            self.conn.execute("UPDATE notes_meta SET value=0 WHERE key='last_prune_at'")
            _, out, _ = _run_cmd(["prune", "--auto"])
            self.assertIn("vacuumed", out)
            # At most weekly, whatever the free space.
            self.conn.execute("UPDATE notes_meta SET value=0 WHERE key='last_prune_at'")
            _, out, _ = _run_cmd(["prune", "--auto"])
            self.assertNotIn("vacuumed", out)


class SnapshotTurnFieldsTests(AgentBoardTestCase):
    """turn_finished_at / turn_prompt_kind / background_tasks /
    turn_ends_background_wait, driven through the real hook."""

    def _hook(self, event, tool="claude", session_id="s1", **payload):
        with mock.patch("agentboard.hook.subprocess.run") as run, \
             mock.patch("agentboard.hook.subprocess.Popen"):
            run.return_value = mock.Mock(returncode=0, stdout="")
            hook.process_payload(self.conn, tool, {
                "cwd": "/tmp/proj", "session_id": session_id, "hook_event_name": event,
                **payload,
            })

    def prompt(self, text="go", **kw):
        self._hook("UserPromptSubmit", prompt=text, **kw)

    def injected(self, **kw):
        self.prompt("<task-notification>task done</task-notification>", **kw)

    def stop(self, reply="Done.", background=None, **kw):
        payload = {"last_assistant_message": reply}
        if background is not None:
            payload["background_tasks"] = [
                {"type": "subagent", "status": "running"} for _ in range(background)
            ]
        self._hook("Stop", **payload, **kw)
        return self._newest_ts("Stop", kw.get("tool", "claude"), kw.get("session_id", "s1"))

    def _newest_ts(self, event, tool="claude", session_id="s1"):
        return self.conn.execute(
            "SELECT ts FROM events WHERE tool=? AND session_id=? AND event=? "
            "AND agent_id IS NULL ORDER BY id DESC LIMIT 1",
            (tool, session_id, event),
        ).fetchone()[0]

    def entry(self, key="claude:s1"):
        snapshot = cli.build_snapshot(self.conn, include_all=True)
        return next(e for e in snapshot["sessions"] if e["key"] == key)

    def test_stop_after_a_prompt_sets_the_stop_ts(self):
        self.prompt()
        stop_ts = self.stop(background=0)
        entry = self.entry()
        self.assertEqual(entry["turn_finished_at"], stop_ts)
        self.assertEqual(entry["turn_prompt_kind"], "human")
        self.assertEqual(entry["background_tasks"], 0)
        self.assertFalse(entry["turn_ends_background_wait"])

    def test_a_new_prompt_nulls_it(self):
        self.prompt()
        self.stop()
        self.prompt("and another thing")
        self.assertIsNone(self.entry()["turn_finished_at"])

    def test_a_stop_listing_background_tasks_gives_null(self):
        self.prompt()
        self.stop(background=2)
        entry = self.entry()
        self.assertIsNone(entry["turn_finished_at"])
        self.assertEqual(entry["background_tasks"], 2)

    def test_background_subagent_stop_keeps_it_null_then_the_report_back_sets_it(self):
        self.prompt()
        self.stop(background=1)
        self._hook("SubagentStop", agent_id="bg-1")
        entry = self.entry()
        # The session column dropped to 0, but the Stop event still lists one.
        self.assertEqual(entry["background_tasks"], 0)
        self.assertIsNone(entry["turn_finished_at"])
        self.injected()
        final = self.stop("Here is what the task found.", background=0)
        entry = self.entry()
        self.assertEqual(entry["turn_finished_at"], final)
        self.assertEqual(entry["turn_prompt_kind"], "injected")
        self.assertTrue(entry["turn_ends_background_wait"])

    def test_later_markers_leave_the_ts_unchanged(self):
        self.prompt()
        stop_ts = self.stop(background=0)
        self._hook("Notification", notification_type="idle_prompt",
                   message="Claude is waiting for your input")
        self._hook("SubagentStop", agent_id="bg-1")
        self._hook("PreCompact", trigger="manual")
        self.assertEqual(self.entry()["turn_finished_at"], stop_ts)

    def test_session_end_nulls_it(self):
        self.prompt()
        self.stop()
        self._hook("SessionEnd", reason="prompt_input_exit")
        self.assertIsNone(self.entry()["turn_finished_at"])

    def test_codex_stop_works_the_same(self):
        self.prompt(tool="codex")
        stop_ts = self.stop(tool="codex")
        entry = self.entry("codex:s1")
        self.assertEqual(entry["turn_finished_at"], stop_ts)
        self.assertIsNone(entry["background_tasks"])

    def test_ends_background_wait_after_a_chain_of_reports(self):
        self.prompt()
        self.stop(background=2)
        self.injected()
        self.stop(background=1)
        self.assertIsNone(self.entry()["turn_finished_at"])
        self.injected()
        final = self.stop(background=0)
        entry = self.entry()
        self.assertEqual(entry["turn_finished_at"], final)
        self.assertTrue(entry["turn_ends_background_wait"])

    def test_an_injected_peer_message_after_a_clean_stop_does_not_end_a_wait(self):
        self.prompt()
        self.stop(background=0)
        self.prompt("<agent-message from=peer>hello</agent-message>")
        stop_ts = self.stop("Hi back.", background=0)
        entry = self.entry()
        self.assertEqual(entry["turn_finished_at"], stop_ts)
        self.assertEqual(entry["turn_prompt_kind"], "injected")
        self.assertFalse(entry["turn_ends_background_wait"])

    def test_a_human_prompt_does_not_end_a_wait(self):
        self.prompt()
        self.stop(background=1)
        self.prompt("never mind, carry on")
        self.stop(background=0)
        entry = self.entry()
        self.assertEqual(entry["turn_prompt_kind"], "human")
        self.assertFalse(entry["turn_ends_background_wait"])

    def test_no_stop_yet_gives_null_and_no_prompt_gives_null_kind(self):
        insert_session(self.conn, session_id="legacy", last_event="Stop")
        entry = self.entry("claude:legacy")
        self.assertIsNone(entry["turn_finished_at"])
        self.assertIsNone(entry["turn_prompt_kind"])
        self.assertIsNone(entry["background_tasks"])
        self.assertFalse(entry["turn_ends_background_wait"])

    def test_idle_compact_keeps_the_state_but_not_turn_finished_at(self):
        # An idle /compact: the local-only UserPromptSubmit (prompt-less),
        # the compaction pair and SessionStart(compact). The hook keeps the
        # Stop and its last_event_at, so the state still reads Review "Your
        # turn" at the Stop's time.
        self.prompt()
        stop_ts = self.stop(background=0)
        self.prompt("<command-name>/compact</command-name>\n"
                    "<command-message>compact</command-message>\n"
                    "<command-args></command-args>")
        self._hook("PreCompact", trigger="manual")
        self._hook("PostCompact", trigger="manual")
        self._hook("SessionStart", source="compact")
        row = self.conn.execute(
            "SELECT last_event, last_event_at FROM sessions WHERE session_id='s1'"
        ).fetchone()
        self.assertEqual(row[0], "Stop")
        self.assertEqual(row[1], stop_ts)
        entry = self.entry()
        self.assertEqual(entry["state"], "review")
        self.assertEqual(entry["waiting_reason"], "Your turn: Done.")
        # Pins today's value, which disagrees with the state above:
        # links.last_parent_prompt_at(any_kind=True) still counts the
        # prompt-less /compact as a newer turn start, so the notify fields
        # read "not finished". The todo "Local-only slash commands should
        # not count as new work" fixes this; that change should turn this
        # assertion into assertEqual(entry["turn_finished_at"], stop_ts).
        self.assertIsNone(entry["turn_finished_at"])

    def test_the_newest_prompt_scan_runs_once_per_snapshot(self):
        # Two sessions, one waiting on a background report-back, so every
        # turn field has something to look up.
        self.prompt()
        self.stop(background=1)
        self.injected()
        self.prompt(session_id="s2")
        self.stop(session_id="s2")
        statements = []
        self.conn.set_trace_callback(statements.append)
        try:
            cli.build_snapshot(self.conn, include_all=True)
        finally:
            self.conn.set_trace_callback(None)
        # The grouped newest-prompt scans of any kind (the non-injected
        # one behind note release filters on prompt_kind and is separate).
        grouped_any_prompt = [
            sql for sql in statements
            if "event='UserPromptSubmit'" in sql and "GROUP BY" in sql
            and "<> 'injected'" not in sql
        ]
        self.assertEqual(len(grouped_any_prompt), 1, grouped_any_prompt)


if __name__ == "__main__":
    import unittest

    unittest.main()
