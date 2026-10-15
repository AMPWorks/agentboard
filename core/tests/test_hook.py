import ast
import io
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentboard import hook
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
