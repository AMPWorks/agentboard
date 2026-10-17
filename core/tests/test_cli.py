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

from agentboard import cli, hook
from tests.helpers import AgentBoardTestCase

FIXTURES = Path(__file__).resolve().parent / "fixtures"


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
        self.assertEqual(set(snapshot["counts"].keys()), set(cli.STATES))
        entry = snapshot["sessions"][0]
        for key in (
            "key", "tool", "session_id", "state", "waiting_reason", "title", "cwd",
            "project", "branch", "first_prompt", "last_prompt", "last_assistant",
            "started_at", "updated_at", "ended_at", "alive", "interactive",
            "can_focus", "resume_command", "status_text", "links", "triage",
        ):
            self.assertIn(key, entry)
        self.assertEqual(entry["key"], "claude:s1")
        self.assertEqual(entry["project"], "proj")

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
        self.conn.execute(
            "INSERT INTO links(tool, session_id, kind, ref, url, state, title, checked_at) "
            "VALUES ('claude','s1','pr','#1','u','MERGED','t','2026-06-15T10:00:00.000000Z')"
        )
        snapshot = cli.build_snapshot(self.conn, include_all=True)
        self.assertEqual(snapshot["sessions"][0]["state"], "done")

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
        self.assertNotEqual(entry["state"], "done")
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

    def test_link_unresolvable_exits_zero(self):
        args = cli.build_parser().parse_args(["link", "--kind", "pr", "--ref", "#5"])
        with mock.patch("agentboard.links.os.getcwd", return_value="/no/such/dir"):
            with mock.patch("agentboard.links.resolve_session_from_env", return_value=None):
                with mock.patch("agentboard.links.resolve_session_from_ancestors", return_value=None):
                    result = cli.cmd_link(args)
        self.assertEqual(result, 0)


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

    # -- _codex_hooks_feature_enabled ----------------------------------------

    def test_feature_flag_missing_file_is_unknown(self):
        self.assertIsNone(cli._codex_hooks_feature_enabled(self.config_path))

    def test_feature_flag_true(self):
        self.config_path.write_text("[features]\nhooks = true\n")
        self.assertIs(cli._codex_hooks_feature_enabled(self.config_path), True)

    def test_feature_flag_false(self):
        self.config_path.write_text("[features]\nhooks = false\n")
        self.assertIs(cli._codex_hooks_feature_enabled(self.config_path), False)

    def test_feature_flag_section_present_without_the_key(self):
        self.config_path.write_text("[features]\njs_repl = true\n")
        self.assertIs(cli._codex_hooks_feature_enabled(self.config_path), False)

    def test_feature_flag_no_features_section_at_all(self):
        self.config_path.write_text('model = "gpt-5"\n')
        self.assertIs(cli._codex_hooks_feature_enabled(self.config_path), False)

    def test_feature_flag_features_defined_as_a_non_table_does_not_crash(self):
        self.config_path.write_text('features = "on"\n')
        self.assertIs(cli._codex_hooks_feature_enabled(self.config_path), False)

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
        self._assert_outcome("not_found")

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
        fake.assert_called_once_with(["open", "-a", "Warp"], capture_output=True, text=True)
        line = self._assert_outcome("activated_only")
        self.assertIn("strategy=open_a", line)
        self.assertIn("open_rc=0", line)

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
        fake.assert_called_once_with(["open", "-a", "Warp"], capture_output=True, text=True)


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


if __name__ == "__main__":
    import unittest

    unittest.main()
