import ast
import io
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentboard import hook, reconcile
from tests.helpers import AgentBoardTestCase


def _no_shellouts(*args, **kwargs):
    raise AssertionError("subprocess.run should not be called here")


class HookProcessPayloadTests(AgentBoardTestCase):
    def _session_row(self, tool="claude", session_id="s1"):
        return self.conn.execute(
            "SELECT * FROM sessions WHERE tool=? AND session_id=?", (tool, session_id)
        ).fetchone()

    def test_session_start_creates_session(self):
        with mock.patch("agentboard.hook.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="main\n")
            hook.process_payload(
                self.conn, "claude",
                {"session_id": "s1", "cwd": "/tmp/proj", "hook_event_name": "SessionStart"},
            )
        row = self._session_row()
        self.assertIsNotNone(row)
        self.assertEqual(row["cwd"], "/tmp/proj")
        self.assertEqual(row["last_event"], "SessionStart")
        self.assertEqual(row["origin"], "hook")

    def test_git_branch_shellout_only_on_boundary_events(self):
        with mock.patch("agentboard.hook.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="feature-x\n")
            hook.process_payload(
                self.conn, "claude",
                {"session_id": "s1", "cwd": "/tmp/proj", "hook_event_name": "SessionStart"},
            )
        self.assertEqual(self._session_row()["branch"], "feature-x")

        # Steady-state PreToolUse must not shell out to git or ps again
        # (branch and pid are already known).
        with mock.patch("agentboard.hook.subprocess.run", side_effect=_no_shellouts):
            hook.process_payload(
                self.conn, "claude",
                {
                    "session_id": "s1", "cwd": "/tmp/proj",
                    "hook_event_name": "PreToolUse", "tool_name": "Bash",
                },
            )
        self.assertEqual(self._session_row()["last_event"], "PreToolUse")

    def test_branch_refreshed_on_stop_and_session_end(self):
        # A branch created mid-turn is seen once the session goes idle.
        with mock.patch("agentboard.hook.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="main\n")
            hook.process_payload(
                self.conn, "claude",
                {"session_id": "s1", "cwd": "/tmp/proj", "hook_event_name": "UserPromptSubmit",
                 "prompt": "open a PR"},
            )
        self.assertEqual(self._session_row()["branch"], "main")
        with mock.patch("agentboard.hook.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="feature-new\n")
            hook.process_payload(
                self.conn, "claude",
                {"session_id": "s1", "cwd": "/tmp/proj", "hook_event_name": "PreToolUse",
                 "tool_name": "Bash"},
            )
            self.assertEqual(self._session_row()["branch"], "main", "no git call mid-turn")
            hook.process_payload(
                self.conn, "claude",
                {"session_id": "s1", "cwd": "/tmp/proj", "hook_event_name": "Stop",
                 "last_assistant_message": "PR opened"},
            )
        self.assertEqual(self._session_row()["branch"], "feature-new")
        with mock.patch("agentboard.hook.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="feature-last\n")
            hook.process_payload(
                self.conn, "claude",
                {"session_id": "s1", "cwd": "/tmp/proj", "hook_event_name": "SessionEnd",
                 "reason": "prompt_input_exit"},
            )
        self.assertEqual(self._session_row()["branch"], "feature-last")

    def test_branch_read_from_head_file_without_running_git(self):
        repo = self.tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/feature/from-head\n")
        (repo / "sub").mkdir()
        with mock.patch("agentboard.hook.subprocess.run", side_effect=_no_shellouts) as run:
            self.assertEqual(hook._git_branch(str(repo / "sub")), "feature/from-head")
            (repo / ".git" / "HEAD").write_text("0123456789abcdef0123456789abcdef01234567\n")
            self.assertIsNone(hook._git_branch(str(repo)), "detached HEAD is no branch")
        run.assert_not_called()

    def test_branch_read_through_a_worktree_gitdir_file(self):
        common = self.tmp_path / "main-repo" / ".git" / "worktrees" / "wt"
        common.mkdir(parents=True)
        (common / "HEAD").write_text("ref: refs/heads/wt-branch\n")
        worktree = self.tmp_path / "wt"
        worktree.mkdir()
        (worktree / ".git").write_text(f"gitdir: {common}\n")
        with mock.patch("agentboard.hook.subprocess.run", side_effect=_no_shellouts):
            self.assertEqual(hook._git_branch(str(worktree)), "wt-branch")

    def test_branch_read_through_a_relative_gitdir_file(self):
        # What git writes for a submodule: a path relative to the directory
        # holding the `.git` file.
        modules = self.tmp_path / "super" / ".git" / "modules" / "sub"
        modules.mkdir(parents=True)
        (modules / "HEAD").write_text("ref: refs/heads/sub-branch\n")
        sub = self.tmp_path / "super" / "sub"
        (sub / "src").mkdir(parents=True)
        (sub / ".git").write_text("gitdir: ../.git/modules/sub\n")
        with mock.patch("agentboard.hook.subprocess.run", side_effect=_no_shellouts):
            self.assertEqual(hook._head_file_branch(str(sub / "src")), (True, "sub-branch"))
            self.assertEqual(hook._git_branch(str(sub / "src")), "sub-branch")

    def test_dot_git_file_without_gitdir_falls_back_to_git(self):
        repo = self.tmp_path / "odd"
        repo.mkdir()
        (repo / ".git").write_text("not a gitdir pointer\n")
        self.assertEqual(hook._head_file_branch(str(repo)), (False, None))
        with mock.patch("agentboard.hook.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="from-git\n")
            self.assertEqual(hook._git_branch(str(repo)), "from-git")
        self.assertEqual(run.call_args.args[0][:2], ["git", "-C"])

    def test_unknown_head_form_falls_back_to_git(self):
        repo = self.tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/.invalid\n")
        with mock.patch("agentboard.hook.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="reftable-branch\n")
            self.assertEqual(hook._git_branch(str(repo)), "reftable-branch")
        self.assertEqual(run.call_args.args[0][:2], ["git", "-C"])

    def test_subagent_stop_does_not_reread_the_branch(self):
        with mock.patch("agentboard.hook.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="feature-x\n")
            hook.process_payload(
                self.conn, "claude",
                {"session_id": "s1", "cwd": "/tmp/proj", "hook_event_name": "SessionStart"},
            )
        with mock.patch("agentboard.hook.subprocess.run") as run:
            hook.process_payload(
                self.conn, "claude",
                {"session_id": "s1", "cwd": "/tmp/proj", "hook_event_name": "Stop",
                 "agent_id": "sub-1"},
            )
        git_calls = [c for c in run.call_args_list if c.args and c.args[0][0] == "git"]
        self.assertEqual(git_calls, [])

    def test_a_failed_git_read_keeps_the_known_branch(self):
        with mock.patch("agentboard.hook.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="feature-x\n")
            hook.process_payload(
                self.conn, "claude",
                {"session_id": "s1", "cwd": "/tmp/proj", "hook_event_name": "SessionStart"},
            )
        with mock.patch("agentboard.hook.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=128, stdout="")
            hook.process_payload(
                self.conn, "claude",
                {"session_id": "s1", "cwd": "/gone", "hook_event_name": "SessionEnd",
                 "reason": "prompt_input_exit"},
            )
        self.assertEqual(self._session_row()["branch"], "feature-x")

    def test_prompt_and_message_truncated(self):
        long_prompt = "x" * 5000
        with mock.patch("agentboard.hook.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="")
            hook.process_payload(
                self.conn, "claude",
                {
                    "session_id": "s1", "cwd": "/tmp",
                    "hook_event_name": "UserPromptSubmit", "prompt": long_prompt,
                },
            )
        row = self._session_row()
        self.assertEqual(len(row["last_prompt"]), hook.MAX_TEXT_CHARS)
        self.assertEqual(len(row["first_prompt"]), hook.MAX_TEXT_CHARS)

    def test_local_command_user_prompt_submit_not_recorded_as_prompt(self):
        with mock.patch("agentboard.hook.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="")
            hook.process_payload(
                self.conn, "claude",
                {"session_id": "s1", "cwd": "/tmp", "hook_event_name": "SessionStart"},
            )
        with mock.patch("agentboard.hook.subprocess.run", side_effect=_no_shellouts):
            hook.process_payload(
                self.conn, "claude",
                {
                    "session_id": "s1", "cwd": "/tmp", "hook_event_name": "UserPromptSubmit",
                    "prompt": (
                        "<command-name>/model</command-name>\n"
                        "<command-message>model</command-message>\n"
                        "<command-args>opus</command-args>"
                    ),
                },
            )
        row = self._session_row()
        self.assertIsNone(row["first_prompt"])
        self.assertIsNone(row["last_prompt"])
        # the event itself still happened and still counts as activity
        self.assertEqual(row["last_event"], "UserPromptSubmit")

    def test_skill_command_user_prompt_submit_becomes_clean_prompt(self):
        with mock.patch("agentboard.hook.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="")
            hook.process_payload(
                self.conn, "claude",
                {"session_id": "s1", "cwd": "/tmp", "hook_event_name": "SessionStart"},
            )
        with mock.patch("agentboard.hook.subprocess.run", side_effect=_no_shellouts):
            hook.process_payload(
                self.conn, "claude",
                {
                    "session_id": "s1", "cwd": "/tmp", "hook_event_name": "UserPromptSubmit",
                    "prompt": (
                        "<command-message>review-pr</command-message>\n"
                        "<command-name>/review-pr</command-name>\n"
                        "<command-args>ABC-123 fix the crash</command-args>"
                    ),
                },
            )
        row = self._session_row()
        self.assertEqual(row["first_prompt"], "/review-pr ABC-123 fix the crash")
        self.assertEqual(row["last_prompt"], "/review-pr ABC-123 fix the crash")

    def test_local_command_does_not_clobber_earlier_real_prompt(self):
        with mock.patch("agentboard.hook.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="")
            hook.process_payload(
                self.conn, "claude",
                {
                    "session_id": "s1", "cwd": "/tmp", "hook_event_name": "UserPromptSubmit",
                    "prompt": "do the real thing",
                },
            )
        with mock.patch("agentboard.hook.subprocess.run", side_effect=_no_shellouts):
            hook.process_payload(
                self.conn, "claude",
                {
                    "session_id": "s1", "cwd": "/tmp", "hook_event_name": "UserPromptSubmit",
                    "prompt": "<command-name>/clear</command-name><command-args></command-args>",
                },
            )
        row = self._session_row()
        self.assertEqual(row["first_prompt"], "do the real thing")
        self.assertEqual(row["last_prompt"], "do the real thing")

    def test_git_branch_head_normalized_to_none_in_session(self):
        with mock.patch("agentboard.hook.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="HEAD\n")
            hook.process_payload(
                self.conn, "claude",
                {"session_id": "s1", "cwd": "/tmp", "hook_event_name": "SessionStart"},
            )
        row = self._session_row()
        self.assertIsNone(row["branch"])

    def test_session_end_records_end_reason_and_ended_at(self):
        with mock.patch("agentboard.hook.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="")
            hook.process_payload(
                self.conn, "claude",
                {"session_id": "s1", "cwd": "/tmp", "hook_event_name": "SessionStart"},
            )
        with mock.patch("agentboard.hook.subprocess.run", side_effect=_no_shellouts):
            hook.process_payload(
                self.conn, "claude",
                {
                    "session_id": "s1", "cwd": "/tmp",
                    "hook_event_name": "SessionEnd", "reason": "prompt_input_exit",
                },
            )
        row = self._session_row()
        self.assertEqual(row["end_reason"], "prompt_input_exit")
        self.assertIsNotNone(row["ended_at"])

    def test_resume_after_session_end_clears_ended_at(self):
        with mock.patch("agentboard.hook.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="")
            hook.process_payload(
                self.conn, "claude",
                {"session_id": "s1", "cwd": "/tmp", "hook_event_name": "SessionStart"},
            )
            hook.process_payload(
                self.conn, "claude",
                {
                    "session_id": "s1", "cwd": "/tmp",
                    "hook_event_name": "SessionEnd", "reason": "prompt_input_exit",
                },
            )
            row = self._session_row()
            self.assertIsNotNone(row["ended_at"])

            hook.process_payload(
                self.conn, "claude",
                {
                    "session_id": "s1", "cwd": "/tmp",
                    "hook_event_name": "UserPromptSubmit", "prompt": "one more thing",
                },
            )
        row = self._session_row()
        self.assertIsNone(row["ended_at"])
        self.assertIsNone(row["end_reason"])

    def test_subagent_session_end_does_not_count_as_resume(self):
        with mock.patch("agentboard.hook.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="")
            hook.process_payload(
                self.conn, "claude",
                {"session_id": "s1", "cwd": "/tmp", "hook_event_name": "SessionStart"},
            )
            hook.process_payload(
                self.conn, "claude",
                {
                    "session_id": "s1", "cwd": "/tmp",
                    "hook_event_name": "SessionEnd", "reason": "logout",
                },
            )
        with mock.patch("agentboard.hook.subprocess.run", side_effect=_no_shellouts):
            hook.process_payload(
                self.conn, "claude",
                {
                    "session_id": "s1", "cwd": "/tmp", "agent_id": "sub-1",
                    "hook_event_name": "UserPromptSubmit", "prompt": "subagent turn",
                },
            )
        row = self._session_row()
        self.assertIsNotNone(row["ended_at"])
        self.assertEqual(row["end_reason"], "logout")

    def test_todowrite_sets_todos_open(self):
        with mock.patch("agentboard.hook.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="")
            hook.process_payload(
                self.conn, "claude",
                {"session_id": "s1", "cwd": "/tmp", "hook_event_name": "SessionStart"},
            )
        with mock.patch("agentboard.hook.subprocess.run", side_effect=_no_shellouts):
            hook.process_payload(
                self.conn, "claude",
                {
                    "session_id": "s1", "cwd": "/tmp", "hook_event_name": "PostToolUse",
                    "tool_name": "TodoWrite",
                    "tool_input": {"todos": [
                        {"content": "a", "status": "completed"},
                        {"content": "b", "status": "in_progress"},
                        {"content": "c", "status": "pending"},
                    ]},
                },
            )
        self.assertEqual(self._session_row()["todos_open"], 2)

    def test_taskcreate_sets_todos_open_to_task_count(self):
        with mock.patch("agentboard.hook.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="")
            hook.process_payload(
                self.conn, "claude",
                {"session_id": "s1", "cwd": "/tmp", "hook_event_name": "SessionStart"},
            )
        with mock.patch("agentboard.hook.subprocess.run", side_effect=_no_shellouts):
            hook.process_payload(
                self.conn, "claude",
                {
                    "session_id": "s1", "cwd": "/tmp", "hook_event_name": "PostToolUse",
                    "tool_name": "TaskCreate",
                    "tool_input": {"tasks": json.dumps([{"subject": "a"}, {"subject": "b"}])},
                },
            )
        self.assertEqual(self._session_row()["todos_open"], 2)

    def test_todos_open_untouched_by_unrelated_posttooluse(self):
        with mock.patch("agentboard.hook.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="")
            hook.process_payload(
                self.conn, "claude",
                {"session_id": "s1", "cwd": "/tmp", "hook_event_name": "SessionStart"},
            )
        with mock.patch("agentboard.hook.subprocess.run", side_effect=_no_shellouts):
            hook.process_payload(
                self.conn, "claude",
                {
                    "session_id": "s1", "cwd": "/tmp", "hook_event_name": "PostToolUse",
                    "tool_name": "TodoWrite",
                    "tool_input": {"todos": [{"content": "a", "status": "pending"}]},
                },
            )
            hook.process_payload(
                self.conn, "claude",
                {
                    "session_id": "s1", "cwd": "/tmp", "hook_event_name": "PostToolUse",
                    "tool_name": "Bash",
                },
            )
        self.assertEqual(self._session_row()["todos_open"], 1)

    def test_permission_request_sets_needs_you_fields(self):
        with mock.patch("agentboard.hook.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="")
            hook.process_payload(
                self.conn, "claude",
                {"session_id": "s1", "cwd": "/tmp", "hook_event_name": "SessionStart"},
            )
        with mock.patch("agentboard.hook.subprocess.run", side_effect=_no_shellouts):
            hook.process_payload(
                self.conn, "claude",
                {
                    "session_id": "s1", "cwd": "/tmp",
                    "hook_event_name": "PermissionRequest", "tool_name": "Bash",
                },
            )
        row = self._session_row()
        self.assertEqual(row["last_event"], "PermissionRequest")
        self.assertEqual(row["waiting_reason"], "Bash")

    def test_subagent_event_does_not_overwrite_parent_last_event(self):
        with mock.patch("agentboard.hook.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="")
            hook.process_payload(
                self.conn, "claude",
                {
                    "session_id": "s1", "cwd": "/tmp",
                    "hook_event_name": "PermissionRequest", "tool_name": "Bash",
                },
            )
        # A subagent's own Stop event (tagged with agent_id) must not clear
        # the parent's needs_you signal.
        with mock.patch("agentboard.hook.subprocess.run", side_effect=_no_shellouts):
            hook.process_payload(
                self.conn, "claude",
                {
                    "session_id": "s1", "cwd": "/tmp", "agent_id": "sub-1",
                    "hook_event_name": "Stop", "last_assistant_message": "done",
                },
            )
        row = self._session_row()
        self.assertEqual(row["last_event"], "PermissionRequest")
        self.assertEqual(row["waiting_reason"], "Bash")

    def test_subagent_marker_events_do_update_last_event(self):
        with mock.patch("agentboard.hook.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="")
            hook.process_payload(
                self.conn, "claude",
                {"session_id": "s1", "cwd": "/tmp", "hook_event_name": "SessionStart"},
            )
        with mock.patch("agentboard.hook.subprocess.run", side_effect=_no_shellouts):
            hook.process_payload(
                self.conn, "claude",
                {
                    "session_id": "s1", "cwd": "/tmp", "agent_id": "sub-1",
                    "hook_event_name": "SubagentStart",
                },
            )
        self.assertEqual(self._session_row()["last_event"], "SubagentStart")

    def test_reopen_after_done(self):
        with mock.patch("agentboard.hook.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="")
            hook.process_payload(
                self.conn, "claude",
                {"session_id": "s1", "cwd": "/tmp", "hook_event_name": "SessionStart"},
            )
        self.conn.execute(
            "INSERT INTO triage(tool, session_id, status, until, note, updated_at) "
            "VALUES ('claude','s1','done',NULL,NULL,'2026-01-01T00:00:00.000000Z')"
        )
        with mock.patch("agentboard.hook.subprocess.run", side_effect=_no_shellouts):
            hook.process_payload(
                self.conn, "claude",
                {
                    "session_id": "s1", "cwd": "/tmp",
                    "hook_event_name": "UserPromptSubmit", "prompt": "one more thing",
                },
            )
        triage = self.conn.execute(
            "SELECT * FROM triage WHERE tool='claude' AND session_id='s1'"
        ).fetchone()
        self.assertEqual(triage["status"], "open")

    def test_subagent_user_prompt_submit_does_not_reopen_triage(self):
        with mock.patch("agentboard.hook.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="")
            hook.process_payload(
                self.conn, "claude",
                {"session_id": "s1", "cwd": "/tmp", "hook_event_name": "SessionStart"},
            )
        self.conn.execute(
            "INSERT INTO triage(tool, session_id, status, until, note, updated_at) "
            "VALUES ('claude','s1','done',NULL,NULL,'2026-01-01T00:00:00.000000Z')"
        )
        with mock.patch("agentboard.hook.subprocess.run", side_effect=_no_shellouts):
            hook.process_payload(
                self.conn, "claude",
                {
                    "session_id": "s1", "cwd": "/tmp", "agent_id": "sub-1",
                    "hook_event_name": "UserPromptSubmit", "prompt": "subagent turn",
                },
            )
        triage = self.conn.execute(
            "SELECT * FROM triage WHERE tool='claude' AND session_id='s1'"
        ).fetchone()
        self.assertEqual(triage["status"], "done")

    def test_daemon_pid_is_not_stored_or_trusted_for_terminal(self):
        def fake_run(cmd, **kwargs):
            if cmd[0] == "git":
                return mock.Mock(returncode=0, stdout="")
            table = {1000: (40007, "ttys003", "sh"), 40007: (1, "??", "codex")}
            entry = table.get(int(cmd[-1]))
            if entry is None:
                return mock.Mock(stdout="")
            ppid, tty, comm = entry
            return mock.Mock(stdout=f"{ppid} {tty} {comm}\n")

        with mock.patch("agentboard.hook.subprocess.run", side_effect=fake_run), \
             mock.patch("agentboard.hook.os.getppid", return_value=1000), \
             mock.patch.dict(
                 "agentboard.hook.os.environ",
                 {"ITERM_SESSION_ID": "w0t1p0:REAL-TUI-UUID", "TERM_PROGRAM": "iTerm.app"},
             ):
            hook.process_payload(
                self.conn, "codex",
                {"session_id": "s1", "cwd": "/tmp", "hook_event_name": "SessionStart"},
            )
        row = self._session_row(tool="codex")
        self.assertIsNone(row["pid"])
        self.assertIsNone(row["tty"])
        self.assertIsNone(row["iterm_session_id"])
        self.assertIsNone(row["term_program"])

    def test_daemon_detection_clears_previously_stored_bad_values(self):
        self.conn.execute(
            "INSERT INTO sessions(tool, session_id, pid, tty, iterm_session_id, "
            "term_program, origin, updated_at) "
            "VALUES ('codex','s1',40007,'??','w0t1p0:OLD','iTerm.app','hook','2026-01-01T00:00:00.000000Z')"
        )

        def fake_run(cmd, **kwargs):
            if cmd[0] == "git":
                return mock.Mock(returncode=0, stdout="")
            table = {1000: (40007, "ttys003", "sh"), 40007: (1, "??", "codex")}
            entry = table.get(int(cmd[-1]))
            if entry is None:
                return mock.Mock(stdout="")
            ppid, tty, comm = entry
            return mock.Mock(stdout=f"{ppid} {tty} {comm}\n")

        with mock.patch("agentboard.hook.subprocess.run", side_effect=fake_run), \
             mock.patch("agentboard.hook.os.getppid", return_value=1000):
            hook.process_payload(
                self.conn, "codex",
                {
                    "session_id": "s1", "cwd": "/tmp",
                    "hook_event_name": "UserPromptSubmit", "prompt": "keep going",
                },
            )
        row = self._session_row(tool="codex")
        self.assertIsNone(row["pid"])
        self.assertIsNone(row["tty"])
        self.assertIsNone(row["iterm_session_id"])
        self.assertIsNone(row["term_program"])

    def test_irrelevant_notification_does_not_change_last_event(self):
        with mock.patch("agentboard.hook.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="")
            hook.process_payload(
                self.conn, "claude",
                {
                    "session_id": "s1", "cwd": "/tmp", "hook_event_name": "Stop",
                    "last_assistant_message": "done",
                },
            )
        with mock.patch("agentboard.hook.subprocess.run", side_effect=_no_shellouts):
            hook.process_payload(
                self.conn, "claude",
                {
                    "session_id": "s1", "cwd": "/tmp", "hook_event_name": "Notification",
                    "notification_type": "agent_completed", "message": "all done",
                },
            )
        row = self._session_row()
        self.assertEqual(row["last_event"], "Stop")

    def test_relevant_notification_still_updates_last_event(self):
        with mock.patch("agentboard.hook.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="")
            hook.process_payload(
                self.conn, "claude",
                {"session_id": "s1", "cwd": "/tmp", "hook_event_name": "SessionStart"},
            )
        with mock.patch("agentboard.hook.subprocess.run", side_effect=_no_shellouts):
            hook.process_payload(
                self.conn, "claude",
                {
                    "session_id": "s1", "cwd": "/tmp", "hook_event_name": "Notification",
                    "notification_type": "permission_prompt", "message": "allow this?",
                },
            )
        row = self._session_row()
        self.assertEqual(row["last_event"], "Notification")
        self.assertEqual(row["waiting_reason"], "allow this?")

    def test_ask_user_question_pretooluse_is_needs_you(self):
        with mock.patch("agentboard.hook.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="")
            hook.process_payload(
                self.conn, "claude",
                {"session_id": "s1", "cwd": "/tmp", "hook_event_name": "SessionStart"},
            )
        with mock.patch("agentboard.hook.subprocess.run", side_effect=_no_shellouts):
            hook.process_payload(
                self.conn, "claude",
                {
                    "session_id": "s1", "cwd": "/tmp",
                    "hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
                },
            )
        row = self._session_row()
        self.assertEqual(row["last_event"], hook.NEEDS_YOU_TOOL_SENTINEL)
        self.assertEqual(row["waiting_reason"], "question")

    def test_exit_plan_mode_pretooluse_is_needs_you(self):
        with mock.patch("agentboard.hook.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="")
            hook.process_payload(
                self.conn, "claude",
                {"session_id": "s1", "cwd": "/tmp", "hook_event_name": "SessionStart"},
            )
        with mock.patch("agentboard.hook.subprocess.run", side_effect=_no_shellouts):
            hook.process_payload(
                self.conn, "claude",
                {
                    "session_id": "s1", "cwd": "/tmp",
                    "hook_event_name": "PreToolUse", "tool_name": "ExitPlanMode",
                },
            )
        row = self._session_row()
        self.assertEqual(row["last_event"], hook.NEEDS_YOU_TOOL_SENTINEL)
        self.assertEqual(row["waiting_reason"], "plan approval")

    def test_needs_you_tool_cleared_by_post_tool_use(self):
        with mock.patch("agentboard.hook.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="")
            hook.process_payload(
                self.conn, "claude",
                {"session_id": "s1", "cwd": "/tmp", "hook_event_name": "SessionStart"},
            )
        with mock.patch("agentboard.hook.subprocess.run", side_effect=_no_shellouts):
            hook.process_payload(
                self.conn, "claude",
                {
                    "session_id": "s1", "cwd": "/tmp",
                    "hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
                },
            )
            hook.process_payload(
                self.conn, "claude",
                {
                    "session_id": "s1", "cwd": "/tmp",
                    "hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion",
                },
            )
        row = self._session_row()
        self.assertEqual(row["last_event"], "PostToolUse")

    def test_needs_you_tool_cleared_by_user_prompt_submit(self):
        with mock.patch("agentboard.hook.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="")
            hook.process_payload(
                self.conn, "claude",
                {"session_id": "s1", "cwd": "/tmp", "hook_event_name": "SessionStart"},
            )
        with mock.patch("agentboard.hook.subprocess.run", side_effect=_no_shellouts):
            hook.process_payload(
                self.conn, "claude",
                {
                    "session_id": "s1", "cwd": "/tmp",
                    "hook_event_name": "PreToolUse", "tool_name": "ExitPlanMode",
                },
            )
            hook.process_payload(
                self.conn, "claude",
                {
                    "session_id": "s1", "cwd": "/tmp",
                    "hook_event_name": "UserPromptSubmit", "prompt": "go ahead",
                },
            )
        row = self._session_row()
        self.assertEqual(row["last_event"], "UserPromptSubmit")

    # -- finished turns, background tasks, compaction (state accuracy) ----------

    _clock = 0

    def _at(self, event, **fields):
        """Send one event at a distinct, increasing timestamp; returns it."""
        HookProcessPayloadTests._clock += 1
        ts = f"2026-10-24T05:{HookProcessPayloadTests._clock // 60 % 60:02d}:" \
             f"{HookProcessPayloadTests._clock % 60:02d}.000000Z"
        payload = {"session_id": "s1", "cwd": "/tmp", "hook_event_name": event, **fields}
        with mock.patch("agentboard.hook.subprocess.run") as run, \
             mock.patch("agentboard.hook.subprocess.Popen"), \
             mock.patch("agentboard.hook._now_iso", return_value=ts):
            run.return_value = mock.Mock(returncode=0, stdout="")
            hook.process_payload(self.conn, "claude", payload)
        return ts

    def test_tagged_subagent_stop_keeps_a_finished_turn(self):
        self._at("UserPromptSubmit", prompt="run the review")
        stop_ts = self._at("Stop", last_assistant_message="Review is running in the background.")
        self._at("Notification", notification_type="idle_prompt", message="waiting")
        late = self._at("SubagentStop", agent_id="bg-1")
        row = self._session_row()
        self.assertEqual(row["last_event"], "Notification")
        self.assertNotEqual(row["last_event_at"], late)
        self.assertEqual(row["updated_at"], late)
        # The same with the Stop itself as the kept event.
        self.conn.execute("DELETE FROM sessions")
        self._at("UserPromptSubmit", prompt="again")
        stop_ts = self._at("Stop", last_assistant_message="done")
        late = self._at("SubagentStop", agent_id="bg-2")
        row = self._session_row()
        self.assertEqual(row["last_event"], "Stop")
        self.assertEqual(row["last_event_at"], stop_ts)
        self.assertEqual(row["updated_at"], late)

    def test_tagged_subagent_stop_keeps_needs_you(self):
        self._at("PermissionRequest", tool_name="Bash")
        self._at("SubagentStart", agent_id="bg-1")
        self._at("SubagentStop", agent_id="bg-1")
        row = self._session_row()
        self.assertEqual(row["last_event"], "PermissionRequest")
        self.assertEqual(row["waiting_reason"], "Bash")

    def test_kept_finished_turn_keeps_last_event_at(self):
        self._at("UserPromptSubmit", prompt="go")
        stop_ts = self._at("Stop", last_assistant_message="done")
        late = self._at("PostToolUse", agent_id="bg-1", tool_name="Bash")
        row = self._session_row()
        self.assertEqual(row["last_event"], "Stop")
        self.assertEqual(row["last_event_at"], stop_ts)
        self.assertEqual(row["updated_at"], late)

    def test_tagged_tool_event_mid_turn_still_refreshes_last_event_at(self):
        self._at("UserPromptSubmit", prompt="go")
        self._at("PreToolUse", tool_name="Agent")
        late = self._at("PostToolUse", agent_id="sub-1", tool_name="Bash")
        row = self._session_row()
        self.assertEqual(row["last_event"], "PreToolUse")
        self.assertEqual(row["last_event_at"], late)

    def test_tagged_marker_mid_turn_still_becomes_last_event(self):
        self._at("UserPromptSubmit", prompt="go")
        late = self._at("SubagentStart", agent_id="sub-1")
        row = self._session_row()
        self.assertEqual(row["last_event"], "SubagentStart")
        self.assertEqual(row["last_event_at"], late)

    def test_stop_records_background_tasks_and_crons(self):
        self._at("UserPromptSubmit", prompt="go")
        self._at(
            "Stop", last_assistant_message="waiting",
            background_tasks=[
                {"id": "a", "type": "subagent", "status": "running", "description": "x"},
                {"id": "b", "type": "shell", "status": "running"},
                {"id": "c", "type": "subagent", "status": "running"},
            ],
            session_crons=[
                {"id": "c1", "schedule": "*/5 * * * *", "recurring": True,
                 "prompt": "check   the\nqueue " + "x" * 300},
                {"id": "c2", "schedule": "0 * * * *", "recurring": True, "prompt": "tick"},
            ],
        )
        row = self._session_row()
        self.assertEqual(row["background_tasks"], 3)
        self.assertEqual(json.loads(row["background_types"]), ["subagent", "shell", "subagent"])
        self.assertEqual(row["session_crons"], 2)
        prompts = json.loads(row["cron_prompts"])
        self.assertEqual(prompts[1], "tick")
        self.assertEqual(len(prompts[0]), hook.CRON_PROMPT_CHARS)
        self.assertTrue(prompts[0].startswith("check the queue xxx"))
        extra = json.loads(self.conn.execute(
            "SELECT extra FROM events WHERE event='Stop'").fetchone()[0])
        self.assertEqual((extra["background_tasks"], extra["session_crons"]), (3, 2))
        # A Stop without the arrays (older Claude Code) clears them to unknown.
        self._at("UserPromptSubmit", prompt="more")
        self._at("Stop", last_assistant_message="done")
        row = self._session_row()
        for col in ("background_tasks", "background_types", "session_crons", "cron_prompts"):
            self.assertIsNone(row[col], col)
        # Empty arrays are a known zero.
        self._at("Stop", last_assistant_message="done", background_tasks=[], session_crons=[])
        row = self._session_row()
        self.assertEqual((row["background_tasks"], row["session_crons"]), (0, 0))

    def test_stop_ignores_completed_background_entries(self):
        self._at("UserPromptSubmit", prompt="go")
        self._at("Stop", last_assistant_message="waiting", background_tasks=[
            {"id": "a", "type": "shell", "status": "running"},
            {"id": "b", "type": "subagent", "status": "completed"},
            {"id": "c", "type": "monitor", "status": "Killed"},
            {"id": "d", "type": "workflow", "status": "paused-ish"},
        ])
        row = self._session_row()
        self.assertEqual(row["background_tasks"], 2)
        self.assertEqual(json.loads(row["background_types"]), ["shell", "workflow"])

    def test_tagged_subagent_stop_decrements_background_tasks(self):
        self._at("UserPromptSubmit", prompt="go")
        self._at("Stop", last_assistant_message="waiting", background_tasks=[
            {"id": "a", "type": "subagent", "status": "running"},
        ])
        self._at("SubagentStop", agent_id="bg-1")
        row = self._session_row()
        self.assertEqual(row["background_tasks"], 0)
        self.assertEqual(json.loads(row["background_types"]), [])
        # Never below zero.
        self._at("SubagentStop", agent_id="bg-2")
        self.assertEqual(self._session_row()["background_tasks"], 0)

    def test_tagged_subagent_stop_does_not_decrement_workflow_task(self):
        self._at("UserPromptSubmit", prompt="go")
        self._at("Stop", last_assistant_message="waiting", background_tasks=[
            {"id": "a", "type": "workflow", "status": "running"},
        ])
        self._at("SubagentStop", agent_id="bg-1")
        row = self._session_row()
        self.assertEqual(row["background_tasks"], 1)
        self.assertEqual(json.loads(row["background_types"]), ["workflow"])

    def test_compact_session_start_keeps_last_event(self):
        self._at("UserPromptSubmit", prompt="go")
        stop_ts = self._at("Stop", last_assistant_message="done")
        self._at("Notification", notification_type="idle_prompt", message="idle")
        self._at("SubagentStop", agent_id="bg-1")
        late = self._at("SessionStart", source="compact")
        row = self._session_row()
        self.assertEqual(row["last_event"], "Notification")
        self.assertNotEqual(row["last_event_at"], late)
        self.assertEqual(row["updated_at"], late)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM events WHERE event='SessionStart'").fetchone()[0], 1,
            "the event row is still recorded",
        )
        self.assertNotEqual(stop_ts, late)

    def test_mid_turn_compact_session_start_refreshes_last_event_at(self):
        self._at("UserPromptSubmit", prompt="go")
        self._at("PreToolUse", tool_name="Bash")
        late = self._at("SessionStart", source="compact")
        row = self._session_row()
        self.assertEqual(row["last_event"], "PreToolUse")
        self.assertEqual(row["last_event_at"], late)

    def test_idle_compaction_keeps_the_finished_turn(self):
        # A /compact typed at an idle prompt: Stop -> PreCompact ->
        # PostCompact -> SessionStart(compact) must still read as Your turn.
        self._at("UserPromptSubmit", prompt="go")
        stop_ts = self._at("Stop", last_assistant_message="All done.")
        self._at("PreCompact", trigger="manual")
        self._at("PostCompact", trigger="manual")
        late = self._at("SessionStart", source="compact")
        row = self._session_row()
        self.assertEqual(row["last_event"], "Stop")
        self.assertIsNone(row["waiting_reason"])
        self.assertEqual(row["last_event_at"], stop_ts)
        self.assertEqual(row["updated_at"], late)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM events WHERE event IN ('PreCompact', 'PostCompact')"
            ).fetchone()[0], 2,
            "the compaction event rows are still recorded",
        )
        now = datetime.fromisoformat(late.replace("Z", "+00:00")) + timedelta(minutes=1)
        state, reason, _auto_done, _done_reason = reconcile.compute_state(
            dict(row), None, [], now=now, alive=True
        )
        self.assertEqual(state, "review")
        self.assertTrue(reason.startswith("Your turn"), reason)

    def test_idle_compaction_keeps_needs_you(self):
        self._at("UserPromptSubmit", prompt="go")
        self._at("PermissionRequest", tool_name="Bash")
        before = self._session_row()
        self._at("PreCompact", trigger="auto")
        row = self._session_row()
        self.assertEqual(row["last_event"], "PermissionRequest")
        self.assertEqual(row["waiting_reason"], before["waiting_reason"])
        self.assertEqual(row["last_event_at"], before["last_event_at"])

    def test_mid_turn_compaction_refreshes_last_event_at(self):
        self._at("UserPromptSubmit", prompt="go")
        self._at("PreToolUse", tool_name="Bash")
        late = self._at("PreCompact", trigger="auto")
        row = self._session_row()
        self.assertEqual(row["last_event"], "PreCompact")
        self.assertEqual(row["last_event_at"], late)

    def test_resume_session_start_keeps_a_finished_turn(self):
        self._at("UserPromptSubmit", prompt="go")
        stop_ts = self._at("Stop", last_assistant_message="done")
        self._at("SessionStart", source="resume")
        row = self._session_row()
        self.assertEqual(row["last_event"], "Stop")
        self.assertEqual(row["last_event_at"], stop_ts)

    def test_continued_session_start_on_a_backfill_row_writes_session_start(self):
        # A pre-hooks session backfill recorded (no last_event yet) and then
        # resumed or compacted: there is no state to keep, so SessionStart
        # is recorded and the registry tie-breaker can resolve it.
        for source in ("compact", "resume"):
            with self.subTest(source=source):
                self.conn.execute("DELETE FROM sessions")
                self.conn.execute(
                    "INSERT INTO sessions(tool, session_id, origin, first_prompt, started_at, "
                    "updated_at) VALUES ('claude', 's1', 'backfill', 'old work', "
                    "'2026-10-20T00:00:00.000000Z', '2026-10-20T00:00:00.000000Z')"
                )
                late = self._at("SessionStart", source=source)
                row = self._session_row()
                self.assertEqual(row["last_event"], "SessionStart")
                self.assertEqual(row["last_event_at"], late)

    def test_resume_after_session_end_writes_session_start(self):
        self._at("UserPromptSubmit", prompt="go")
        self._at("Stop", last_assistant_message="done")
        self._at("SessionEnd", reason="prompt_input_exit")
        late = self._at("SessionStart", source="resume")
        row = self._session_row()
        self.assertEqual(row["last_event"], "SessionStart")
        self.assertEqual(row["last_event_at"], late)
        self.assertIsNone(row["ended_at"])

    def test_startup_session_start_still_resets(self):
        for source in ("startup", "clear", None):
            self._at("UserPromptSubmit", prompt="go")
            self._at("Stop", last_assistant_message="done")
            fields = {"source": source} if source else {}
            late = self._at("SessionStart", **fields)
            row = self._session_row()
            self.assertEqual(row["last_event"], "SessionStart", source)
            self.assertEqual(row["last_event_at"], late, source)

    def test_subagent_user_prompt_submit_does_not_set_prompts(self):
        self._at("SessionStart")
        self._at("UserPromptSubmit", agent_id="sub-1", prompt="subagent instructions")
        row = self._session_row()
        self.assertIsNone(row["first_prompt"])
        self.assertIsNone(row["last_prompt"])
        self._at("UserPromptSubmit", prompt="the real prompt")
        self._at("UserPromptSubmit", agent_id="sub-1", prompt="more subagent text")
        row = self._session_row()
        self.assertEqual((row["first_prompt"], row["last_prompt"]),
                         ("the real prompt", "the real prompt"))

    def test_injected_prompt_does_not_reopen_done(self):
        self._at("UserPromptSubmit", prompt="ship it")
        self._at("Stop", last_assistant_message="shipped")
        self.conn.execute(
            "INSERT INTO triage(tool, session_id, status, updated_at) "
            "VALUES ('claude', 's1', 'done', '2026-01-01T00:00:00.000000Z')"
        )
        self._at("UserPromptSubmit", prompt="<task-notification>bg done</task-notification>")
        status = lambda: self.conn.execute(  # noqa: E731
            "SELECT status FROM triage WHERE session_id='s1'").fetchone()[0]
        self.assertEqual(status(), "done")
        row = self._session_row()
        self.assertEqual((row["first_prompt"], row["last_prompt"]), ("ship it", "ship it"))
        # A real prompt still reopens it.
        self._at("UserPromptSubmit", prompt="one more thing")
        self.assertEqual(status(), "open")

    def test_injected_prompt_is_still_activity(self):
        self._at("UserPromptSubmit", prompt="go")
        self._at("Stop", last_assistant_message="done")
        ts = self._at("UserPromptSubmit", prompt='<agent-message from="x">hi</agent-message>')
        row = self._session_row()
        self.assertEqual((row["last_event"], row["last_event_at"]), ("UserPromptSubmit", ts))
        kind = self.conn.execute(
            "SELECT prompt_kind FROM events ORDER BY id DESC LIMIT 1").fetchone()[0]
        self.assertEqual(kind, "injected")

    def test_prompt_kind_failure_still_records_the_prompt(self):
        with mock.patch("agentboard.notes.prompt_kind", side_effect=RuntimeError("boom")):
            self._at("UserPromptSubmit", prompt="hello")
        event = self.conn.execute("SELECT text, prompt_kind FROM events").fetchone()
        self.assertEqual((event["text"], event["prompt_kind"]), ("hello", None))
        self.assertEqual(self._session_row()["last_prompt"], "hello")

    def test_stop_failure_records_documented_fields(self):
        self._at("UserPromptSubmit", prompt="go")
        self._at("StopFailure", error="rate_limit",
                 error_details="429 Too Many Requests",
                 last_assistant_message="API Error: Rate limit reached")
        event = self.conn.execute(
            "SELECT text, extra FROM events WHERE event='StopFailure'").fetchone()
        self.assertEqual(event["text"], "API Error: Rate limit reached")
        extra = json.loads(event["extra"])
        self.assertEqual((extra["error"], extra["error_details"]),
                         ("rate_limit", "429 Too Many Requests"))
        row = self._session_row()
        self.assertEqual(row["last_event"], "StopFailure")
        self.assertEqual(row["waiting_reason"], "rate_limit: API Error: Rate limit reached")
        notes = self.conn.execute("SELECT kind, text, source, count FROM notes").fetchall()
        self.assertEqual([tuple(n) for n in notes],
                         [("error", "rate_limit: API Error: Rate limit reached", "stop", 1)])
        # A repeat bumps the count instead of adding a note.
        self._at("StopFailure", error="rate_limit",
                 last_assistant_message="API Error: Rate limit reached")
        self.assertEqual(self.conn.execute("SELECT count FROM notes").fetchone()[0], 2)

    def test_stop_failure_text_falls_back_to_details_then_error(self):
        self._at("StopFailure", error="overloaded", error_details="upstream busy")
        self.assertEqual(self._session_row()["waiting_reason"], "overloaded: upstream busy")
        self._at("StopFailure", error="server_error")
        self.assertEqual(self._session_row()["waiting_reason"], "server_error")
        # The pre-documentation keys still work.
        self._at("StopFailure", error_message="legacy failure")
        self.assertEqual(self._session_row()["waiting_reason"], "legacy failure")

    def test_subagent_stop_failure_keeps_parent_state_but_records_reason(self):
        self._at("UserPromptSubmit", prompt="go")
        self._at("PreToolUse", tool_name="Agent")
        self._at("StopFailure", agent_id="sub-1", error="overloaded",
                 last_assistant_message="API Error: Overloaded")
        row = self._session_row()
        self.assertEqual(row["last_event"], "PreToolUse")
        event = self.conn.execute(
            "SELECT text, extra FROM events WHERE event='StopFailure'").fetchone()
        self.assertEqual(event["text"], "API Error: Overloaded")
        self.assertEqual(json.loads(event["extra"])["error"], "overloaded")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 0)

    def test_usage_limit_notifications(self):
        self._at("UserPromptSubmit", prompt="go")
        self._at("Notification", notification_type="quota_auto_resume_stale")
        row = self._session_row()
        self.assertEqual(row["last_event"], "Notification")
        self.assertEqual(row["waiting_reason"],
                         "Usage limit reset while asleep: press Enter to resume")
        self._at("Notification", notification_type="quota_auto_resume_disabled",
                 message="Auto-resume is off")
        self.assertEqual(self._session_row()["waiting_reason"], "Auto-resume is off")
        fired = self._at("Notification", notification_type="quota_auto_resume_fired",
                         message="Resuming")
        row = self._session_row()
        self.assertEqual((row["last_event"], row["waiting_reason"]), ("Notification", None))
        # After an auto-resume a background marker is activity, not a kept
        # finished turn: last_event_at keeps advancing.
        late = self._at("SubagentStart", agent_id="sub-1")
        row = self._session_row()
        self.assertEqual((row["last_event"], row["last_event_at"]), ("SubagentStart", late))
        self.assertNotEqual(fired, late)

    def test_prompt_less_session_end_marks_non_interactive(self):
        self._at("SessionStart")
        self._at("SessionEnd", reason="other")
        self.assertEqual(self._session_row()["interactive"], 0)
        # A prompted session keeps its interactive flag.
        self.conn.execute("DELETE FROM sessions")
        self.conn.execute("DELETE FROM events")
        self._at("SessionStart")
        self._at("UserPromptSubmit", prompt="hello")
        self._at("SessionEnd", reason="other")
        self.assertIsNone(self._session_row()["interactive"])
        # A subagent's SessionEnd marks nothing.
        self.conn.execute("DELETE FROM sessions")
        self.conn.execute("DELETE FROM events")
        self._at("SessionStart")
        self._at("SessionEnd", agent_id="sub-1", reason="other")
        self.assertIsNone(self._session_row()["interactive"])


def _fake_ps_chain(hops):
    """hops: ordered [(pid, ppid, tty, comm), ...] consumed by pid lookups."""
    table = {pid: (ppid, tty, comm) for pid, ppid, tty, comm in hops}

    def fake_run(cmd, **kwargs):
        pid = int(cmd[-1])
        entry = table.get(pid)
        if entry is None:
            return mock.Mock(stdout="")
        ppid, tty, comm = entry
        return mock.Mock(stdout=f"{ppid} {tty} {comm}\n")

    return fake_run


class HookTmuxIdentityTests(AgentBoardTestCase):
    SOCK = "/private/tmp/tmux-501/default"
    TMUX_ENV = {
        "TMUX": "/private/tmp/tmux-501/default,4242,0",
        "TMUX_PANE": "%3",
        "TERM_PROGRAM": "tmux",
        "ITERM_SESSION_ID": "w0t0p0:STALE",
    }

    def _row(self, tool="claude"):
        return self.conn.execute(
            "SELECT * FROM sessions WHERE tool=? AND session_id='s1'", (tool,)
        ).fetchone()

    def _fire(self, event, env, tool="claude", fake_run=None, **extra):
        payload = {"session_id": "s1", "cwd": "/tmp", "hook_event_name": event}
        payload.update(extra)
        if fake_run is None:
            def fake_run(cmd, **kwargs):
                return mock.Mock(returncode=0, stdout="")
        with mock.patch("agentboard.hook.subprocess.run", side_effect=fake_run), \
             mock.patch.dict("agentboard.hook.os.environ", env, clear=False):
            for key in ("TMUX", "TMUX_PANE", "TERM_PROGRAM"):
                if key not in env:
                    os.environ.pop(key, None)
            hook.process_payload(self.conn, tool, payload)

    def test_identity_parses_socket_and_pane(self):
        self.assertEqual(
            hook._tmux_identity(self.TMUX_ENV), (self.SOCK, "%3")
        )
        # A socket path with a comma keeps it: the split is from the right.
        self.assertEqual(
            hook._tmux_identity({"TMUX": "/tmp/a,b/sock,1,2", "TMUX_PANE": "%0"}),
            ("/tmp/a,b/sock", "%0"),
        )

    def test_identity_rejects_malformed_values(self):
        cases = [
            {},
            {"TMUX": "", "TMUX_PANE": "%1"},
            {"TMUX": "/tmp/sock,1,2"},
            {"TMUX": "/tmp/sock,1,2", "TMUX_PANE": "1"},
            {"TMUX": "/tmp/sock,1,2", "TMUX_PANE": "%1; rm -rf /"},
            {"TMUX": "relative/sock,1,2", "TMUX_PANE": "%1"},
            {"TMUX": "/tmp/sock,abc,2", "TMUX_PANE": "%1"},
            {"TMUX": "/tmp/sock,1", "TMUX_PANE": "%1"},
            {"TMUX": "/tmp/so\nck,1,2", "TMUX_PANE": "%1"},
        ]
        for env in cases:
            with self.subTest(env=env):
                self.assertEqual(hook._tmux_identity(env), (None, None))

    def test_tmux_env_is_recorded(self):
        self._fire("SessionStart", self.TMUX_ENV)
        row = self._row()
        self.assertEqual(row["tmux_socket"], self.SOCK)
        self.assertEqual(row["tmux_pane"], "%3")
        self.assertEqual(row["term_program"], "tmux")

    def test_malformed_tmux_env_is_ignored(self):
        env = dict(self.TMUX_ENV, TMUX="garbage")
        self._fire("SessionStart", env)
        row = self._row()
        self.assertIsNone(row["tmux_socket"])
        self.assertIsNone(row["tmux_pane"])

    def test_not_recorded_for_daemon_origin_event(self):
        def fake_run(cmd, **kwargs):
            if cmd[0] == "git":
                return mock.Mock(returncode=0, stdout="")
            table = {1000: (40007, "ttys003", "sh"), 40007: (1, "??", "codex")}
            entry = table.get(int(cmd[-1]))
            if entry is None:
                return mock.Mock(stdout="")
            ppid, tty, comm = entry
            return mock.Mock(stdout=f"{ppid} {tty} {comm}\n")

        self.conn.execute(
            "INSERT INTO sessions(tool, session_id, tmux_socket, tmux_pane, origin, "
            "updated_at) VALUES ('codex','s1','/tmp/old','%9','hook',"
            "'2026-01-01T00:00:00.000000Z')"
        )
        with mock.patch("agentboard.hook.os.getppid", return_value=1000):
            self._fire("SessionStart", self.TMUX_ENV, tool="codex", fake_run=fake_run)
        row = self._row(tool="codex")
        self.assertIsNone(row["tmux_socket"])
        self.assertIsNone(row["tmux_pane"])

    def test_later_event_without_vars_keeps_stored_values(self):
        self._fire("SessionStart", self.TMUX_ENV)
        self._fire("PreToolUse", {}, tool_name="Bash")
        row = self._row()
        self.assertEqual(row["tmux_socket"], self.SOCK)
        self.assertEqual(row["tmux_pane"], "%3")

    def test_event_from_a_plain_terminal_clears_tmux_columns(self):
        self._fire("SessionStart", self.TMUX_ENV)
        self._fire(
            "UserPromptSubmit",
            {"TERM_PROGRAM": "iTerm.app", "ITERM_SESSION_ID": "w0t1p0:NEW"},
            prompt="resumed in a plain tab",
        )
        row = self._row()
        self.assertIsNone(row["tmux_socket"])
        self.assertIsNone(row["tmux_pane"])
        self.assertEqual(row["term_program"], "iTerm.app")

    def test_tmux_term_program_without_tmux_vars_keeps_stored_values(self):
        # TERM_PROGRAM=tmux with $TMUX/$TMUX_PANE scrubbed is still inside
        # tmux: only a terminal other than tmux means the session left it.
        self._fire("SessionStart", self.TMUX_ENV)
        self._fire("PreToolUse", {"TERM_PROGRAM": "tmux"}, tool_name="Bash")
        row = self._row()
        self.assertEqual(row["tmux_socket"], self.SOCK)
        self.assertEqual(row["tmux_pane"], "%3")

    def test_new_pane_replaces_old_one(self):
        self._fire("SessionStart", self.TMUX_ENV)
        self._fire(
            "UserPromptSubmit",
            dict(self.TMUX_ENV, TMUX_PANE="%7"),
            prompt="moved",
        )
        self.assertEqual(self._row()["tmux_pane"], "%7")


class SessionStartUsageTests(AgentBoardTestCase):
    """A parent SessionStart stores the model and, on resume/fork, the
    context size; any other parent event that names a model stores it."""

    def _send(self, event, tool="claude", ts="2026-10-24T05:00:00.000000Z", **fields):
        payload = {"session_id": "s1", "cwd": "/tmp", "hook_event_name": event, **fields}
        with mock.patch("agentboard.hook.subprocess.run") as run, \
             mock.patch("agentboard.hook.subprocess.Popen"), \
             mock.patch("agentboard.hook._now_iso", return_value=ts):
            run.return_value = mock.Mock(returncode=0, stdout="")
            hook.process_payload(self.conn, tool, payload)

    def _row(self, tool="claude"):
        return self.conn.execute(
            "SELECT * FROM sessions WHERE tool=? AND session_id='s1'", (tool,)
        ).fetchone()

    def test_startup_with_model_stores_it(self):
        self._send("SessionStart", source="startup", model="claude-opus-5-5")
        row = self._row()
        self.assertEqual(row["model"], "claude-opus-5-5")
        self.assertIsNone(row["context_tokens"])
        self.assertIsNone(row["context_at"])

    def test_resume_without_model_keeps_the_stored_model(self):
        self._send("SessionStart", source="startup", model="claude-opus-5-5")
        self._send("Stop", ts="2026-10-24T05:01:00.000000Z", last_assistant_message="done")
        self._send("SessionStart", ts="2026-10-24T05:02:00.000000Z", source="resume")
        self.assertEqual(self._row()["model"], "claude-opus-5-5")

    def test_later_model_replaces_the_stored_one(self):
        self._send("SessionStart", source="startup", model="claude-opus-5-5")
        self._send("SessionStart", ts="2026-10-24T05:02:00.000000Z", source="compact",
                   model="claude-fable-5-1")
        self.assertEqual(self._row()["model"], "claude-fable-5-1")

    def test_resume_with_context_tokens_stores_size_and_time(self):
        self._send("SessionStart", source="startup", model="claude-opus-5-5")
        late = "2026-10-24T06:00:00.123456Z"
        self._send("SessionStart", ts=late, source="resume", context_tokens=308011)
        row = self._row()
        self.assertEqual(row["context_tokens"], 308011)
        self.assertEqual(row["context_at"], late)
        self.assertEqual(row["model"], "claude-opus-5-5")
        extra = json.loads(self.conn.execute(
            "SELECT extra FROM events ORDER BY id DESC LIMIT 1").fetchone()[0])
        self.assertEqual(extra["context_tokens"], 308011)

    def test_compact_without_size_clears_context_until_the_next_reply(self):
        from agentboard import backfill
        self._send("SessionStart", source="resume", context_tokens=308011,
                   ts="2026-10-24T05:00:00.000000Z")
        compact_at = "2026-10-24T05:10:00.123456Z"
        self._send("SessionStart", ts=compact_at, source="compact", model="claude-opus-5-5")
        row = self._row()
        self.assertIsNone(row["context_tokens"])
        self.assertEqual(row["context_at"], compact_at)
        self.assertEqual(row["model"], "claude-opus-5-5")

        # The re-parse sees only the pre-compact assistant line: it keeps NULL.
        path = self.tmp_path / "s1.jsonl"
        usage = {"input_tokens": 8, "cache_read_input_tokens": 300000,
                 "cache_creation_input_tokens": 8003, "output_tokens": 50}
        lines = [
            {"type": "user", "timestamp": "2026-10-24T05:00:30.000Z",
             "message": {"role": "user", "content": "go"}},
            {"type": "assistant", "timestamp": "2026-10-24T05:09:00.000Z",
             "message": {"role": "assistant", "model": "claude-opus-5-5", "usage": usage,
                         "content": [{"type": "text", "text": "done"}]}},
        ]
        path.write_text("".join(json.dumps(line) + "\n" for line in lines))
        backfill.backfill_single_session(self.conn, "claude", "s1", transcript_path=str(path))
        row = self._row()
        self.assertIsNone(row["context_tokens"])
        self.assertEqual(row["context_at"], compact_at)

        # The first reply after the compact measures it again.
        lines.append({"type": "assistant", "timestamp": "2026-10-24T05:11:00.000Z",
                      "message": {"role": "assistant", "model": "claude-opus-5-5",
                                  "usage": dict(usage, cache_read_input_tokens=40000),
                                  "content": [{"type": "text", "text": "after"}]}})
        path.write_text("".join(json.dumps(line) + "\n" for line in lines))
        backfill.backfill_single_session(self.conn, "claude", "s1", transcript_path=str(path))
        row = self._row()
        self.assertEqual(row["context_tokens"], 48011)
        self.assertEqual(row["context_at"], "2026-10-24T05:11:00.000Z")

    def test_compact_with_size_stores_it(self):
        self._send("SessionStart", source="resume", context_tokens=308011)
        self._send("SessionStart", ts="2026-10-24T05:10:00.000000Z", source="compact",
                   context_tokens=40000)
        self.assertEqual(self._row()["context_tokens"], 40000)

    def test_non_compact_session_start_without_size_keeps_context(self):
        self._send("SessionStart", source="resume", context_tokens=308011)
        self._send("SessionStart", ts="2026-10-24T05:10:00.000000Z", source="startup",
                   model="claude-opus-5-5")
        self.assertEqual(self._row()["context_tokens"], 308011)

    def test_subagent_session_start_writes_neither(self):
        self._send("SessionStart", source="startup", model="claude-opus-5-5")
        self._send("SessionStart", ts="2026-10-24T05:02:00.000000Z", agent_id="sub-1",
                   model="claude-haiku-4-5", context_tokens=1000)
        row = self._row()
        self.assertEqual(row["model"], "claude-opus-5-5")
        self.assertIsNone(row["context_tokens"])

    def test_malformed_values_are_ignored(self):
        for bad in ("308011", 12.5, True, -1, None, [1]):
            with self.subTest(context_tokens=bad):
                self.conn.execute("DELETE FROM sessions")
                self._send("SessionStart", source="resume", context_tokens=bad)
                row = self._row()
                self.assertIsNone(row["context_tokens"])
                self.assertIsNone(row["context_at"])
        for bad in ("", "   ", 5, None, {"id": "x"}):
            with self.subTest(model=bad):
                self.conn.execute("DELETE FROM sessions")
                self._send("SessionStart", source="startup", model=bad)
                self.assertIsNone(self._row()["model"])

    def test_zero_context_tokens_is_valid(self):
        self._send("SessionStart", source="fork", context_tokens=0)
        self.assertEqual(self._row()["context_tokens"], 0)

    def test_other_events_never_write_context(self):
        self._send("UserPromptSubmit", prompt="go", context_tokens=5)
        self._send("Stop", ts="2026-10-24T05:01:00.000000Z", last_assistant_message="done",
                   context_tokens=5)
        row = self._row()
        self.assertIsNone(row["model"])
        self.assertIsNone(row["context_tokens"])

    def test_any_parent_event_that_names_a_model_records_it(self):
        # Codex sends `model` on every hook event (codex-rs hooks/src/schema.rs:
        # SessionStart, UserPromptSubmit, Stop, PreToolUse, ...), so a /model
        # switch shows up at the next event; the rule is tool-agnostic.
        self._send("SessionStart", tool="codex", source="startup", model="gpt-5")
        self._send("UserPromptSubmit", tool="codex", ts="2026-10-24T05:01:00.000000Z",
                   prompt="go", model="gpt-5-codex", turn_id="t1")
        self.assertEqual(self._row("codex")["model"], "gpt-5-codex")
        self._send("Stop", tool="codex", ts="2026-10-24T05:02:00.000000Z",
                   last_assistant_message="done", model="gpt-5.1-codex", turn_id="t1")
        self.assertEqual(self._row("codex")["model"], "gpt-5.1-codex")
        self._send("Stop", tool="codex", ts="2026-10-24T05:03:00.000000Z",
                   last_assistant_message="again", turn_id="t2")
        self.assertEqual(self._row("codex")["model"], "gpt-5.1-codex")

    def test_subagent_events_never_write_the_model(self):
        self._send("SessionStart", source="startup", model="claude-opus-5-5")
        self._send("SubagentStart", ts="2026-10-24T05:01:00.000000Z", agent_id="a1",
                   agent_type="Explore", model="claude-haiku-4-5")
        self._send("PreToolUse", ts="2026-10-24T05:02:00.000000Z", agent_id="a1",
                   tool_name="Read", tool_input={}, model="claude-haiku-4-5")
        self._send("SubagentStop", ts="2026-10-24T05:03:00.000000Z", agent_id="a1",
                   model="claude-haiku-4-5")
        self.assertEqual(self._row()["model"], "claude-opus-5-5")

    def test_codex_session_start_with_and_without_model(self):
        self._send("SessionStart", tool="codex", source="startup", model="gpt-5-codex")
        self.assertEqual(self._row("codex")["model"], "gpt-5-codex")
        self._send("SessionStart", tool="codex", ts="2026-10-24T05:02:00.000000Z",
                   source="resume")
        row = self._row("codex")
        self.assertEqual(row["model"], "gpt-5-codex")
        self.assertIsNone(row["context_tokens"])

    def test_codex_session_start_without_model_leaves_it_null(self):
        self._send("SessionStart", tool="codex", source="startup")
        self.assertIsNone(self._row("codex")["model"])


class HookNotesTests(AgentBoardTestCase):
    MILESTONE = (
        "Opened PR #31 for the fade task and pushed the follow-up commit. " * 8
    ).strip()

    def _send(self, payload, tool="claude"):
        payload = {"cwd": "/tmp", **payload}
        with mock.patch("agentboard.hook.subprocess.run") as run, \
             mock.patch("agentboard.hook.subprocess.Popen"):
            run.return_value = mock.Mock(returncode=0, stdout="")
            hook.process_payload(self.conn, tool, payload)

    def _stop(self, text, session_id="s1", tool="claude", **extra):
        self._send({"session_id": session_id, "hook_event_name": "Stop",
                    "last_assistant_message": text, **extra}, tool=tool)

    def _prompt(self, text, session_id="s1", **extra):
        self._send({"session_id": session_id, "hook_event_name": "UserPromptSubmit",
                    "prompt": text, **extra})

    def _notes(self):
        return self.conn.execute("SELECT * FROM notes ORDER BY id").fetchall()

    def _last_event_id(self):
        return self.conn.execute("SELECT MAX(id) FROM events").fetchone()[0]

    def test_user_action_stop_records_needs_you_note_with_event_id(self):
        self._stop("USER_ACTION: [proj] plan in Pending — awaiting your approval")
        (note,) = self._notes()
        self.assertEqual(note["kind"], "needs_you")
        self.assertEqual(note["source"], "stop")
        self.assertEqual(note["tool"], "claude")
        self.assertEqual(note["session_id"], "s1")
        self.assertEqual(note["event_id"], self._last_event_id())
        self.assertEqual(note["last_event_id"], self._last_event_id())
        self.assertEqual(note["count"], 1)

    def test_quiet_tick_records_no_note(self):
        self._prompt("# Autonomous loop tick")
        self._stop("Quiet tick, backoff window, nothing changed.")
        self.assertEqual(self._notes(), [])

    def test_repeat_bumps_count(self):
        self._stop("USER_ACTION: approve the plan")
        self._stop("USER_ACTION: approve the plan")
        (note,) = self._notes()
        self.assertEqual(note["count"], 2)
        self.assertEqual(note["last_event_id"], self._last_event_id())
        self.assertLess(note["event_id"], note["last_event_id"])

    def test_subagent_stop_records_no_note(self):
        self._stop("USER_ACTION: approve the plan", agent_id="sub-1")
        self.assertEqual(self._notes(), [])

    def test_codex_stop_records_note(self):
        self._stop("USER_ACTION: approve the plan", tool="codex", session_id="c1")
        (note,) = self._notes()
        self.assertEqual((note["tool"], note["session_id"]), ("codex", "c1"))

    def test_attended_milestone_is_not_kept_but_unattended_is(self):
        self._prompt("please open the PR")
        self._stop(self.MILESTONE)
        self.assertEqual(self._notes(), [])
        self._prompt("# Autonomous loop tick (dynamic pacing)\n\nRun the check.")
        self._stop(self.MILESTONE)
        (note,) = self._notes()
        self.assertEqual(note["kind"], "update")

    def test_subagent_prompt_does_not_change_the_unattended_flag(self):
        # A subagent-tagged prompt overwrites sessions.last_prompt, but the
        # unattended gate reads the newest *parent* prompt, as rebuild does.
        self._prompt("# Autonomous loop tick")
        self._prompt("summarize the diff", agent_id="sub-1")
        self._stop(self.MILESTONE)
        (note,) = self._notes()
        self.assertEqual(note["kind"], "update")
        # notes --rebuild --reset classifies the same Stop the same way.
        from agentboard import notes
        notes.rebuild(self.conn, {}, reset=True)
        (rebuilt,) = self._notes()
        self.assertEqual((rebuilt["kind"], rebuilt["source"]), ("update", "backfill"))

    def test_subagent_autonomous_prompt_does_not_make_a_parent_turn_unattended(self):
        self._prompt("please open the PR")
        self._prompt("# Autonomous loop tick", agent_id="sub-1")
        self._stop(self.MILESTONE)
        self.assertEqual(self._notes(), [])

    def test_unattended_prefix_override_from_config(self):
        (self.agentboard_home / "config.json").write_text(
            json.dumps({"note_unattended_prefixes": ["[cron]"]})
        )
        self._prompt("[cron] nightly")
        self._stop(self.MILESTONE)
        self.assertEqual(self._notes()[0]["kind"], "update")

    def test_repeat_mention_of_a_noted_ref_records_nothing(self):
        self._prompt("# Autonomous loop tick")
        self._stop("The builder is still fixing PR #4's review findings.")
        (first,) = self._notes()
        self.assertEqual(first["kind"], "update")
        self._notes_dismissed_all()
        # The ref is already in a (dismissed) note of this session...
        self._stop("The builder is still on PR #4; I will re-review when it reports back.")
        self.assertEqual(len(self._notes()), 1)
        # ...but an event about it is still news, and another session's
        # notes never count as seen.
        self._stop("PR #4 merged as abc1234.")
        self._prompt("# Autonomous loop tick", session_id="s2")
        self._stop("The builder is still on PR #4; I will re-review when it reports back.",
                   session_id="s2")
        self.assertEqual(
            [(n["session_id"], n["text"]) for n in self._notes()[1:]],
            [("s1", "PR #4 merged as abc1234."),
             ("s2", "The builder is still on PR #4; I will re-review when it reports back.")],
        )

    def test_agent_message_after_loop_tick_keeps_turn_unattended(self):
        self._prompt("# Autonomous loop tick")
        self._prompt('<agent-message from="reviewer">PR #31 looks good</agent-message>')
        self._stop(self.MILESTONE)
        (note,) = self._notes()
        self.assertEqual(note["kind"], "update")
        kinds = [r[0] for r in self.conn.execute(
            "SELECT prompt_kind FROM events WHERE event='UserPromptSubmit' ORDER BY id")]
        self.assertEqual(kinds, ["loop", "injected"])
        row = self.conn.execute("SELECT last_prompt FROM sessions").fetchone()
        self.assertEqual(row["last_prompt"], "# Autonomous loop tick")

    def test_cron_tick_stop_records_update_note(self):
        cron_prompt = "Sweep the review queue and report anything new.\n" + "Check item. " * 40
        self.assertGreater(len(cron_prompt), 200)
        self._prompt("set up the sweep")
        self._stop("Scheduled.", session_crons=[
            {"id": "c1", "schedule": "*/10 * * * *", "recurring": True, "prompt": cron_prompt},
        ])
        # The tick arrives with its whitespace reflowed; no loop prefix.
        self._prompt(cron_prompt.replace(" ", "  "))
        self._stop(self.MILESTONE)
        (note,) = self._notes()
        self.assertEqual(note["kind"], "update")
        kind = self.conn.execute(
            "SELECT prompt_kind FROM events WHERE event='UserPromptSubmit' ORDER BY id DESC"
        ).fetchone()[0]
        self.assertEqual(kind, "cron")

    def test_injected_prefix_override_from_config(self):
        (self.agentboard_home / "config.json").write_text(
            json.dumps({"prompt_injected_prefixes": ["[relay]"]})
        )
        self._prompt("[relay] from the other session")
        self._prompt("<task-notification>now a human prompt</task-notification>")
        kinds = [r[0] for r in self.conn.execute(
            "SELECT prompt_kind FROM events WHERE event='UserPromptSubmit' ORDER BY id")]
        self.assertEqual(kinds, ["injected", "human"])

    def test_waiting_restatement_records_no_note(self):
        self._prompt("# Autonomous loop tick")
        self._stop("Nothing changed this tick:\n- **PR #5.** It still needs your approval.")
        self._stop("Two things are still waiting on you:\n- **PR #5.** Approve it.")
        self.assertEqual(self._notes(), [])

    def _notes_dismissed_all(self):
        self.conn.execute("UPDATE notes SET dismissed_at='2026-10-20T00:00:00.000000Z'")

    def test_classifier_failure_keeps_the_event_and_session_update(self):
        with mock.patch("agentboard.notes.classify", side_effect=RuntimeError("boom")):
            self._stop("USER_ACTION: approve the plan")
        self.assertEqual(self._notes(), [])
        events = self.conn.execute("SELECT event FROM events").fetchall()
        self.assertEqual([e["event"] for e in events], ["Stop"])
        row = self.conn.execute("SELECT last_assistant FROM sessions").fetchone()
        self.assertEqual(row["last_assistant"], "USER_ACTION: approve the plan")

    def test_note_write_failure_rolls_back_only_the_note(self):
        with mock.patch("agentboard.notes.record_note", side_effect=RuntimeError("boom")):
            self._stop("USER_ACTION: approve the plan")
        self.assertEqual(self._notes(), [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
        self.assertFalse(self.conn.in_transaction)

    def test_empty_stop_text_records_no_note(self):
        self._stop("")
        self.assertEqual(self._notes(), [])

    def test_other_events_never_import_notes(self):
        # Parent prompts (classified) and Stops (noted) import notes; tool
        # use, notifications and subagent events never do.
        import agentboard

        self._prompt("# Autonomous loop tick")
        original = sys.modules.pop("agentboard.notes")
        delattr(agentboard, "notes")
        try:
            self._send({"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Bash"})
            self._send({"session_id": "s1", "hook_event_name": "PostToolUse", "tool_name": "Bash"})
            self._send({"session_id": "s1", "hook_event_name": "Notification",
                        "notification_type": "idle_prompt", "message": "idle"})
            self._send({"session_id": "s1", "hook_event_name": "SubagentStop", "agent_id": "a1"})
            self._send({"session_id": "s1", "hook_event_name": "UserPromptSubmit",
                        "agent_id": "a1", "prompt": "subagent prompt"})
            self.assertNotIn("agentboard.notes", sys.modules)
        finally:
            sys.modules["agentboard.notes"] = original
            agentboard.notes = original


class FindAgentPidTests(AgentBoardTestCase):
    def test_real_ancestry_python_sh_claude(self):
        # hook (python) calls _find_agent_pid(os.getppid()), i.e. starting
        # from its own parent: /bin/sh (1000), whose parent is claude (998).
        fake = _fake_ps_chain([(1000, 998, "ttys003", "sh"), (998, 1, "ttys003", "claude")])
        with mock.patch("agentboard.hook.subprocess.run", side_effect=fake):
            pid, tty, _is_daemon = hook._find_agent_pid(1000)
        self.assertEqual(pid, 998)
        self.assertEqual(tty, "ttys003")

    def test_no_claude_ancestor_returns_none_not_start_pid(self):
        fake = _fake_ps_chain([(100, 99, "ttys005", "sh"), (99, 1, "??", "launchd")])
        with mock.patch("agentboard.hook.subprocess.run", side_effect=fake):
            pid, tty, _is_daemon = hook._find_agent_pid(100)
        self.assertIsNone(pid)
        self.assertEqual(tty, "ttys005")

    def test_walk_limit_is_five(self):
        hops = [
            (100, 99, "ttys001", "sh"),
            (99, 98, "ttys001", "x"),
            (98, 97, "ttys001", "y"),
            (97, 96, "ttys001", "z"),
            (96, 95, "ttys001", "claude"),
        ]
        fake = _fake_ps_chain(hops)
        with mock.patch("agentboard.hook.subprocess.run", side_effect=fake):
            pid, tty, _is_daemon = hook._find_agent_pid(100)
        self.assertEqual(pid, 96)

    def test_tty_prefers_resolved_agent_over_detached_first_hop(self):
        # Hook spawned detached (setsid): its own parent has no controlling
        # terminal ("??"), but the claude process further up does.
        fake = _fake_ps_chain([(1000, 998, "??", "sh"), (998, 1, "ttys007", "claude")])
        with mock.patch("agentboard.hook.subprocess.run", side_effect=fake):
            pid, tty, _is_daemon = hook._find_agent_pid(1000)
        self.assertEqual(pid, 998)
        self.assertEqual(tty, "ttys007")

    def test_tty_falls_back_to_first_hop_when_resolved_agent_has_none(self):
        # ppid 500 (not 1) so this isn't mistaken for the daemon pattern --
        # just an ordinary claude process that happens to report no tty.
        fake = _fake_ps_chain([(1000, 998, "ttys003", "sh"), (998, 500, "??", "claude")])
        with mock.patch("agentboard.hook.subprocess.run", side_effect=fake):
            pid, tty, is_daemon = hook._find_agent_pid(1000)
        self.assertEqual(pid, 998)
        self.assertEqual(tty, "ttys003")
        self.assertFalse(is_daemon)

    def test_codex_app_server_daemon_is_detected_and_rejected(self):
        # Real shape: hook -> ... -> codex app-server-daemon (ppid 1, no tty).
        fake = _fake_ps_chain([
            (1000, 40007, "ttys003", "sh"),
            (40007, 1, "??", "codex"),
        ])
        with mock.patch("agentboard.hook.subprocess.run", side_effect=fake):
            pid, tty, is_daemon = hook._find_agent_pid(1000)
        self.assertIsNone(pid)
        self.assertIsNone(tty)
        self.assertTrue(is_daemon)

    def test_daemon_pattern_requires_both_ppid1_and_no_tty(self):
        # ppid 1 alone isn't enough if it still has a real tty.
        fake = _fake_ps_chain([(1000, 998, "ttys003", "sh"), (998, 1, "ttys003", "codex")])
        with mock.patch("agentboard.hook.subprocess.run", side_effect=fake):
            pid, tty, is_daemon = hook._find_agent_pid(1000)
        self.assertEqual(pid, 998)
        self.assertFalse(is_daemon)


class IsUntrustedDaemonPidTests(AgentBoardTestCase):
    def test_none_pid_is_not_a_daemon(self):
        self.assertFalse(hook.is_untrusted_daemon_pid(None))

    def test_daemon_shaped_pid_detected(self):
        fake = _fake_ps_chain([(40007, 1, "??", "codex")])
        with mock.patch("agentboard.hook.subprocess.run", side_effect=fake):
            self.assertTrue(hook.is_untrusted_daemon_pid(40007))

    def test_normal_pid_not_detected(self):
        fake = _fake_ps_chain([(998, 500, "ttys003", "codex")])
        with mock.patch("agentboard.hook.subprocess.run", side_effect=fake):
            self.assertFalse(hook.is_untrusted_daemon_pid(998))

    def test_unknown_pid_not_detected(self):
        fake = _fake_ps_chain([])
        with mock.patch("agentboard.hook.subprocess.run", side_effect=fake):
            self.assertFalse(hook.is_untrusted_daemon_pid(999999))


class CleanTtyTests(AgentBoardTestCase):
    def test_clean_tty_treats_questionmarks_and_empty_as_none(self):
        self.assertIsNone(hook._clean_tty("??"))
        self.assertIsNone(hook._clean_tty(""))
        self.assertIsNone(hook._clean_tty(None))
        self.assertEqual(hook._clean_tty("ttys003"), "ttys003")


class CleanPromptTextTests(AgentBoardTestCase):
    def test_plain_text_passes_through(self):
        self.assertEqual(hook.clean_prompt_text("fix the bug"), "fix the bug")

    def test_none_passes_through(self):
        self.assertIsNone(hook.clean_prompt_text(None))

    def test_exit_and_quit_are_not_prompts(self):
        for word in ("exit", "Exit", "QUIT", "quit", "/exit", "/Quit", "  exit  ", "/quit\n"):
            self.assertIsNone(hook.clean_prompt_text(word), repr(word))

    def test_exit_inside_a_real_sentence_is_still_a_prompt(self):
        self.assertEqual(hook.clean_prompt_text("please exit cleanly"), "please exit cleanly")

    def test_model_command_is_not_a_prompt(self):
        text = (
            "<command-name>/model</command-name>\n            "
            "<command-message>model</command-message>\n            "
            "<command-args>opus</command-args>"
        )
        self.assertIsNone(hook.clean_prompt_text(text))

    def test_clear_command_is_not_a_prompt(self):
        text = (
            "<command-name>/clear</command-name>\n            "
            "<command-message>clear</command-message>\n            "
            "<command-args></command-args>"
        )
        self.assertIsNone(hook.clean_prompt_text(text))

    def test_skill_command_name_first_resolves_to_slash_command(self):
        text = (
            "<command-name>/review-pr</command-name>\n"
            "<command-message>review-pr</command-message>\n"
            "<command-args>ABC-123 fix the crash</command-args>"
        )
        self.assertEqual(hook.clean_prompt_text(text), "/review-pr ABC-123 fix the crash")

    def test_skill_command_message_first_resolves_to_slash_command(self):
        text = (
            "<command-message>review-pr</command-message>\n"
            "<command-name>/review-pr</command-name>\n"
            "<command-args>ABC-123 fix the crash</command-args>"
        )
        self.assertEqual(hook.clean_prompt_text(text), "/review-pr ABC-123 fix the crash")

    def test_skill_command_with_no_args_tag(self):
        text = (
            "<command-message>review-pr-deterministic</command-message>\n"
            "<command-name>/review-pr-deterministic</command-name>"
        )
        self.assertEqual(hook.clean_prompt_text(text), "/review-pr-deterministic")


class NormalizeBranchTests(AgentBoardTestCase):
    def test_head_becomes_none(self):
        self.assertIsNone(hook.normalize_branch("HEAD"))

    def test_empty_and_none_become_none(self):
        self.assertIsNone(hook.normalize_branch(""))
        self.assertIsNone(hook.normalize_branch(None))

    def test_real_branch_passes_through(self):
        self.assertEqual(hook.normalize_branch("ABC-123"), "ABC-123")


class SpawnTodosReparseTests(AgentBoardTestCase):
    def test_no_transcript_path_does_not_spawn(self):
        with mock.patch("agentboard.hook.subprocess.Popen") as popen:
            hook.spawn_todos_reparse("claude", "s1", None)
        popen.assert_not_called()

    def test_spawns_detached_with_expected_args(self):
        fake_popen = mock.Mock()
        with mock.patch("agentboard.hook.subprocess.Popen", return_value=fake_popen) as popen:
            hook.spawn_todos_reparse("claude", "s1", "/tmp/sess1.jsonl")
        popen.assert_called_once()
        args, kwargs = popen.call_args
        argv = args[0]
        self.assertEqual(argv[1:], [
            "backfill", "--session", "claude:s1", "--transcript", "/tmp/sess1.jsonl",
        ])
        self.assertTrue(str(argv[0]).endswith("bin/agentboard"))
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertTrue(kwargs["start_new_session"])
        # Fully fire-and-forget: never waited on.
        fake_popen.wait.assert_not_called()
        fake_popen.communicate.assert_not_called()

    def test_second_spawn_within_guard_window_is_skipped(self):
        with mock.patch("agentboard.hook.subprocess.Popen") as popen:
            hook.spawn_todos_reparse("claude", "s1", "/tmp/sess1.jsonl")
            hook.spawn_todos_reparse("claude", "s1", "/tmp/sess1.jsonl")
        self.assertEqual(popen.call_count, 1)

    def test_different_sessions_are_not_guarded_against_each_other(self):
        with mock.patch("agentboard.hook.subprocess.Popen") as popen:
            hook.spawn_todos_reparse("claude", "s1", "/tmp/sess1.jsonl")
            hook.spawn_todos_reparse("claude", "s2", "/tmp/sess2.jsonl")
        self.assertEqual(popen.call_count, 2)

    def test_spawn_failure_is_swallowed(self):
        with mock.patch("agentboard.hook.subprocess.Popen", side_effect=OSError("nope")):
            hook.spawn_todos_reparse("claude", "s1", "/tmp/sess1.jsonl")  # must not raise


class ProcessPayloadReparseTriggerTests(AgentBoardTestCase):
    def test_stop_event_triggers_spawn(self):
        with mock.patch("agentboard.hook.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="")
            hook.process_payload(
                self.conn, "claude",
                {"session_id": "s1", "cwd": "/tmp", "hook_event_name": "SessionStart"},
            )
        with mock.patch("agentboard.hook.subprocess.run", side_effect=_no_shellouts), \
             mock.patch("agentboard.hook.subprocess.Popen") as popen:
            hook.process_payload(
                self.conn, "claude",
                {
                    "session_id": "s1", "cwd": "/tmp", "transcript_path": "/tmp/sess1.jsonl",
                    "hook_event_name": "Stop", "last_assistant_message": "done",
                },
            )
        popen.assert_called_once()

    def test_session_end_event_triggers_spawn(self):
        with mock.patch("agentboard.hook.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="")
            hook.process_payload(
                self.conn, "claude",
                {"session_id": "s1", "cwd": "/tmp", "hook_event_name": "SessionStart"},
            )
        with mock.patch("agentboard.hook.subprocess.run", side_effect=_no_shellouts), \
             mock.patch("agentboard.hook.subprocess.Popen") as popen:
            hook.process_payload(
                self.conn, "claude",
                {
                    "session_id": "s1", "cwd": "/tmp", "transcript_path": "/tmp/sess1.jsonl",
                    "hook_event_name": "SessionEnd", "reason": "prompt_input_exit",
                },
            )
        popen.assert_called_once()

    def test_other_events_do_not_trigger_spawn(self):
        with mock.patch("agentboard.hook.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="")
            hook.process_payload(
                self.conn, "claude",
                {"session_id": "s1", "cwd": "/tmp", "hook_event_name": "SessionStart"},
            )
        with mock.patch("agentboard.hook.subprocess.run", side_effect=_no_shellouts), \
             mock.patch("agentboard.hook.subprocess.Popen") as popen:
            hook.process_payload(
                self.conn, "claude",
                {
                    "session_id": "s1", "cwd": "/tmp", "transcript_path": "/tmp/sess1.jsonl",
                    "hook_event_name": "PreToolUse", "tool_name": "Bash",
                },
            )
        popen.assert_not_called()

    def test_subagent_stop_does_not_trigger_spawn(self):
        with mock.patch("agentboard.hook.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="")
            hook.process_payload(
                self.conn, "claude",
                {"session_id": "s1", "cwd": "/tmp", "hook_event_name": "SessionStart"},
            )
        with mock.patch("agentboard.hook.subprocess.run", side_effect=_no_shellouts), \
             mock.patch("agentboard.hook.subprocess.Popen") as popen:
            hook.process_payload(
                self.conn, "claude",
                {
                    "session_id": "s1", "cwd": "/tmp", "transcript_path": "/tmp/sess1.jsonl",
                    "agent_id": "sub-1", "hook_event_name": "Stop",
                    "last_assistant_message": "sub done",
                },
            )
        popen.assert_not_called()

    def test_process_payload_returns_promptly_even_if_spawn_hangs(self):
        # A real detached Popen() call itself returns immediately (it
        # doesn't wait for the child); simulate a slow/failing spawn path
        # and confirm process_payload still doesn't block on it.
        with mock.patch("agentboard.hook.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="")
            hook.process_payload(
                self.conn, "claude",
                {"session_id": "s1", "cwd": "/tmp", "hook_event_name": "SessionStart"},
            )
        with mock.patch("agentboard.hook.subprocess.run", side_effect=_no_shellouts), \
             mock.patch("agentboard.hook.subprocess.Popen") as popen:
            start = time.monotonic()
            hook.process_payload(
                self.conn, "claude",
                {
                    "session_id": "s1", "cwd": "/tmp", "transcript_path": "/tmp/sess1.jsonl",
                    "hook_event_name": "Stop", "last_assistant_message": "done",
                },
            )
            elapsed = time.monotonic() - start
        popen.return_value.wait.assert_not_called()
        self.assertLess(elapsed, 1.0)


class SpawnCodexMapTests(AgentBoardTestCase):
    def test_no_cwd_does_not_spawn(self):
        with mock.patch("agentboard.hook.subprocess.Popen") as popen:
            hook.spawn_codex_map("s1", None, "2026-06-15T12:00:00.000000Z")
        popen.assert_not_called()

    def test_spawns_detached_with_expected_args(self):
        fake_popen = mock.Mock()
        with mock.patch("agentboard.hook.subprocess.Popen", return_value=fake_popen) as popen:
            hook.spawn_codex_map("s1", "/tmp/proj", "2026-06-15T12:00:00.000000Z")
        popen.assert_called_once()
        args, kwargs = popen.call_args
        argv = args[0]
        self.assertEqual(argv[1:], [
            "map-codex", "--session", "s1", "--cwd", "/tmp/proj",
            "--at", "2026-06-15T12:00:00.000000Z",
        ])
        self.assertTrue(str(argv[0]).endswith("bin/agentboard"))
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertTrue(kwargs["start_new_session"])
        fake_popen.wait.assert_not_called()

    def test_spawn_failure_is_swallowed(self):
        with mock.patch("agentboard.hook.subprocess.Popen", side_effect=OSError("nope")):
            hook.spawn_codex_map("s1", "/tmp/proj", "2026-06-15T12:00:00.000000Z")  # must not raise


def _daemon_fake_run(cmd, **kwargs):
    """Simulates hook -> ... -> shared app-server-daemon (ppid 1, no tty)."""
    if cmd[0] == "git":
        return mock.Mock(returncode=0, stdout="")
    table = {1000: (40007, "ttys003", "sh"), 40007: (1, "??", "codex")}
    entry = table.get(int(cmd[-1]))
    if entry is None:
        return mock.Mock(stdout="")
    ppid, tty, comm = entry
    return mock.Mock(stdout=f"{ppid} {tty} {comm}\n")


class ProcessPayloadCodexMapTriggerTests(AgentBoardTestCase):
    def test_session_start_from_daemon_triggers_spawn(self):
        with mock.patch("agentboard.hook.subprocess.run", side_effect=_daemon_fake_run), \
             mock.patch("agentboard.hook.os.getppid", return_value=1000), \
             mock.patch("agentboard.hook.subprocess.Popen") as popen:
            hook.process_payload(
                self.conn, "codex",
                {"session_id": "s1", "cwd": "/tmp/proj", "hook_event_name": "SessionStart"},
            )
        popen.assert_called_once()
        argv = popen.call_args[0][0]
        self.assertIn("map-codex", argv)

    def test_user_prompt_submit_from_daemon_triggers_spawn(self):
        with mock.patch("agentboard.hook.subprocess.run", side_effect=_daemon_fake_run), \
             mock.patch("agentboard.hook.os.getppid", return_value=1000), \
             mock.patch("agentboard.hook.subprocess.Popen") as popen:
            hook.process_payload(
                self.conn, "codex",
                {
                    "session_id": "s1", "cwd": "/tmp/proj",
                    "hook_event_name": "UserPromptSubmit", "prompt": "resume thread",
                },
            )
        popen.assert_called_once()

    def test_claude_sessions_never_trigger_spawn(self):
        with mock.patch("agentboard.hook.subprocess.run", side_effect=_no_shellouts), \
             mock.patch("agentboard.hook.subprocess.Popen") as popen:
            hook.process_payload(
                self.conn, "claude",
                {"session_id": "s1", "cwd": "/tmp/proj", "hook_event_name": "SessionStart"},
            )
        popen.assert_not_called()

    def test_non_trigger_event_does_not_spawn(self):
        with mock.patch("agentboard.hook.subprocess.run", side_effect=_daemon_fake_run), \
             mock.patch("agentboard.hook.os.getppid", return_value=1000):
            hook.process_payload(
                self.conn, "codex",
                {"session_id": "s1", "cwd": "/tmp/proj", "hook_event_name": "SessionStart"},
            )
        with mock.patch("agentboard.hook.subprocess.run", side_effect=_daemon_fake_run), \
             mock.patch("agentboard.hook.os.getppid", return_value=1000), \
             mock.patch("agentboard.hook.subprocess.Popen") as popen:
            hook.process_payload(
                self.conn, "codex",
                {
                    "session_id": "s1", "cwd": "/tmp/proj",
                    "hook_event_name": "PostToolUse", "tool_name": "Bash",
                },
            )
        popen.assert_not_called()

    def test_non_daemon_resolution_does_not_trigger_spawn(self):
        # A real (non-daemon) codex ancestor resolves pid/tty directly --
        # the heuristic mapper would be redundant, so it must not run.
        def fake_run(cmd, **kwargs):
            if cmd[0] == "git":
                return mock.Mock(returncode=0, stdout="")
            table = {1000: (500, "ttys003", "codex")}
            entry = table.get(int(cmd[-1]))
            if entry is None:
                return mock.Mock(stdout="")
            ppid, tty, comm = entry
            return mock.Mock(stdout=f"{ppid} {tty} {comm}\n")

        with mock.patch("agentboard.hook.subprocess.run", side_effect=fake_run), \
             mock.patch("agentboard.hook.os.getppid", return_value=1000), \
             mock.patch("agentboard.hook.subprocess.Popen") as popen:
            hook.process_payload(
                self.conn, "codex",
                {"session_id": "s1", "cwd": "/tmp/proj", "hook_event_name": "SessionStart"},
            )
        popen.assert_not_called()

    def test_subagent_tagged_prompt_does_not_trigger_spawn(self):
        with mock.patch("agentboard.hook.subprocess.run", side_effect=_daemon_fake_run), \
             mock.patch("agentboard.hook.os.getppid", return_value=1000):
            hook.process_payload(
                self.conn, "codex",
                {"session_id": "s1", "cwd": "/tmp/proj", "hook_event_name": "SessionStart"},
            )
        with mock.patch("agentboard.hook.subprocess.run", side_effect=_daemon_fake_run), \
             mock.patch("agentboard.hook.os.getppid", return_value=1000), \
             mock.patch("agentboard.hook.subprocess.Popen") as popen:
            hook.process_payload(
                self.conn, "codex",
                {
                    "session_id": "s1", "cwd": "/tmp/proj", "agent_id": "sub-1",
                    "hook_event_name": "UserPromptSubmit", "prompt": "subagent turn",
                },
            )
        popen.assert_not_called()


class HookRunTests(AgentBoardTestCase):
    def test_run_never_raises_on_bad_json(self):
        hook.run("claude", stdin_stream=io.StringIO("not json"))
        log_path = self.agentboard_home / "hook-errors.log"
        self.assertTrue(log_path.exists())

    def test_run_prints_nothing_to_stdout(self):
        payload = json.dumps({"session_id": "s1", "cwd": "/tmp", "hook_event_name": "SessionStart"})
        captured = io.StringIO()
        with mock.patch("sys.stdout", captured):
            with mock.patch("agentboard.hook.subprocess.run") as run:
                run.return_value = mock.Mock(returncode=0, stdout="")
                hook.run("claude", stdin_stream=io.StringIO(payload))
        self.assertEqual(captured.getvalue(), "")

    def test_run_missing_session_id_is_noop(self):
        hook.run("claude", stdin_stream=io.StringIO(json.dumps({"hook_event_name": "SessionStart"})))
        count = self.conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"]
        self.assertEqual(count, 0)

    def test_error_log_capped(self):
        hook._log_error("x" * 2000)
        for _ in range(1000):
            hook._log_error("y" * 2000)
        log_path = self.agentboard_home / "hook-errors.log"
        self.assertLessEqual(log_path.stat().st_size, hook.MAX_LOG_BYTES)


_OLD_SYSTEM_PYTHON3 = Path("/usr/bin/python3")


def _old_system_python3_version():
    try:
        out = subprocess.run(
            [str(_OLD_SYSTEM_PYTHON3), "-c", "import sys; print(sys.version_info[:2])"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


class HookDiagTests(AgentBoardTestCase):
    """Hook tracing: routine events (including the daemon and subagent
    downgrades) log at debug only; only real drops log at info."""

    def setUp(self):
        super().setUp()
        self._old_level = os.environ.pop("AGENTBOARD_LOG_LEVEL", None)

    def tearDown(self):
        os.environ.pop("AGENTBOARD_LOG_LEVEL", None)
        if self._old_level is not None:
            os.environ["AGENTBOARD_LOG_LEVEL"] = self._old_level
        super().tearDown()

    def _lines(self):
        path = self.agentboard_home / "agentboard.log"
        return path.read_text().splitlines() if path.exists() else []

    def _daemon_codex_event(self):
        def fake_run(cmd, **kwargs):
            if cmd[0] == "git":
                return mock.Mock(returncode=0, stdout="")
            table = {1000: (40007, "ttys003", "sh"), 40007: (1, "??", "codex")}
            entry = table.get(int(cmd[-1]))
            if entry is None:
                return mock.Mock(stdout="")
            ppid, tty, comm = entry
            return mock.Mock(stdout=f"{ppid} {tty} {comm}\n")

        with mock.patch("agentboard.hook.subprocess.run", side_effect=fake_run), \
             mock.patch("agentboard.hook.subprocess.Popen"), \
             mock.patch("agentboard.hook.os.getppid", return_value=1000):
            hook.process_payload(
                self.conn, "codex",
                {"session_id": "s1", "cwd": "/tmp", "hook_event_name": "SessionStart"},
            )

    def _subagent_event(self):
        with mock.patch("agentboard.hook.subprocess.run") as run, \
             mock.patch("agentboard.hook.subprocess.Popen"):
            run.return_value = mock.Mock(returncode=0, stdout="")
            hook.process_payload(
                self.conn, "claude",
                {"session_id": "s1", "cwd": "/tmp", "hook_event_name": "SessionStart"},
            )
            hook.process_payload(
                self.conn, "claude",
                {
                    "session_id": "s1", "cwd": "/tmp", "agent_id": "sub-1",
                    "hook_event_name": "Stop", "last_assistant_message": "sub done",
                },
            )

    def test_daemon_event_logs_nothing_at_info(self):
        self._daemon_codex_event()
        self.assertEqual(self._lines(), [])

    def test_daemon_event_debug_line(self):
        os.environ["AGENTBOARD_LOG_LEVEL"] = "debug"
        self._daemon_codex_event()
        events = [l for l in self._lines() if " hook.event " in l]
        self.assertEqual(len(events), 1)
        self.assertIn("DEBUG hook.event tool=codex event=SessionStart session=s1", events[0])
        self.assertIn("daemon=true", events[0])
        self.assertIn("pid=-", events[0])
        self.assertFalse([l for l in self._lines() if " INFO " in l])

    def test_subagent_event_logs_nothing_at_info(self):
        self._subagent_event()
        self.assertEqual(self._lines(), [])

    def test_subagent_event_debug_line(self):
        os.environ["AGENTBOARD_LOG_LEVEL"] = "debug"
        self._subagent_event()
        events = [l for l in self._lines() if " hook.event " in l]
        self.assertEqual(len(events), 2)
        self.assertIn("event=Stop", events[1])
        self.assertIn("subagent=true", events[1])
        self.assertIn("ignored=subagent", events[1])
        self.assertFalse([l for l in self._lines() if " INFO " in l])

    def test_missing_session_id_is_an_info_drop(self):
        hook.run("claude", stdin_stream=io.StringIO(json.dumps({"hook_event_name": "Stop"})))
        [line] = self._lines()
        self.assertIn("INFO hook.drop tool=claude event=Stop reason=missing_session_id", line)

    def test_empty_payload_is_an_info_drop(self):
        hook.run("claude", stdin_stream=io.StringIO(""))
        [line] = self._lines()
        self.assertIn("reason=empty_payload", line)

    def test_unparseable_payload_is_an_info_drop_and_still_hits_hook_errors(self):
        hook.run("codex", stdin_stream=io.StringIO("{not json"))
        [line] = self._lines()
        self.assertIn("INFO hook.drop tool=codex reason=unparseable_payload", line)
        self.assertTrue((self.agentboard_home / "hook-errors.log").exists())

    def test_diag_is_not_imported_at_hook_module_top(self):
        tree = ast.parse(Path(hook.__file__).read_text())
        top_level = [
            alias.name
            for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        ]
        self.assertNotIn("diag", top_level)

    def test_broken_diag_never_breaks_the_hook(self):
        with mock.patch("agentboard.diag.log", side_effect=RuntimeError("log broke")):
            hook.run("claude", stdin_stream=io.StringIO(json.dumps({"hook_event_name": "Stop"})))
        self.assertFalse((self.agentboard_home / "hook-errors.log").exists())


class BinAgentboardOldInterpreterTests(AgentBoardTestCase):
    """Regression test for the exact repro in the adversarial review: a
    hook command resolved against (or, as here, invoked directly with) an
    old system python3 must not crash -- it must fail soft and keep the
    hook's "always exit 0, nothing on stdout" contract.

    Runs the real core/bin/agentboard, under the real macOS system python3
    at /usr/bin/python3 (3.9.6 as of this writing) when present, since that
    is precisely the interpreter the review found breaking in practice.
    Skips itself if that binary isn't old enough (or isn't present) rather
    than asserting anything about an environment it can't verify.
    """

    @unittest.skipUnless(_OLD_SYSTEM_PYTHON3.is_file(), "/usr/bin/python3 not present on this machine")
    def test_hook_fails_soft_under_old_system_python3(self):
        version = _old_system_python3_version()
        if version is None:
            self.skipTest("/usr/bin/python3 did not run")
        major, minor = ast.literal_eval(version)
        if (major, minor) >= (3, 11):
            self.skipTest(f"/usr/bin/python3 is already >= 3.11 ({version}) on this machine")

        bin_path = Path(__file__).resolve().parent.parent / "bin" / "agentboard"
        payload = json.dumps({
            "session_id": "old-interp-e2e", "cwd": "/tmp", "hook_event_name": "SessionStart",
        })
        result = subprocess.run(
            [str(_OLD_SYSTEM_PYTHON3), str(bin_path), "hook", "--tool", "claude"],
            input=payload, text=True, capture_output=True, timeout=10,
            env={"HOME": str(self.fake_home), "AGENTBOARD_HOME": str(self.agentboard_home), "PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("Traceback", result.stderr)

        log_path = self.agentboard_home / "hook-errors.log"
        self.assertTrue(log_path.is_file())
        self.assertIn("3.11", log_path.read_text())

        row = self.conn.execute(
            "SELECT 1 FROM sessions WHERE tool='claude' AND session_id='old-interp-e2e'"
        ).fetchone()
        self.assertIsNone(row, "the skipped event must not be recorded")


class BakedAgentboardHomeTests(AgentBoardTestCase):
    """A non-default AGENTBOARD_HOME used at install time is baked into the
    installed/bundled shim (see pin_core_bin in install.sh/build-app.sh) so
    hooks agree with the app and backfill on one database, even though
    AGENTBOARD_HOME itself is never set in the hook's own environment."""

    def test_baked_home_is_used_when_agentboard_home_is_unset(self):
        core_dir = self.tmp_path / "staged-core"
        repo_core = Path(__file__).resolve().parent.parent
        shutil.copytree(repo_core / "bin", core_dir / "bin")
        shutil.copytree(
            repo_core / "agentboard", core_dir / "agentboard",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        bin_path = core_dir / "bin" / "agentboard"
        bin_path.chmod(0o755)

        # Simulate exactly the textual edit pin_core_bin makes.
        baked_home = self.tmp_path / "baked agentboard home"
        text = bin_path.read_text()
        self.assertIn("_BAKED_AGENTBOARD_HOME = None", text)
        bin_path.write_text(
            text.replace(
                "_BAKED_AGENTBOARD_HOME = None",
                f"_BAKED_AGENTBOARD_HOME = {str(baked_home)!r}",
            )
        )

        payload = json.dumps({
            "session_id": "baked-home-e2e", "cwd": "/tmp", "hook_event_name": "SessionStart",
        })
        env = dict(os.environ)
        env["HOME"] = str(self.fake_home)
        env.pop("AGENTBOARD_HOME", None)

        result = subprocess.run(
            [sys.executable, str(bin_path), "hook", "--tool", "claude"],
            input=payload, text=True, capture_output=True, timeout=10, env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

        self.assertTrue(
            (baked_home / "agentboard.db").is_file(),
            "the hook must write into the baked AGENTBOARD_HOME, not the default",
        )
        self.assertFalse(
            (self.fake_home / "Library" / "Application Support" / "AgentBoard" / "agentboard.db").exists(),
            "the hook must not also write into the default data dir",
        )

    def test_dev_checkout_bin_agentboard_still_uses_env_python3_shebang(self):
        # The repo's own core/bin/agentboard (never installed/pinned) must
        # keep resolving python3 off PATH -- only install.sh's/build-app.sh's
        # copies get their shebang rewritten.
        bin_path = Path(__file__).resolve().parent.parent / "bin" / "agentboard"
        self.assertEqual(bin_path.read_text().splitlines()[0], "#!/usr/bin/env python3")


if __name__ == "__main__":
    import unittest

    unittest.main()
