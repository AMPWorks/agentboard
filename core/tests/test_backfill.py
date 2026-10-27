import json
import sqlite3
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

    def test_skips_injected_prompts_by_configured_prefixes(self):
        path = self.tmp_path / "inj.jsonl"
        write_jsonl(path, [
            {"type": "user", "message": {"role": "user", "content": "real prompt"},
             "timestamp": "2026-01-01T00:00:00.000Z", "cwd": "/tmp"},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "text", "text": '<agent-message from="x">ping</agent-message>'}]},
             "timestamp": "2026-01-01T00:01:00.000Z"},
            {"type": "user", "message": {"role": "user", "content": "<task-notification>x"},
             "timestamp": "2026-01-01T00:02:00.000Z"},
        ])
        try:
            backfill.set_injected_prefixes(backfill.notes.DEFAULT_INJECTED_PREFIXES)
            self.assertEqual(backfill.parse_claude_transcript(path)["last_prompt"], "real prompt")
            # A config override replaces the list (local-command tags stay skipped).
            (self.agentboard_home / "config.json").write_text(
                json.dumps({"prompt_injected_prefixes": ["<agent-message"]}))
            backfill.load_injected_prefixes()
            self.assertEqual(backfill.parse_claude_transcript(path)["last_prompt"],
                             "<task-notification>x")
        finally:
            backfill.set_injected_prefixes(backfill.notes.DEFAULT_INJECTED_PREFIXES)

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

    def _task_create(self, n, ts="2026-01-01T00:00:00.000Z"):
        return _assistant_tool_use(
            "TaskCreate", {"tasks": json.dumps([{"subject": str(i)} for i in range(n)])}, ts)

    def _append(self, path, entries):
        with open(path, "a") as fh:
            for entry in entries:
                fh.write(json.dumps(entry) + "\n")

    def test_todo_scan_resumes_from_offset(self):
        path = self.tmp_path / "resume.jsonl"
        write_jsonl(path, [
            {"type": "user", "message": {"role": "user", "content": "go"}},
            self._task_create(3),
        ])
        count, state = backfill._scan_claude_todos(path)
        self.assertEqual(count, 3)
        self.assertEqual(state["offset"], path.stat().st_size)
        self._append(path, [
            _assistant_tool_use("TaskUpdate", {"taskId": "1", "status": "completed"},
                                "2026-01-01T00:00:01.000Z"),
        ])
        # Only the appended line is parsed: the earlier TaskCreate (which
        # needs two json.loads calls) is not read again.
        real_loads = json.loads
        with mock.patch.object(backfill.json, "loads", side_effect=real_loads) as loads:
            count, state = backfill._scan_claude_todos(path, state)
        self.assertEqual(count, 2)
        self.assertEqual(loads.call_count, 1)
        self.assertEqual(state["offset"], path.stat().st_size)
        # Nothing new: no parse at all, same answer.
        with mock.patch.object(backfill.json, "loads", side_effect=real_loads) as loads:
            again = backfill._scan_claude_todos(path, state)
        self.assertEqual((again[0], loads.call_count), (2, 0))

    def test_todo_scan_restarts_when_file_shrinks(self):
        path = self.tmp_path / "shrink.jsonl"
        write_jsonl(path, [self._task_create(5), {"type": "user", "message": {"content": "x" * 500}}])
        count, state = backfill._scan_claude_todos(path)
        self.assertEqual(count, 5)
        write_jsonl(path, [self._task_create(2)])
        count, state = backfill._scan_claude_todos(path, state)
        self.assertEqual(count, 2)
        self.assertEqual(state["offset"], path.stat().st_size)

    def test_todo_scan_restarts_when_offset_is_not_at_a_line_start(self):
        path = self.tmp_path / "misaligned.jsonl"
        write_jsonl(path, [self._task_create(4)])
        _count, state = backfill._scan_claude_todos(path)
        stale = dict(state, offset=state["offset"] - 3, task_status={"1": "pending"})
        count, state = backfill._scan_claude_todos(path, stale)
        self.assertEqual(count, 4)
        # A malformed stored state also restarts cleanly.
        self.assertEqual(backfill._scan_claude_todos(path, {"offset": "x"})[0], 4)

    def test_todo_scan_leaves_a_trailing_partial_line_for_next_run(self):
        path = self.tmp_path / "partial.jsonl"
        write_jsonl(path, [self._task_create(2)])
        complete = path.stat().st_size
        partial = json.dumps(_assistant_tool_use(
            "TaskUpdate", {"taskId": "1", "status": "completed"}, "2026-01-01T00:00:01.000Z"))
        with open(path, "a") as fh:
            fh.write(partial)  # Claude Code mid-write: no newline yet
        count, state = backfill._scan_claude_todos(path)
        self.assertEqual((count, state["offset"]), (2, complete))
        with open(path, "a") as fh:
            fh.write("\n")
        count, state = backfill._scan_claude_todos(path, state)
        self.assertEqual((count, state["offset"]), (1, path.stat().st_size))

    def test_todo_scan_offsets_are_bytes_not_characters(self):
        path = self.tmp_path / "utf8.jsonl"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(_assistant_tool_use("TodoWrite", {"todos": [
                {"content": "café ✓ 日本語", "status": "pending"},
                {"content": "naïve", "status": "pending"},
            ]}, "2026-01-01T00:00:00.000Z"), ensure_ascii=False) + "\n")
        size = path.stat().st_size
        self.assertGreater(size, len(path.read_text(encoding="utf-8")))
        count, state = backfill._scan_claude_todos(path)
        self.assertEqual((count, state["offset"]), (2, size))
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(_assistant_tool_use("TodoWrite", {"todos": [
                {"content": "café ✓", "status": "completed"},
            ]}, "2026-01-01T00:00:01.000Z"), ensure_ascii=False) + "\n")
        count, state = backfill._scan_claude_todos(path, state)
        self.assertEqual((count, state["offset"]), (0, path.stat().st_size))

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

    def test_backfill_restores_interactive_when_transcript_has_a_prompt(self):
        # The hook marked a prompt-less SessionEnd as a helper; the transcript
        # shows a real first prompt (hooks installed mid-way).
        self.conn.execute(
            "INSERT INTO sessions(tool, session_id, origin, interactive) "
            "VALUES ('claude','s1','hook',0), ('claude','s2','hook',0)"
        )
        fields = {
            "cwd": "/tmp", "branch": None, "title": None, "first_prompt": "fix the bug",
            "last_prompt": None, "last_assistant": None, "started_at": None,
            "updated_at": None, "last_event": None, "last_event_at": None,
            "transcript_path": None, "interactive": 1,
        }
        backfill.upsert_backfill_session(self.conn, "claude", "s1", fields)
        # No prompt in the transcript either: it stays a helper.
        backfill.upsert_backfill_session(self.conn, "claude", "s2",
                                         dict(fields, first_prompt=None))
        rows = dict(self.conn.execute(
            "SELECT session_id, interactive FROM sessions ORDER BY session_id").fetchall())
        self.assertEqual(rows, {"s1": 1, "s2": 0})
        # An sdk-cli transcript parses as interactive 0 and never flips it.
        self.conn.execute("UPDATE sessions SET interactive=0 WHERE session_id='s1'")
        backfill.upsert_backfill_session(self.conn, "claude", "s1",
                                         dict(fields, interactive=0))
        self.assertEqual(self.conn.execute(
            "SELECT interactive FROM sessions WHERE session_id='s1'").fetchone()[0], 0)

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


def _user(text, ts="2026-01-01T00:00:00.000Z"):
    return {"type": "user", "message": {"role": "user", "content": text}, "timestamp": ts,
            "cwd": "/tmp", "gitBranch": "main"}


def _assistant(text, ts="2026-01-01T00:05:00.000Z"):
    return {"type": "assistant", "timestamp": ts,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


class TitleSourceTests(AgentBoardTestCase):
    def parse(self, entries):
        path = self.tmp_path / "t.jsonl"
        write_jsonl(path, entries)
        return backfill.parse_claude_transcript(path)

    def test_newest_ai_title_wins(self):
        result = self.parse([
            _user("start"), {"type": "ai-title", "aiTitle": "A"}, _assistant("x"),
            {"type": "ai-title", "aiTitle": "B"}, _assistant("y", "2026-01-01T00:06:00.000Z"),
        ])
        self.assertEqual((result["title"], result["title_source"]), ("B", "ai"))

    def test_custom_title_beats_a_newer_ai_title(self):
        result = self.parse([
            _user("start"), {"type": "custom-title", "customTitle": "My name"},
            _assistant("x"), {"type": "ai-title", "aiTitle": "Later AI"},
            _assistant("y", "2026-01-01T00:06:00.000Z"),
        ])
        self.assertEqual((result["title"], result["title_source"]), ("My name", "custom"))

    def test_custom_title_far_back_in_the_tail_is_still_found(self):
        filler = [_assistant(f"line {i} " + "x" * 200, "2026-01-01T00:05:00.000Z")
                  for i in range(2000)]
        result = self.parse(
            [_user("start"), {"type": "custom-title", "customTitle": "Named"}]
            + filler + [{"type": "ai-title", "aiTitle": "AI"}, _user("last")]
        )
        self.assertEqual((result["title"], result["title_source"]), ("Named", "custom"))

    def test_custom_title_hunt_stops_after_its_budget(self):
        # Past CUSTOM_TITLE_SCAN_BYTES the hunt gives up; a custom title the
        # row already stores is kept by the backfill merge rule instead.
        filler = [_assistant(f"line {i} " + "x" * 200, "2026-01-01T00:05:00.000Z")
                  for i in range(200)]
        entries = ([_user("start"), {"type": "custom-title", "customTitle": "Named"}]
                   + filler + [{"type": "ai-title", "aiTitle": "AI"}, _user("last")])
        with mock.patch.object(backfill, "CUSTOM_TITLE_SCAN_BYTES", 10_000):
            result = self.parse(entries)
        self.assertEqual((result["title"], result["title_source"]), ("AI", "ai"))
        self.assertEqual(result["last_prompt"], "last")
        self.assertEqual(result["last_assistant"], "line 199 " + "x" * 200)

    def test_summary_title_source(self):
        result = self.parse([_user("start"), {"type": "summary", "summary": "Sum"}])
        self.assertEqual((result["title"], result["title_source"]), ("Sum", "summary"))

    def test_fallback_title_source(self):
        result = self.parse([_user("first line\nsecond")])
        self.assertEqual((result["title"], result["title_source"]), ("first line", "fallback"))

    def test_newest_branch_in_the_tail_wins(self):
        old = dict(_user("start"), gitBranch="old-branch")
        new = dict(_assistant("x"), gitBranch="new-branch")
        self.assertEqual(self.parse([old, new])["branch"], "new-branch")

    def _hook_row(self, title, title_source=None):
        self.conn.execute(
            "INSERT INTO sessions(tool, session_id, title, title_source, origin) "
            "VALUES ('claude','s1',?,?,'hook')", (title, title_source),
        )

    def _row(self):
        return self.conn.execute(
            "SELECT title, title_source FROM sessions WHERE session_id='s1'"
        ).fetchone()

    def test_hook_row_title_updated_from_a_newer_ai_title(self):
        self._hook_row("PR #4 author investigation")
        fields = self.parse([_user("start"), {"type": "ai-title", "aiTitle": "Current work"}])
        backfill.upsert_backfill_session(self.conn, "claude", "s1", fields)
        self.assertEqual(tuple(self._row()), ("Current work", "ai"))

    def test_hook_row_title_kept_when_transcript_only_has_a_first_prompt(self):
        self._hook_row("Old real title")
        fields = self.parse([_user("just a prompt")])
        backfill.upsert_backfill_session(self.conn, "claude", "s1", fields)
        self.assertEqual(tuple(self._row()), ("Old real title", None))

    def test_custom_title_row_is_not_replaced_by_a_newer_ai_title(self):
        self._hook_row("My own name", "custom")
        fields = self.parse([_user("start"), {"type": "ai-title", "aiTitle": "Newer AI"}])
        backfill.upsert_backfill_session(self.conn, "claude", "s1", fields)
        self.assertEqual(tuple(self._row()), ("My own name", "custom"))
        fields = self.parse([_user("start"), {"type": "custom-title", "customTitle": "Renamed"}])
        backfill.upsert_backfill_session(self.conn, "claude", "s1", fields)
        self.assertEqual(tuple(self._row()), ("Renamed", "custom"))

    def test_generated_and_cli_titles_yield_only_to_custom(self):
        for source in ("generated", "cli"):
            self.conn.execute("DELETE FROM sessions")
            backfill.upsert_backfill_session(self.conn, "claude", "s1",
                                             {"title": "first", "title_source": "ai"})
            self.conn.execute("UPDATE sessions SET title='Retitled', title_source=?", (source,))
            backfill.upsert_backfill_session(self.conn, "claude", "s1",
                                             {"title": "AI again", "title_source": "ai"})
            self.assertEqual(tuple(self._row()), ("Retitled", source))
            backfill.upsert_backfill_session(self.conn, "claude", "s1",
                                             {"title": "Mine", "title_source": "custom"})
            self.assertEqual(tuple(self._row()), ("Mine", "custom"))

    def test_title_update_rules(self):
        update = backfill.title_update
        self.assertEqual(update(None, None, "t", "fallback"), ("t", "fallback"))
        self.assertEqual(update("old", "fallback", "new", "fallback"), ("new", "fallback"))
        self.assertIsNone(update("same", "ai", "same", "ai"))
        self.assertEqual(update("fb", "fallback", "AI", "ai"), ("AI", "ai"))
        self.assertIsNone(update("t", "ai", None, None))


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

    def test_second_run_resumes_the_todo_scan(self):
        path = self.tmp_path / "sessR.jsonl"
        self._claude_fixture(path)
        backfill.backfill_single_session(self.conn, "claude", "sessR", transcript_path=str(path))
        row = self.conn.execute("SELECT * FROM sessions WHERE session_id='sessR'").fetchone()
        self.assertEqual((row["todos_open"], row["todos_scan_offset"]),
                         (1, path.stat().st_size))
        self.assertEqual(json.loads(row["todos_scan_state"])["last_mechanism"], "task")
        with open(path, "a") as fh:
            fh.write(json.dumps(_assistant_tool_use(
                "TaskUpdate", {"taskId": "2", "status": "completed"},
                "2026-01-01T00:00:04.000Z")) + "\n")
        with mock.patch.object(backfill, "_scan_claude_todos",
                               wraps=backfill._scan_claude_todos) as scan:
            backfill.backfill_single_session(self.conn, "claude", "sessR",
                                             transcript_path=str(path))
        self.assertGreater(scan.call_args.args[1]["offset"], 0, "resumed, not from 0")
        row = self.conn.execute("SELECT * FROM sessions WHERE session_id='sessR'").fetchone()
        self.assertEqual((row["todos_open"], row["todos_scan_offset"]),
                         (0, path.stat().st_size))

    def test_offset_write_skipped_when_another_run_advanced_it(self):
        path = self.tmp_path / "sessC.jsonl"
        self._claude_fixture(path)
        backfill.backfill_single_session(self.conn, "claude", "sessC", transcript_path=str(path))
        base, state = backfill.stored_todo_scan(self.conn, "claude", "sessC")
        fields = backfill.parse_claude_transcript(path, todos_state=state)
        fields["todos_scan_base"] = base
        fields["last_assistant"] = "newer text"
        # Another run (a concurrent per-Stop re-parse) advanced the offset.
        self.conn.execute(
            "UPDATE sessions SET todos_scan_offset=?, todos_open=7 WHERE session_id='sessC'",
            (base + 100,),
        )
        fields["todos_open"] = 3
        backfill.upsert_backfill_session(self.conn, "claude", "sessC", fields)
        row = self.conn.execute("SELECT * FROM sessions WHERE session_id='sessC'").fetchone()
        self.assertEqual((row["todos_open"], row["todos_scan_offset"]), (7, base + 100))
        self.assertEqual(row["last_assistant"], "newer text", "other columns still update")
        # The same on a hook-origin row.
        self.conn.execute("UPDATE sessions SET origin='hook' WHERE session_id='sessC'")
        backfill.upsert_backfill_session(self.conn, "claude", "sessC", fields)
        row = self.conn.execute("SELECT * FROM sessions WHERE session_id='sessC'").fetchone()
        self.assertEqual((row["todos_open"], row["todos_scan_offset"]), (7, base + 100))

    def test_run_backfill_holds_no_transaction_while_parsing(self):
        projects = self.fake_home / ".claude" / "projects" / "-tmp-proj"
        projects.mkdir(parents=True)
        for name in ("a", "b"):
            self._claude_fixture(projects / f"{name}.jsonl")
        real_parse = backfill.parse_claude_transcript
        writes = []

        def slow_parse(path, todos_state=None):
            # Mid-parse, a hook's write on another connection must not wait.
            other = sqlite3.connect(str(self.agentboard_home / "agentboard.db"), timeout=0.2,
                                    isolation_level=None)
            try:
                other.execute("BEGIN IMMEDIATE")
                other.execute(
                    "INSERT INTO events(ts, tool, session_id, event) "
                    "VALUES ('t', 'claude', 'hook', 'PreToolUse')")
                other.execute("COMMIT")
                writes.append(path.name)
            finally:
                other.close()
            return real_parse(path, todos_state=todos_state)

        with mock.patch.object(backfill, "parse_claude_transcript", side_effect=slow_parse):
            stats = backfill.run_backfill(self.conn, days=None)
        self.assertEqual(sorted(writes), ["a.jsonl", "b.jsonl"])
        self.assertEqual((stats["claude"], stats["errors"]), (2, 0))
        self.assertFalse(self.conn.in_transaction)

    def test_run_backfill_upsert_failure_counts_and_continues(self):
        projects = self.fake_home / ".claude" / "projects" / "-tmp-proj"
        projects.mkdir(parents=True)
        for name in ("a", "b"):
            self._claude_fixture(projects / f"{name}.jsonl")
        real_upsert = backfill.upsert_backfill_session

        def flaky(conn, tool, session_id, fields):
            if session_id == "a":
                raise RuntimeError("boom")
            return real_upsert(conn, tool, session_id, fields)

        with mock.patch.object(backfill, "upsert_backfill_session", side_effect=flaky):
            stats = backfill.run_backfill(self.conn, days=None)
        self.assertEqual((stats["claude"], stats["errors"]), (1, 1))
        ids = [r[0] for r in self.conn.execute("SELECT session_id FROM sessions")]
        self.assertEqual(ids, ["b"])

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



def _usage_line(model, ts, usage=None, text=None, **extra):
    """A transcript assistant line with model and usage, as Claude Code writes
    it (one line per content block, each carrying the response's usage)."""
    content = ([{"type": "text", "text": text}] if text is not None
               else [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}])
    message = {"role": "assistant", "content": content}
    if model is not None:
        message["model"] = model
    if usage is not None:
        message["usage"] = usage
    return {"type": "assistant", "timestamp": ts, "message": message, **extra}


def _usage(input_tokens, cache_read=0, cache_creation=0, output=50):
    return {"input_tokens": input_tokens, "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation, "output_tokens": output}


def _synthetic_error_line(ts):
    return _usage_line("<synthetic>", ts, _usage(0), text="API Error: overloaded",
                       isApiErrorMessage=True)


class ModelContextParseTests(AgentBoardTestCase):
    def parse(self, entries):
        path = self.tmp_path / "mc.jsonl"
        write_jsonl(path, entries)
        return backfill.parse_claude_transcript(path)

    def test_newest_real_assistant_line_wins(self):
        result = self.parse([
            _user("start"),
            _usage_line("claude-sonnet-4-5-20250929", "2026-01-01T00:01:00.000Z",
                        _usage(10, 1000, 100), text="first"),
            _usage_line("claude-opus-5-5", "2026-01-01T00:02:00.000Z",
                        _usage(3, 300000, 8008), text="second"),
        ])
        self.assertEqual(result["model"], "claude-opus-5-5")
        self.assertEqual(result["context_tokens"], 308011)
        self.assertEqual(result["context_at"], "2026-01-01T00:02:00.000Z")

    def test_sidechain_line_is_skipped(self):
        result = self.parse([
            _user("start"),
            _usage_line("claude-opus-5-5", "2026-01-01T00:01:00.000Z", _usage(5, 500),
                        text="main"),
            _usage_line("claude-haiku-4-5", "2026-01-01T00:02:00.000Z", _usage(1),
                        text="sub", isSidechain=True),
        ])
        self.assertEqual((result["model"], result["context_tokens"]), ("claude-opus-5-5", 505))

    def test_tool_use_only_line_counts(self):
        result = self.parse([
            _user("start"),
            _usage_line("claude-opus-5-5", "2026-01-01T00:01:00.000Z", _usage(5, 500),
                        text="Let me look."),
            _usage_line("claude-opus-5-5", "2026-01-01T00:02:00.000Z", _usage(7, 900)),
        ])
        self.assertEqual(result["context_tokens"], 907)
        self.assertEqual(result["context_at"], "2026-01-01T00:02:00.000Z")
        self.assertEqual(result["last_assistant"], "Let me look.")

    def test_synthetic_api_error_line_yields_to_the_older_real_line(self):
        result = self.parse([
            _user("start"),
            _usage_line("claude-opus-5-5", "2026-01-01T00:01:00.000Z", _usage(5, 500),
                        text="working"),
            _synthetic_error_line("2026-01-01T00:02:00.000Z"),
        ])
        self.assertEqual(result["model"], "claude-opus-5-5")
        self.assertEqual(result["context_tokens"], 505)
        self.assertEqual(result["context_at"], "2026-01-01T00:01:00.000Z")

    def test_synthetic_then_custom_title_then_real_line(self):
        # Read backwards: the synthetic line sets last_assistant, the ai-title
        # and the user prompt complete what hunt mode waits for, and the
        # custom title would trigger the early break; both are gated on the
        # real assistant line having been examined.
        result = self.parse([
            _user("start"),
            _usage_line("claude-opus-5-5", "2026-01-01T00:01:00.000Z", _usage(5, 500),
                        text="real answer"),
            {"type": "custom-title", "customTitle": "Named"},
            _user("last", "2026-01-01T00:01:30.000Z"),
            {"type": "ai-title", "aiTitle": "AI"},
            _synthetic_error_line("2026-01-01T00:02:00.000Z"),
        ])
        self.assertEqual((result["title"], result["title_source"]), ("Named", "custom"))
        self.assertEqual(result["model"], "claude-opus-5-5")
        self.assertEqual(result["context_tokens"], 505)

    def test_newest_real_line_without_usage_sets_model_only(self):
        result = self.parse([
            _user("start"),
            _usage_line("claude-sonnet-4-5", "2026-01-01T00:01:00.000Z", _usage(5, 500),
                        text="older"),
            _usage_line("claude-opus-5-5", "2026-01-01T00:02:00.000Z", None, text="newer"),
        ])
        self.assertEqual(result["model"], "claude-opus-5-5")
        self.assertIsNone(result["context_tokens"])
        self.assertIsNone(result["context_at"])

    def test_partial_and_malformed_usage(self):
        result = self.parse([
            _user("start"),
            _usage_line("claude-opus-5-5", "2026-01-01T00:01:00.000Z",
                        {"input_tokens": 12, "cache_read_input_tokens": "x",
                         "cache_creation_input_tokens": True}, text="a"),
        ])
        self.assertEqual(result["context_tokens"], 12)
        result = self.parse([
            _user("start"),
            _usage_line("claude-opus-5-5", "2026-01-01T00:01:00.000Z",
                        {"output_tokens": 9}, text="a"),
        ])
        self.assertIsNone(result["context_tokens"])

    def test_transcript_without_assistant_usage(self):
        result = self.parse([_user("start"), _assistant("plain")])
        self.assertIsNone(result["model"])
        self.assertIsNone(result["context_tokens"])


class ModelContextUpsertTests(AgentBoardTestCase):
    def _fields(self, **overrides):
        fields = {col: None for col in backfill.BACKFILL_COLUMNS}
        fields.update(cwd="/tmp", interactive=1)
        fields.update(overrides)
        return fields

    def _row(self):
        return self.conn.execute("SELECT * FROM sessions WHERE session_id='s1'").fetchone()

    def _insert(self, origin, model, tokens, at):
        self.conn.execute(
            "INSERT INTO sessions(tool, session_id, origin, model, context_tokens, context_at) "
            "VALUES ('claude', 's1', ?, ?, ?, ?)", (origin, model, tokens, at))

    def test_insert_writes_parsed_values(self):
        backfill.upsert_backfill_session(self.conn, "claude", "s1", self._fields(
            model="claude-opus-5-5", context_tokens=1200,
            context_at="2026-01-01T00:02:00.000Z"))
        row = self._row()
        self.assertEqual((row["model"], row["context_tokens"], row["context_at"]),
                         ("claude-opus-5-5", 1200, "2026-01-01T00:02:00.000Z"))

    def test_newer_measurement_replaces_on_both_origins(self):
        for origin in ("hook", "backfill"):
            with self.subTest(origin=origin):
                self.conn.execute("DELETE FROM sessions")
                self._insert(origin, "claude-sonnet-4-5", 100, "2026-01-01T00:01:00.000000Z")
                backfill.upsert_backfill_session(self.conn, "claude", "s1", self._fields(
                    model="claude-opus-5-5", context_tokens=900,
                    context_at="2026-01-01T00:05:00.000Z"))
                row = self._row()
                self.assertEqual((row["model"], row["context_tokens"], row["context_at"]),
                                 ("claude-opus-5-5", 900, "2026-01-01T00:05:00.000Z"))

    def test_older_measurement_never_replaces_on_both_origins(self):
        # A hook-stored resume context (microseconds) newer than the
        # transcript's newest assistant line (milliseconds) survives.
        for origin in ("hook", "backfill"):
            with self.subTest(origin=origin):
                self.conn.execute("DELETE FROM sessions")
                self._insert(origin, "claude-opus-5-5", 308011, "2026-01-01T00:05:00.000500Z")
                backfill.upsert_backfill_session(self.conn, "claude", "s1", self._fields(
                    model="claude-opus-5-5", context_tokens=1200,
                    context_at="2026-01-01T00:05:00.000Z"))
                row = self._row()
                self.assertEqual((row["context_tokens"], row["context_at"]),
                                 (308011, "2026-01-01T00:05:00.000500Z"))

    def test_a_stored_future_time_never_freezes_the_context(self):
        # No real measurement is in the future; keeping one would block
        # every later size for good.
        for origin in ("hook", "backfill"):
            with self.subTest(origin=origin):
                self.conn.execute("DELETE FROM sessions")
                self._insert(origin, "claude-opus-5-5", 900, "2099-01-01T00:00:00Z")
                backfill.upsert_backfill_session(self.conn, "claude", "s1", self._fields(
                    model="claude-opus-5-5", context_tokens=1200,
                    context_at="2026-01-01T00:05:00.000Z"))
                row = self._row()
                self.assertEqual((row["context_tokens"], row["context_at"]),
                                 (1200, "2026-01-01T00:05:00.000Z"))

    def test_equal_time_rewrites(self):
        self._insert("hook", None, 5, "2026-01-01T00:05:00.000Z")
        backfill.upsert_backfill_session(self.conn, "claude", "s1", self._fields(
            context_tokens=7, context_at="2026-01-01T00:05:00.000000Z"))
        self.assertEqual(self._row()["context_tokens"], 7)

    def test_missing_parse_never_clears_on_both_origins(self):
        for origin in ("hook", "backfill"):
            with self.subTest(origin=origin):
                self.conn.execute("DELETE FROM sessions")
                self._insert(origin, "claude-opus-5-5", 308011, "2026-01-01T00:05:00.000Z")
                backfill.upsert_backfill_session(self.conn, "claude", "s1", self._fields())
                row = self._row()
                self.assertEqual((row["model"], row["context_tokens"], row["context_at"]),
                                 ("claude-opus-5-5", 308011, "2026-01-01T00:05:00.000Z"))

    def test_backfill_origin_row_survives_a_reparse_without_usage(self):
        path = self.tmp_path / "s1.jsonl"
        write_jsonl(path, [_user("start"), _assistant("plain")])
        self._insert("backfill", "claude-opus-5-5", 308011, "2026-01-01T00:05:00.000Z")
        backfill.upsert_backfill_session(self.conn, "claude", "s1",
                                         backfill.parse_claude_transcript(path))
        row = self._row()
        self.assertEqual((row["model"], row["context_tokens"], row["context_at"]),
                         ("claude-opus-5-5", 308011, "2026-01-01T00:05:00.000Z"))
        self.assertEqual(row["first_prompt"], "start")

    def test_session_backfill_end_to_end(self):
        path = self.tmp_path / "e2e.jsonl"
        write_jsonl(path, [
            _user("start"),
            _usage_line("claude-opus-5-5", "2026-01-01T00:01:00.000Z", _usage(4, 400, 40),
                        text="done"),
        ])
        # The hook recorded the session first, with the startup model.
        self.conn.execute(
            "INSERT INTO sessions(tool, session_id, origin, model, transcript_path) "
            "VALUES ('claude', 'e2e', 'hook', 'claude-sonnet-4-5', ?)", (str(path),))
        self.assertEqual(backfill.backfill_single_session(self.conn, "claude", "e2e"),
                         "updated")
        row = self.conn.execute("SELECT * FROM sessions WHERE session_id='e2e'").fetchone()
        self.assertEqual((row["model"], row["context_tokens"], row["context_at"]),
                         ("claude-opus-5-5", 444, "2026-01-01T00:01:00.000Z"))



# Codex rollout lines as codex-rs writes them: RolloutLine is
# {"timestamp", "type", "payload"} (RolloutItemWire, tag "type", snake_case);
# TurnContextItem carries `model: String`; EventMsg::TokenCount is
# {"type": "token_count", "info": TokenUsageInfo | null, "rate_limits": ...}
# with TokenUsageInfo {total_token_usage, last_token_usage,
# model_context_window} and TokenUsage {input_tokens, cached_input_tokens,
# cache_write_input_tokens, output_tokens, reasoning_output_tokens,
# total_tokens}, where input_tokens includes cached_input_tokens
# (TokenUsage::non_cached_input subtracts it). Both are persisted
# (codex-rs/rollout/src/policy.rs).
def _codex_meta():
    return {"type": "session_meta", "timestamp": "2026-01-01T00:00:00.000Z",
            "payload": {"id": "cx-1", "cwd": "/tmp/proj", "source": "cli",
                        "timestamp": "2026-01-01T00:00:00.000Z"}}


def _codex_turn_context(model, ts):
    return {"timestamp": ts, "type": "turn_context",
            "payload": {"cwd": "/tmp/proj", "approval_policy": "on-request",
                        "sandbox_policy": {"type": "workspace-write"}, "model": model,
                        "effort": "medium", "summary": "auto"}}


def _codex_msg(ptype, ts, **fields):
    return {"timestamp": ts, "type": "event_msg", "payload": {"type": ptype, **fields}}


def _codex_token_count(ts, input_tokens, cached, output=200):
    usage = {"input_tokens": input_tokens, "cached_input_tokens": cached,
             "cache_write_input_tokens": 0, "output_tokens": output,
             "reasoning_output_tokens": 64, "total_tokens": input_tokens + output}
    return _codex_msg("token_count", ts, info={
        "total_token_usage": dict(usage, input_tokens=input_tokens * 3),
        "last_token_usage": usage, "model_context_window": 272000,
    }, rate_limits=None)


def _codex_turn(model, start, input_tokens, cached, prompt="go", reply="done"):
    return [
        _codex_turn_context(model, f"2026-01-01T00:{start:02d}:00.000Z"),
        _codex_msg("user_message", f"2026-01-01T00:{start:02d}:01.000Z", message=prompt),
        _codex_msg("agent_message", f"2026-01-01T00:{start:02d}:30.000Z", message=reply),
        _codex_token_count(f"2026-01-01T00:{start:02d}:31.000Z", input_tokens, cached),
        _codex_msg("task_complete", f"2026-01-01T00:{start:02d}:32.000Z"),
    ]


class CodexModelContextTests(AgentBoardTestCase):
    def parse(self, entries):
        path = self.tmp_path / "rollout-cx.jsonl"
        write_jsonl(path, entries)
        return backfill.parse_codex_rollout(path)

    def test_newest_turn_context_and_token_count_win(self):
        result = self.parse([_codex_meta()]
                            + _codex_turn("gpt-5", 1, 9000, 8000)
                            + _codex_turn("gpt-5-codex", 2, 45000, 40000, prompt="more"))
        self.assertEqual(result["model"], "gpt-5-codex")
        self.assertEqual(result["context_tokens"], 45000, "input_tokens alone, never + cached")
        self.assertEqual(result["context_at"], "2026-01-01T00:02:31.000Z")
        self.assertEqual(result["last_prompt"], "more")

    def test_token_count_without_info_is_skipped(self):
        entries = [_codex_meta()] + _codex_turn("gpt-5", 1, 9000, 8000)
        entries.append(_codex_msg("token_count", "2026-01-01T00:01:40.000Z", info=None,
                                  rate_limits={"primary": {"used_percent": 10}}))
        result = self.parse(entries)
        self.assertEqual(result["context_tokens"], 9000)

    def test_rollout_without_either_leaves_both_none(self):
        result = self.parse([
            _codex_meta(),
            _codex_msg("user_message", "2026-01-01T00:00:01.000Z", message="go"),
            _codex_msg("agent_message", "2026-01-01T00:00:30.000Z", message="done"),
        ])
        self.assertIsNone(result["model"])
        self.assertIsNone(result["context_tokens"])
        self.assertIsNone(result["context_at"])
        self.assertEqual(result["last_assistant"], "done")

    def test_usage_hunt_is_bounded(self):
        filler = [_codex_msg("agent_reasoning", "2026-01-01T00:01:10.000Z",
                             text="x" * 300) for _ in range(200)]
        entries = ([_codex_meta(), _codex_turn_context("gpt-5", "2026-01-01T00:01:00.000Z")]
                   + filler
                   + [_codex_msg("user_message", "2026-01-01T00:01:01.000Z", message="go"),
                      _codex_msg("agent_message", "2026-01-01T00:01:30.000Z", message="ok")])
        with mock.patch.object(backfill, "CUSTOM_TITLE_SCAN_BYTES", 10_000):
            result = self.parse(entries)
        self.assertIsNone(result["model"], "past the budget the hunt gives up")
        self.assertEqual(result["last_prompt"], "go")
        result = self.parse(entries)
        self.assertEqual(result["model"], "gpt-5")

    def test_upsert_does_not_clear_existing_values(self):
        self.conn.execute(
            "INSERT INTO sessions(tool, session_id, origin, model, context_tokens, context_at) "
            "VALUES ('codex', 'cx-1', 'hook', 'gpt-5', 1000, '2026-01-01T00:00:31.000Z')")
        fields = self.parse([
            _codex_meta(),
            _codex_msg("user_message", "2026-01-01T00:00:01.000Z", message="go"),
            _codex_msg("agent_message", "2026-01-01T00:00:30.000Z", message="done"),
        ])
        backfill.upsert_backfill_session(self.conn, "codex", "cx-1", fields)
        row = self.conn.execute("SELECT * FROM sessions WHERE session_id='cx-1'").fetchone()
        self.assertEqual((row["model"], row["context_tokens"]), ("gpt-5", 1000))



# codex-rs protocol RateLimitSnapshot: limit_id ("codex" for the plan's own
# limit), primary/secondary RateLimitWindow {used_percent, window_minutes,
# resets_at (epoch s)}, credits, plan_type; null under API-key auth.
def _codex_rate_limits(primary_pct, secondary_pct, limit_id="codex"):
    return {
        "limit_id": limit_id, "limit_name": None,
        "primary": {"used_percent": primary_pct, "window_minutes": 300,
                    "resets_at": 1767240000},
        "secondary": {"used_percent": secondary_pct, "window_minutes": 10080,
                      "resets_at": 1767600000},
        "credits": {"has_credits": False, "unlimited": False, "balance": None},
        "plan_type": "plus",
    }


def _codex_turn_with_limits(start, limits, ts_day="01"):
    turn = _codex_turn("gpt-5-codex", start, 9000, 8000)
    turn[3] = dict(turn[3], timestamp=f"2026-01-{ts_day}T00:{start:02d}:31.000Z",
                   payload=dict(turn[3]["payload"], rate_limits=limits))
    return turn


class CodexPlanUsageTests(AgentBoardTestCase):
    def rollout(self, name, entries):
        path = self.tmp_path / name
        write_jsonl(path, entries)
        return path

    def usage_file(self):
        return self.agentboard_home / "usage" / "codex.json"

    def test_newest_token_count_rate_limits_become_a_usage_source(self):
        path = self.rollout("r.jsonl", [_codex_meta()]
                            + _codex_turn_with_limits(1, _codex_rate_limits(10, 20))
                            + _codex_turn_with_limits(2, _codex_rate_limits(12.5, 21)))
        self.assertEqual(backfill.parse_codex_rollout(path)["plan_usage"], {
            "label": "Codex",
            "updated_at": "2026-01-01T00:02:31Z",
            "windows": [
                {"label": "5h", "used_percentage": 12.5, "resets_at": "2026-01-01T04:00:00Z"},
                {"label": "7d", "used_percentage": 21.0, "resets_at": "2026-01-05T08:00:00Z"},
            ],
        })

    def test_deeply_nested_lines_are_skipped_not_raised(self):
        # json.loads raises RecursionError, not ValueError, on deep nesting.
        nested = "[" * 20000 + "]" * 20000
        plan_call = {"timestamp": "2026-01-01T00:01:35.000Z", "type": "response_item",
                     "payload": {"type": "function_call", "name": "update_plan",
                                 "arguments": nested}}
        path = self.tmp_path / "r.jsonl"
        lines = [json.dumps(e) for e in [_codex_meta()]
                 + _codex_turn_with_limits(1, _codex_rate_limits(10, 20))]
        lines += [nested, json.dumps(plan_call)]
        lines += [json.dumps(e) for e in _codex_turn_with_limits(2, _codex_rate_limits(12.5, 21))]
        lines.append(nested)
        path.write_text("\n".join(lines) + "\n")
        result = backfill.parse_codex_rollout(path)
        self.assertEqual(result["model"], "gpt-5-codex")
        self.assertEqual(result["context_tokens"], 9000)
        self.assertEqual(result["plan_usage"]["windows"][0]["used_percentage"], 12.5)
        self.assertIsNone(backfill._compute_codex_todos_open(path))

    def test_out_of_range_event_time_gives_no_plan_usage(self):
        turn = _codex_turn_with_limits(1, _codex_rate_limits(10, 20))
        turn[3] = dict(turn[3], timestamp="9999-12-31T23:59:59-05:00")
        path = self.rollout("r.jsonl", [_codex_meta()] + turn)
        self.assertIsNone(backfill.parse_codex_rollout(path)["plan_usage"])

    def test_api_key_rollout_has_no_plan_usage_and_writes_nothing(self):
        path = self.rollout("r.jsonl", [_codex_meta()] + _codex_turn("gpt-5", 1, 9000, 8000))
        self.assertIsNone(backfill.parse_codex_rollout(path)["plan_usage"])
        backfill.backfill_single_session(self.conn, "codex", "cx-1", transcript_path=str(path))
        self.assertFalse(self.usage_file().exists())

    def test_other_limit_ids_are_ignored(self):
        path = self.rollout("r.jsonl", [_codex_meta()] + _codex_turn_with_limits(
            1, _codex_rate_limits(50, 60, limit_id="codex_other")))
        self.assertIsNone(backfill.parse_codex_rollout(path)["plan_usage"])

    def test_single_session_backfill_writes_and_never_goes_back_in_time(self):
        newer = self.rollout("a.jsonl", [_codex_meta()] + _codex_turn_with_limits(
            1, _codex_rate_limits(30, 40), ts_day="02"))
        older = self.rollout("b.jsonl", [_codex_meta()] + _codex_turn_with_limits(
            1, _codex_rate_limits(99, 99), ts_day="01"))
        backfill.backfill_single_session(self.conn, "codex", "cx-1", transcript_path=str(newer))
        backfill.backfill_single_session(self.conn, "codex", "cx-1", transcript_path=str(older))
        data = json.loads(self.usage_file().read_text())
        self.assertEqual(data["windows"][0]["used_percentage"], 30.0)
        row = self.conn.execute("SELECT * FROM sessions WHERE session_id='cx-1'").fetchone()
        self.assertIsNotNone(row, "plan_usage is not a column and does not break the upsert")

    def test_full_backfill_writes_the_newest_across_rollouts(self):
        base = self.fake_home / ".codex" / "sessions" / "2026" / "01" / "01"
        base.mkdir(parents=True)
        for name, pct, day in (("rollout-a.jsonl", 70, "02"), ("rollout-b.jsonl", 5, "01")):
            meta = _codex_meta()
            meta["payload"] = dict(meta["payload"], id=name)
            write_jsonl(base / name, [meta] + _codex_turn_with_limits(
                1, _codex_rate_limits(pct, pct), ts_day=day))
        with mock.patch.object(backfill.usage_mod, "record_source",
                               wraps=backfill.usage_mod.record_source) as record:
            stats = backfill.run_backfill(self.conn, days=100000)
        self.assertEqual(stats["codex"], 2)
        self.assertEqual(record.call_count, 1)
        data = json.loads(self.usage_file().read_text())
        self.assertEqual(data["windows"][0]["used_percentage"], 70.0)

if __name__ == "__main__":
    import unittest

    unittest.main()
