import json
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentboard import backfill
from tests.helpers import AgentBoardTestCase


def write_jsonl(path, entries):
    with open(path, "w") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")


class ClaudeTranscriptTests(AgentBoardTestCase):
    def test_basic_prompt_and_assistant_extraction(self):
        path = self.tmp_path / "sess1.jsonl"
        write_jsonl(path, [
            {"type": "mode", "mode": "normal"},
            {
                "type": "user", "message": {"role": "user", "content": "fix the bug"},
                "timestamp": "2026-01-01T00:00:00.000Z", "cwd": "/tmp/proj",
                "gitBranch": "main", "entrypoint": "cli", "userType": "external",
            },
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "Fixed it."}]},
                "timestamp": "2026-01-01T00:05:00.000Z",
            },
        ])
        result = backfill.parse_claude_transcript(path)
        self.assertEqual(result["cwd"], "/tmp/proj")
        self.assertEqual(result["branch"], "main")
        self.assertEqual(result["first_prompt"], "fix the bug")
        self.assertEqual(result["last_assistant"], "Fixed it.")
        self.assertEqual(result["last_event"], "Stop")
        self.assertEqual(result["interactive"], 1)

    def test_skips_tool_result_and_meta_and_synthetic_tags(self):
        path = self.tmp_path / "sess2.jsonl"
        write_jsonl(path, [
            {
                "type": "user", "message": {"role": "user", "content": "real prompt"},
                "timestamp": "2026-01-01T00:00:00.000Z", "cwd": "/tmp", "gitBranch": "main",
            },
            {
                "type": "user", "isMeta": True,
                "message": {"role": "user", "content": "<local-command-caveat>ignore</local-command-caveat>"},
                "timestamp": "2026-01-01T00:01:00.000Z",
            },
            {
                "type": "user",
                "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]},
                "timestamp": "2026-01-01T00:02:00.000Z",
            },
            {
                "type": "user",
                "message": {"role": "user", "content": "<local-command-stdout>side effect</local-command-stdout>"},
                "timestamp": "2026-01-01T00:03:00.000Z",
            },
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "all done"}]},
                "timestamp": "2026-01-01T00:04:00.000Z",
            },
        ])
        result = backfill.parse_claude_transcript(path)
        self.assertEqual(result["first_prompt"], "real prompt")
        self.assertEqual(result["last_prompt"], "real prompt")
        self.assertEqual(result["last_assistant"], "all done")
        self.assertEqual(result["last_event"], "Stop")

    def test_last_event_is_user_prompt_submit_when_turn_mid_flight(self):
        path = self.tmp_path / "sess3.jsonl"
        write_jsonl(path, [
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "first reply"}]},
                "timestamp": "2026-01-01T00:00:00.000Z", "cwd": "/tmp",
            },
            {
                "type": "user", "message": {"role": "user", "content": "follow up"},
                "timestamp": "2026-01-01T00:01:00.000Z", "cwd": "/tmp", "gitBranch": "main",
            },
        ])
        result = backfill.parse_claude_transcript(path)
        self.assertEqual(result["last_event"], "UserPromptSubmit")

    def test_title_from_ai_title_entry(self):
        path = self.tmp_path / "sess4.jsonl"
        write_jsonl(path, [
            {
                "type": "user", "message": {"role": "user", "content": "do the thing"},
                "timestamp": "2026-01-01T00:00:00.000Z", "cwd": "/tmp", "gitBranch": "main",
            },
            {"type": "ai-title", "aiTitle": "do-the-thing-session"},
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
                "timestamp": "2026-01-01T00:05:00.000Z",
            },
        ])
        result = backfill.parse_claude_transcript(path)
        self.assertEqual(result["title"], "do-the-thing-session")

    def test_title_falls_back_to_first_prompt_line(self):
        path = self.tmp_path / "sess5.jsonl"
        write_jsonl(path, [
            {
                "type": "user", "message": {"role": "user", "content": "line one\nline two"},
                "timestamp": "2026-01-01T00:00:00.000Z", "cwd": "/tmp", "gitBranch": "main",
            },
        ])
        result = backfill.parse_claude_transcript(path)
        self.assertEqual(result["title"], "line one")

    def test_sdk_cli_entrypoint_marks_noninteractive(self):
        path = self.tmp_path / "sess6.jsonl"
        write_jsonl(path, [
            {
                "type": "user", "message": {"role": "user", "content": "automated run"},
                "timestamp": "2026-01-01T00:00:00.000Z", "cwd": "/tmp",
                "entrypoint": "sdk-cli", "userType": "external",
            },
        ])
        result = backfill.parse_claude_transcript(path)
        self.assertEqual(result["interactive"], 0)

    def test_skill_command_resolves_to_clean_prompt_and_title(self):
        path = self.tmp_path / "sess_cmd1.jsonl"
        write_jsonl(path, [
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": (
                        "<command-message>review-pr</command-message>\n"
                        "<command-name>/review-pr</command-name>\n"
                        "<command-args>ABC-123 fix the crash</command-args>"
                    ),
                },
                "timestamp": "2026-01-01T00:00:00.000Z", "cwd": "/tmp", "gitBranch": "main",
            },
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "working on it"}]},
                "timestamp": "2026-01-01T00:05:00.000Z",
            },
        ])
        result = backfill.parse_claude_transcript(path)
        self.assertEqual(result["first_prompt"], "/review-pr ABC-123 fix the crash")
        self.assertEqual(result["title"], "/review-pr ABC-123 fix the crash")

    def test_local_only_command_skipped_for_first_real_prompt(self):
        path = self.tmp_path / "sess_cmd2.jsonl"
        write_jsonl(path, [
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": (
                        "<command-name>/model</command-name>\n"
                        "<command-message>model</command-message>\n"
                        "<command-args>opus</command-args>"
                    ),
                },
                "timestamp": "2026-01-01T00:00:00.000Z", "cwd": "/tmp", "gitBranch": "main",
            },
            {
                "type": "user",
                "message": {"role": "user", "content": "<local-command-stdout>Set model to opus</local-command-stdout>"},
                "timestamp": "2026-01-01T00:00:01.000Z",
            },
            {
                "type": "user",
                "message": {"role": "user", "content": "actually fix the real bug"},
                "timestamp": "2026-01-01T00:00:02.000Z",
            },
        ])
        result = backfill.parse_claude_transcript(path)
        self.assertEqual(result["first_prompt"], "actually fix the real bug")
        self.assertEqual(result["title"], "actually fix the real bug")

    def test_head_branch_normalized_to_none(self):
        path = self.tmp_path / "sess_head.jsonl"
        write_jsonl(path, [
            {
                "type": "user", "message": {"role": "user", "content": "hi"},
                "timestamp": "2026-01-01T00:00:00.000Z", "cwd": "/Users/example", "gitBranch": "HEAD",
            },
        ])
        result = backfill.parse_claude_transcript(path)
        self.assertIsNone(result["branch"])

    def test_bare_exit_skipped_for_first_real_prompt(self):
        path = self.tmp_path / "sess_exit.jsonl"
        write_jsonl(path, [
            {
                "type": "user", "message": {"role": "user", "content": "exit"},
                "timestamp": "2026-01-01T00:00:00.000Z", "cwd": "/tmp", "gitBranch": "main",
            },
            {
                "type": "user", "message": {"role": "user", "content": "do the real task"},
                "timestamp": "2026-01-01T00:00:01.000Z",
            },
        ])
        result = backfill.parse_claude_transcript(path)
        self.assertEqual(result["first_prompt"], "do the real task")

    def test_only_exit_leaves_no_prompt(self):
        path = self.tmp_path / "sess_only_exit.jsonl"
        write_jsonl(path, [
            {
                "type": "user", "message": {"role": "user", "content": "  /QUIT  "},
                "timestamp": "2026-01-01T00:00:00.000Z", "cwd": "/tmp", "gitBranch": "main",
            },
        ])
        result = backfill.parse_claude_transcript(path)
        self.assertIsNone(result["first_prompt"])

    def test_iter_claude_transcripts_skips_subagents_dir(self):
        proj = self.fake_home / ".claude" / "projects" / "-tmp-proj"
        proj.mkdir(parents=True)
        write_jsonl(proj / "abc.jsonl", [{"type": "user", "message": {"role": "user", "content": "hi"}}])
        sub = proj / "subagents"
        sub.mkdir()
        write_jsonl(sub / "def.jsonl", [{"type": "user", "message": {"role": "user", "content": "hi"}}])

        found = list(backfill.iter_claude_transcripts(None))
        names = [p.name for _, p in found]
        self.assertIn("abc.jsonl", names)
        self.assertNotIn("def.jsonl", names)


def _assistant_tool_use(name, input_, ts):
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": name, "input": input_}],
        },
        "timestamp": ts,
    }


class TodosOpenTests(AgentBoardTestCase):
    def test_todowrite_full_list_counts_open(self):
        path = self.tmp_path / "sess_todo1.jsonl"
        write_jsonl(path, [
            _assistant_tool_use("TodoWrite", {"todos": [
                {"content": "a", "status": "completed"},
                {"content": "b", "status": "in_progress"},
                {"content": "c", "status": "pending"},
            ]}, "2026-01-01T00:00:00.000Z"),
        ])
        self.assertEqual(backfill._compute_claude_todos_open(path), 2)

    def test_todowrite_all_completed_is_zero_open(self):
        path = self.tmp_path / "sess_todo2.jsonl"
        write_jsonl(path, [
            _assistant_tool_use("TodoWrite", {"todos": [
                {"content": "a", "status": "completed"},
            ]}, "2026-01-01T00:00:00.000Z"),
        ])
        self.assertEqual(backfill._compute_claude_todos_open(path), 0)

    def test_taskcreate_then_updates_tracks_deltas(self):
        path = self.tmp_path / "sess_todo3.jsonl"
        write_jsonl(path, [
            _assistant_tool_use(
                "TaskCreate",
                {"tasks": json.dumps([{"subject": "a"}, {"subject": "b"}, {"subject": "c"}])},
                "2026-01-01T00:00:00.000Z",
            ),
            _assistant_tool_use("TaskUpdate", {"taskId": "1", "status": "completed"}, "2026-01-01T00:00:01.000Z"),
            _assistant_tool_use("TaskUpdate", {"taskId": "2", "status": "in_progress"}, "2026-01-01T00:00:02.000Z"),
        ])
        # task 1 completed, tasks 2 (in_progress) and 3 (still pending) open
        self.assertEqual(backfill._compute_claude_todos_open(path), 2)

    def test_taskstop_closes_a_task(self):
        path = self.tmp_path / "sess_todo4.jsonl"
        write_jsonl(path, [
            _assistant_tool_use(
                "TaskCreate", {"tasks": json.dumps([{"subject": "a"}, {"subject": "b"}])},
                "2026-01-01T00:00:00.000Z",
            ),
            _assistant_tool_use("TaskStop", {"task_id": "1"}, "2026-01-01T00:00:01.000Z"),
        ])
        self.assertEqual(backfill._compute_claude_todos_open(path), 1)

    def test_no_todo_tool_used_is_none(self):
        path = self.tmp_path / "sess_todo5.jsonl"
        write_jsonl(path, [
            {
                "type": "user", "message": {"role": "user", "content": "hi"},
                "timestamp": "2026-01-01T00:00:00.000Z",
            },
        ])
        self.assertIsNone(backfill._compute_claude_todos_open(path))

    def test_codex_update_plan_last_call_wins(self):
        path = self.tmp_path / "rollout_plan1.jsonl"
        write_jsonl(path, [
            {
                "type": "response_item", "timestamp": "2026-01-01T00:00:00.000Z",
                "payload": {
                    "type": "function_call", "name": "update_plan",
                    "arguments": json.dumps({"plan": [
                        {"status": "in_progress", "step": "a"},
                        {"status": "pending", "step": "b"},
                    ]}),
                },
            },
            {
                "type": "response_item", "timestamp": "2026-01-01T00:00:01.000Z",
                "payload": {
                    "type": "function_call", "name": "update_plan",
                    "arguments": json.dumps({"plan": [
                        {"status": "completed", "step": "a"},
                        {"status": "in_progress", "step": "b"},
                    ]}),
                },
            },
        ])
        self.assertEqual(backfill._compute_codex_todos_open(path), 1)

    def test_codex_no_update_plan_is_none(self):
        path = self.tmp_path / "rollout_plan2.jsonl"
        write_jsonl(path, [
            {"type": "session_meta", "timestamp": "2026-01-01T00:00:00.000Z", "payload": {"id": "x"}},
        ])
        self.assertIsNone(backfill._compute_codex_todos_open(path))


class CodexRolloutTests(AgentBoardTestCase):
    def test_old_schema_user_and_agent_message(self):
        path = self.tmp_path / "rollout1.jsonl"
        write_jsonl(path, [
            {
                "type": "session_meta", "timestamp": "2026-01-01T00:00:00.000Z",
                "payload": {
                    "id": "abc-123", "cwd": "/tmp/proj", "source": "cli",
                    "timestamp": "2026-01-01T00:00:00.000Z",
                    "git": {"branch": "feature-1"},
                },
            },
            {
                "timestamp": "2026-01-01T00:00:01.000Z", "type": "event_msg",
                "payload": {"type": "user_message", "message": "please build this"},
            },
            {
                "timestamp": "2026-01-01T00:05:00.000Z", "type": "event_msg",
                "payload": {"type": "agent_message", "message": "built it"},
            },
            {
                "timestamp": "2026-01-01T00:05:01.000Z", "type": "event_msg",
                "payload": {"type": "task_complete"},
            },
        ])
        result = backfill.parse_codex_rollout(path)
        self.assertEqual(result["session_id"], "abc-123")
        self.assertEqual(result["cwd"], "/tmp/proj")
        self.assertEqual(result["branch"], "feature-1")
        self.assertEqual(result["first_prompt"], "please build this")
        self.assertEqual(result["last_assistant"], "built it")
        self.assertEqual(result["last_event"], "Stop")
        self.assertEqual(result["interactive"], 1)

    def test_turn_aborted_maps_to_interrupt(self):
        path = self.tmp_path / "rollout2.jsonl"
        write_jsonl(path, [
            {
                "type": "session_meta", "timestamp": "2026-01-01T00:00:00.000Z",
                "payload": {"id": "abc-456", "cwd": "/tmp", "source": "cli"},
            },
            {
                "timestamp": "2026-01-01T00:00:01.000Z", "type": "event_msg",
                "payload": {"type": "user_message", "message": "do it"},
            },
            {
                "timestamp": "2026-01-01T00:00:02.000Z", "type": "event_msg",
                "payload": {"type": "turn_aborted"},
            },
        ])
        result = backfill.parse_codex_rollout(path)
        self.assertEqual(result["last_event"], "Interrupt")

    def test_new_schema_item_completed(self):
        path = self.tmp_path / "rollout3.jsonl"
        write_jsonl(path, [
            {
                "type": "session_meta", "timestamp": "2026-01-01T00:00:00.000Z",
                "payload": {"id": "abc-789", "cwd": "/tmp", "source": "vscode"},
            },
            {
                "timestamp": "2026-01-01T00:00:01.000Z", "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {"type": "UserMessage", "content": [{"type": "text", "text": "evaluate this"}]},
                },
            },
            {
                "timestamp": "2026-01-01T00:00:05.000Z", "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {"type": "AgentMessage", "content": [{"type": "text", "text": "evaluated"}]},
                },
            },
        ])
        result = backfill.parse_codex_rollout(path)
        self.assertEqual(result["first_prompt"], "evaluate this")
        self.assertEqual(result["last_assistant"], "evaluated")

    def test_exec_source_marks_noninteractive(self):
        path = self.tmp_path / "rollout4.jsonl"
        write_jsonl(path, [
            {
                "type": "session_meta", "timestamp": "2026-01-01T00:00:00.000Z",
                "payload": {"id": "abc-exec", "cwd": "/tmp", "source": "exec"},
            },
        ])
        result = backfill.parse_codex_rollout(path)
        self.assertEqual(result["interactive"], 0)

    def test_head_branch_normalized_to_none(self):
        path = self.tmp_path / "rollout5.jsonl"
        write_jsonl(path, [
            {
                "type": "session_meta", "timestamp": "2026-01-01T00:00:00.000Z",
                "payload": {
                    "id": "abc-head", "cwd": "/tmp", "source": "cli",
                    "git": {"branch": "HEAD"},
                },
            },
        ])
        result = backfill.parse_codex_rollout(path)
        self.assertIsNone(result["branch"])

    def test_bare_quit_skipped_for_last_prompt(self):
        path = self.tmp_path / "rollout6.jsonl"
        write_jsonl(path, [
            {
                "type": "session_meta", "timestamp": "2026-01-01T00:00:00.000Z",
                "payload": {"id": "abc-quit", "cwd": "/tmp", "source": "cli"},
            },
            {
                "timestamp": "2026-01-01T00:00:01.000Z", "type": "event_msg",
                "payload": {"type": "user_message", "message": "do the real task"},
            },
            {
                "timestamp": "2026-01-01T00:00:02.000Z", "type": "event_msg",
                "payload": {"type": "agent_message", "message": "done"},
            },
            {
                "timestamp": "2026-01-01T00:00:03.000Z", "type": "event_msg",
                "payload": {"type": "user_message", "message": "quit"},
            },
        ])
        result = backfill.parse_codex_rollout(path)
        self.assertEqual(result["last_prompt"], "do the real task")


class UpsertBackfillSessionTests(AgentBoardTestCase):
    def test_insert_new_backfill_session(self):
        backfill.upsert_backfill_session(self.conn, "claude", "s1", {
            "cwd": "/tmp", "branch": "main", "title": "t", "first_prompt": "p",
            "last_prompt": "p2", "last_assistant": "a", "started_at": "2026-01-01T00:00:00.000Z",
            "updated_at": "2026-01-01T00:05:00.000Z", "last_event": "Stop",
            "last_event_at": "2026-01-01T00:05:00.000Z", "transcript_path": "/x", "interactive": 1,
        })
        row = self.conn.execute("SELECT * FROM sessions WHERE tool='claude' AND session_id='s1'").fetchone()
        self.assertEqual(row["origin"], "backfill")
        self.assertEqual(row["title"], "t")

    def test_never_overwrites_hook_fields_except_nulls(self):
        self.conn.execute(
            "INSERT INTO sessions(tool, session_id, cwd, branch, title, origin, interactive) "
            "VALUES ('claude','s1','/real/cwd','real-branch',NULL,'hook',NULL)"
        )
        backfill.upsert_backfill_session(self.conn, "claude", "s1", {
            "cwd": "/backfill/cwd", "branch": "backfill-branch", "title": "backfill title",
            "first_prompt": None, "last_prompt": None, "last_assistant": None,
            "started_at": None, "updated_at": None, "last_event": None,
            "last_event_at": None, "transcript_path": None, "interactive": 0,
        })
        row = self.conn.execute("SELECT * FROM sessions WHERE tool='claude' AND session_id='s1'").fetchone()
        # hook-origin, non-null fields must survive untouched
        self.assertEqual(row["cwd"], "/real/cwd")
        self.assertEqual(row["branch"], "real-branch")
        self.assertEqual(row["origin"], "hook")
        # null fields get filled in
        self.assertEqual(row["title"], "backfill title")
        self.assertEqual(row["interactive"], 0)

    def test_corrects_legacy_head_branch_on_hook_origin_row(self):
        self.conn.execute(
            "INSERT INTO sessions(tool, session_id, cwd, branch, title, origin, interactive) "
            "VALUES ('claude','s1','/real/cwd','HEAD',NULL,'hook',NULL)"
        )
        backfill.upsert_backfill_session(self.conn, "claude", "s1", {
            "cwd": "/backfill/cwd", "branch": None, "title": None,
            "first_prompt": None, "last_prompt": None, "last_assistant": None,
            "started_at": None, "updated_at": None, "last_event": None,
            "last_event_at": None, "transcript_path": None, "interactive": None,
        })
        row = self.conn.execute("SELECT * FROM sessions WHERE tool='claude' AND session_id='s1'").fetchone()
        self.assertEqual(row["cwd"], "/real/cwd")  # untouched, not a known-bad value
        self.assertIsNone(row["branch"])  # HEAD corrected to None

    def test_corrects_legacy_command_markup_prompt_on_hook_origin_row(self):
        bad_prompt = "<command-name>/model</command-name><command-args>opus</command-args>"
        self.conn.execute(
            "INSERT INTO sessions(tool, session_id, first_prompt, last_prompt, title, origin) "
            "VALUES ('claude','s1',?,?,?,'hook')",
            (bad_prompt, bad_prompt, bad_prompt),
        )
        backfill.upsert_backfill_session(self.conn, "claude", "s1", {
            "cwd": None, "branch": None, "title": "fix the real bug",
            "first_prompt": "fix the real bug", "last_prompt": "fix the real bug",
            "last_assistant": None, "started_at": None, "updated_at": None,
            "last_event": None, "last_event_at": None, "transcript_path": None,
            "interactive": None,
        })
        row = self.conn.execute("SELECT * FROM sessions WHERE tool='claude' AND session_id='s1'").fetchone()
        self.assertEqual(row["first_prompt"], "fix the real bug")
        self.assertEqual(row["last_prompt"], "fix the real bug")
        self.assertEqual(row["title"], "fix the real bug")

    def test_corrects_legacy_bare_exit_prompt_on_hook_origin_row(self):
        # Real-world shape: a codex session whose first/last prompt is
        # literally "exit" (pre-fix, this also leaked into the title).
        self.conn.execute(
            "INSERT INTO sessions(tool, session_id, first_prompt, last_prompt, origin) "
            "VALUES ('codex','s1','exit','exit','hook')"
        )
        backfill.upsert_backfill_session(self.conn, "codex", "s1", {
            "cwd": None, "branch": None, "title": None,
            "first_prompt": None, "last_prompt": None,
            "last_assistant": "Exited.", "started_at": None, "updated_at": None,
            "last_event": "SessionEnd", "last_event_at": None, "transcript_path": None,
            "interactive": None,
        })
        row = self.conn.execute("SELECT * FROM sessions WHERE tool='codex' AND session_id='s1'").fetchone()
        self.assertIsNone(row["first_prompt"])
        self.assertIsNone(row["last_prompt"])

    def test_backfill_origin_rows_overwritten_on_rerun(self):
        backfill.upsert_backfill_session(self.conn, "claude", "s1", {
            "cwd": "/old", "branch": "old-branch", "title": "old", "first_prompt": None,
            "last_prompt": None, "last_assistant": None, "started_at": None,
            "updated_at": "2026-01-01T00:00:00.000Z", "last_event": "Stop",
            "last_event_at": None, "transcript_path": None, "interactive": 1,
        })
        backfill.upsert_backfill_session(self.conn, "claude", "s1", {
            "cwd": "/new", "branch": "new-branch", "title": "new", "first_prompt": None,
            "last_prompt": None, "last_assistant": None, "started_at": None,
            "updated_at": "2026-01-02T00:00:00.000Z", "last_event": "Stop",
            "last_event_at": None, "transcript_path": None, "interactive": 1,
        })
        row = self.conn.execute("SELECT * FROM sessions WHERE tool='claude' AND session_id='s1'").fetchone()
        self.assertEqual(row["cwd"], "/new")
        self.assertEqual(row["title"], "new")


class CorrectDaemonPidRowsTests(AgentBoardTestCase):
    def _insert(self, session_id, pid):
        self.conn.execute(
            "INSERT INTO sessions(tool, session_id, pid, tty, iterm_session_id, "
            "term_program, origin, updated_at) "
            "VALUES ('codex',?,?,'??','w0t1p0:OLD','iTerm.app','hook','2026-01-01T00:00:00.000000Z')",
            (session_id, pid),
        )

    def test_corrects_rows_whose_pid_is_currently_a_daemon(self):
        self._insert("s1", 40007)

        def fake_run(cmd, **kwargs):
            return mock.Mock(stdout="1 ?? codex\n")

        with mock.patch("agentboard.hook.subprocess.run", side_effect=fake_run):
            corrected = backfill.correct_daemon_pid_rows(self.conn)

        self.assertEqual(corrected, 1)
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE tool='codex' AND session_id='s1'"
        ).fetchone()
        self.assertIsNone(row["pid"])
        self.assertIsNone(row["tty"])
        self.assertIsNone(row["iterm_session_id"])
        self.assertIsNone(row["term_program"])

    def test_leaves_rows_with_a_real_client_pid_alone(self):
        import os

        real_pid = os.getpid()  # alive, and (per the fake ps below) not daemon-shaped
        self._insert("s1", real_pid)

        def fake_run(cmd, **kwargs):
            return mock.Mock(stdout="500 ttys003 codex\n")

        with mock.patch("agentboard.hook.subprocess.run", side_effect=fake_run):
            corrected = backfill.correct_daemon_pid_rows(self.conn)

        self.assertEqual(corrected, 0)
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE tool='codex' AND session_id='s1'"
        ).fetchone()
        self.assertEqual(row["pid"], real_pid)
        self.assertEqual(row["term_program"], "iTerm.app")

    def test_corrects_a_dead_pid_even_when_no_longer_identifiable_as_the_daemon(self):
        # The daemon restarts under a new pid (e.g. a Codex upgrade), so an
        # old daemon pid from before this fix is often just dead, not
        # provably daemon-shaped any more -- still correctable.
        self._insert("s1", 2**30)  # not a real pid; ps lookup finds nothing

        def fake_run(cmd, **kwargs):
            return mock.Mock(stdout="")

        with mock.patch("agentboard.hook.subprocess.run", side_effect=fake_run):
            corrected = backfill.correct_daemon_pid_rows(self.conn)

        self.assertEqual(corrected, 1)
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE tool='codex' AND session_id='s1'"
        ).fetchone()
        self.assertIsNone(row["pid"])

    def test_no_codex_rows_is_a_noop(self):
        corrected = backfill.correct_daemon_pid_rows(self.conn)
        self.assertEqual(corrected, 0)


class BackfillSingleSessionTests(AgentBoardTestCase):
    def _claude_fixture(self, path):
        write_jsonl(path, [
            {
                "type": "user", "message": {"role": "user", "content": "do the thing"},
                "timestamp": "2026-01-01T00:00:00.000Z", "cwd": "/tmp", "gitBranch": "main",
            },
            _assistant_tool_use(
                "TaskCreate", {"tasks": json.dumps([{"subject": "a"}, {"subject": "b"}])},
                "2026-01-01T00:00:01.000Z",
            ),
            _assistant_tool_use("TaskUpdate", {"taskId": "1", "status": "completed"}, "2026-01-01T00:00:02.000Z"),
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
                "timestamp": "2026-01-01T00:00:03.000Z",
            },
        ])

    def test_matches_full_backfill_result(self):
        path = self.tmp_path / "sess1.jsonl"
        self._claude_fixture(path)

        # What the full backfill would do for this one file:
        full_fields = backfill.parse_claude_transcript(path)
        full_conn_fields = dict(full_fields)

        # What backfill_single_session actually does, from a second,
        # independent connection/row:
        backfill.backfill_single_session(self.conn, "claude", "sess1", transcript_path=str(path))
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE tool='claude' AND session_id='sess1'"
        ).fetchone()

        for col in backfill.BACKFILL_COLUMNS:
            self.assertEqual(row[col], full_conn_fields[col], col)

    def test_uses_explicit_transcript_path(self):
        path = self.tmp_path / "sess2.jsonl"
        self._claude_fixture(path)
        result = backfill.backfill_single_session(self.conn, "claude", "sess2", transcript_path=str(path))
        self.assertEqual(result, "updated")
        row = self.conn.execute(
            "SELECT todos_open FROM sessions WHERE tool='claude' AND session_id='sess2'"
        ).fetchone()
        self.assertEqual(row["todos_open"], 1)

    def test_falls_back_to_db_transcript_path(self):
        path = self.tmp_path / "sess3.jsonl"
        self._claude_fixture(path)
        self.conn.execute(
            "INSERT INTO sessions(tool, session_id, transcript_path, origin) "
            "VALUES ('claude','sess3',?,'hook')",
            (str(path),),
        )
        result = backfill.backfill_single_session(self.conn, "claude", "sess3")
        self.assertEqual(result, "updated")
        row = self.conn.execute(
            "SELECT todos_open FROM sessions WHERE tool='claude' AND session_id='sess3'"
        ).fetchone()
        self.assertEqual(row["todos_open"], 1)

    def test_no_transcript_path_anywhere(self):
        result = backfill.backfill_single_session(self.conn, "claude", "nope")
        self.assertEqual(result, "no-transcript")

    def test_missing_file_on_disk(self):
        result = backfill.backfill_single_session(
            self.conn, "claude", "sess4", transcript_path=str(self.tmp_path / "nope.jsonl")
        )
        self.assertEqual(result, "missing-file")

    def test_unknown_tool(self):
        path = self.tmp_path / "sess5.jsonl"
        self._claude_fixture(path)
        result = backfill.backfill_single_session(self.conn, "gemini", "sess5", transcript_path=str(path))
        self.assertEqual(result, "unknown-tool")

    def test_codex_session(self):
        path = self.tmp_path / "rollout1.jsonl"
        write_jsonl(path, [
            {
                "type": "session_meta", "timestamp": "2026-01-01T00:00:00.000Z",
                "payload": {"id": "codex-1", "cwd": "/tmp", "source": "cli"},
            },
            {
                "type": "response_item", "timestamp": "2026-01-01T00:00:01.000Z",
                "payload": {
                    "type": "function_call", "name": "update_plan",
                    "arguments": json.dumps({"plan": [{"status": "pending", "step": "a"}]}),
                },
            },
        ])
        result = backfill.backfill_single_session(self.conn, "codex", "codex-1", transcript_path=str(path))
        self.assertEqual(result, "updated")
        row = self.conn.execute(
            "SELECT todos_open FROM sessions WHERE tool='codex' AND session_id='codex-1'"
        ).fetchone()
        self.assertEqual(row["todos_open"], 1)

    def test_respects_hook_origin_protection(self):
        # Same fill-or-correct rules as the full backfill -- a hook-origin
        # row's good cwd must survive, since backfill_single_session uses
        # the identical upsert_backfill_session path.
        path = self.tmp_path / "sess6.jsonl"
        self._claude_fixture(path)
        self.conn.execute(
            "INSERT INTO sessions(tool, session_id, cwd, transcript_path, origin) "
            "VALUES ('claude','sess6','/real/cwd',?,'hook')",
            (str(path),),
        )
        backfill.backfill_single_session(self.conn, "claude", "sess6")
        row = self.conn.execute(
            "SELECT cwd FROM sessions WHERE tool='claude' AND session_id='sess6'"
        ).fetchone()
        self.assertEqual(row["cwd"], "/real/cwd")


if __name__ == "__main__":
    import unittest

    unittest.main()
