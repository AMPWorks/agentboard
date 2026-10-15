import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentboard import reconcile
from tests.helpers import AgentBoardTestCase

NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def base_session(**overrides):
    session = {
        "tool": "claude",
        "session_id": "s1",
        "last_event": None,
        "last_event_at": iso(NOW),
        "waiting_reason": None,
        "updated_at": iso(NOW),
        "ended_at": None,
        "pid": None,
    }
    session.update(overrides)
    return session


class ComputeStateTests(AgentBoardTestCase):
    def test_needs_you_permission_request(self):
        session = base_session(last_event="PermissionRequest", waiting_reason="Bash")
        state, reason, _auto_done, _done_reason = reconcile.compute_state(session, None, [], now=NOW, alive=True)
        self.assertEqual(state, "needs_you")
        self.assertEqual(reason, "Bash")

    def test_needs_you_stop_failure(self):
        session = base_session(last_event="StopFailure", waiting_reason="boom")
        state, _, _auto_done, _done_reason = reconcile.compute_state(session, None, [], now=NOW, alive=True)
        self.assertEqual(state, "needs_you")

    def test_needs_you_notification_permission_prompt(self):
        session = base_session(last_event="Notification", waiting_reason="please confirm")
        state, _, _auto_done, _done_reason = reconcile.compute_state(
            session, None, [], now=NOW, alive=True, notification_type="permission_prompt"
        )
        self.assertEqual(state, "needs_you")

    def test_notification_idle_prompt_is_review_not_needs_you(self):
        session = base_session(last_event="Notification", waiting_reason="idle")
        state, _, _auto_done, _done_reason = reconcile.compute_state(
            session, None, [], now=NOW, alive=True, notification_type="idle_prompt"
        )
        self.assertEqual(state, "review")

    def test_running_activity_event_alive(self):
        session = base_session(last_event="PreToolUse")
        state, _, _auto_done, _done_reason = reconcile.compute_state(session, None, [], now=NOW, alive=True)
        self.assertEqual(state, "running")

    def test_running_no_pid_fresh(self):
        session = base_session(last_event="PreToolUse", last_event_at=iso(NOW - timedelta(minutes=5)))
        state, _, _auto_done, _done_reason = reconcile.compute_state(session, None, [], now=NOW, alive=None)
        self.assertEqual(state, "running")

    def test_interrupted_dead_pid(self):
        session = base_session(last_event="PreToolUse", pid=12345)
        state, _, _auto_done, _done_reason = reconcile.compute_state(session, None, [], now=NOW, alive=False)
        self.assertEqual(state, "interrupted")

    def test_interrupted_stale_no_pid(self):
        session = base_session(
            last_event="PreToolUse", last_event_at=iso(NOW - timedelta(minutes=45))
        )
        state, _, _auto_done, _done_reason = reconcile.compute_state(session, None, [], now=NOW, alive=None)
        self.assertEqual(state, "interrupted")

    def test_interrupted_dead_pid_within_24h_stays_interrupted(self):
        session = base_session(
            last_event="PreToolUse", pid=12345, last_event_at=iso(NOW - timedelta(hours=1))
        )
        state, reason, _auto_done, _done_reason = reconcile.compute_state(session, None, [], now=NOW, alive=False)
        self.assertEqual(state, "interrupted")
        self.assertIsNone(reason)

    def test_dead_pid_older_than_24h_becomes_unconcluded(self):
        session = base_session(
            last_event="PreToolUse", pid=12345, last_event_at=iso(NOW - timedelta(days=3))
        )
        state, reason, _auto_done, _done_reason = reconcile.compute_state(session, None, [], now=NOW, alive=False)
        self.assertEqual(state, "unconcluded")
        self.assertEqual(reason, "interrupted")

    def test_stale_no_pid_older_than_24h_becomes_unconcluded(self):
        session = base_session(
            last_event="PreToolUse", last_event_at=iso(NOW - timedelta(days=10))
        )
        state, reason, _auto_done, _done_reason = reconcile.compute_state(session, None, [], now=NOW, alive=None)
        self.assertEqual(state, "unconcluded")
        self.assertEqual(reason, "interrupted")

    def test_dead_pid_unknown_age_does_not_crowd_interrupted(self):
        session = base_session(last_event="PreToolUse", pid=12345, last_event_at=None)
        state, reason, _auto_done, _done_reason = reconcile.compute_state(session, None, [], now=NOW, alive=False)
        self.assertEqual(state, "unconcluded")
        self.assertEqual(reason, "interrupted")

    def test_ambiguous_activity_falls_through_to_unconcluded(self):
        # No pid, 20 minutes old: not fresh enough for running, not stale
        # enough for interrupted.
        session = base_session(
            last_event="PreToolUse",
            last_event_at=iso(NOW - timedelta(minutes=20)),
            updated_at=iso(NOW - timedelta(minutes=20)),
        )
        state, _, _auto_done, _done_reason = reconcile.compute_state(session, None, [], now=NOW, alive=None)
        self.assertEqual(state, "unconcluded")

    def test_review_recent_stop(self):
        session = base_session(last_event="Stop", updated_at=iso(NOW - timedelta(hours=1)))
        state, _, _auto_done, _done_reason = reconcile.compute_state(session, None, [], now=NOW, alive=False)
        self.assertEqual(state, "review")

    def test_unconcluded_old_stop(self):
        session = base_session(last_event="Stop", updated_at=iso(NOW - timedelta(hours=48)))
        state, _, _auto_done, _done_reason = reconcile.compute_state(session, None, [], now=NOW, alive=False)
        self.assertEqual(state, "unconcluded")

    def test_unconcluded_catch_all_for_unknown_last_event(self):
        session = base_session(last_event="SessionStart")
        state, _, _auto_done, _done_reason = reconcile.compute_state(session, None, [], now=NOW, alive=None)
        self.assertEqual(state, "unconcluded")

    def test_done_via_triage(self):
        session = base_session(last_event="PermissionRequest", waiting_reason="x")
        triage = {"status": "done", "until": None}
        state, _, _auto_done, _done_reason = reconcile.compute_state(session, triage, [], now=NOW, alive=True)
        self.assertEqual(state, "done")

    def test_done_via_all_pr_links_merged(self):
        session = base_session(last_event="Stop", updated_at=iso(NOW - timedelta(hours=100)))
        links = [{"kind": "pr", "ref": "#1", "state": "MERGED"}]
        state, _, _auto_done, _done_reason = reconcile.compute_state(session, None, links, now=NOW, alive=False)
        self.assertEqual(state, "done")

    def test_not_done_if_one_pr_link_still_open(self):
        session = base_session(last_event="Stop", updated_at=iso(NOW - timedelta(hours=1)))
        links = [
            {"kind": "pr", "ref": "#1", "state": "MERGED"},
            {"kind": "pr", "ref": "#2", "state": "OPEN"},
        ]
        state, _, _auto_done, _done_reason = reconcile.compute_state(session, None, links, now=NOW, alive=False)
        self.assertNotEqual(state, "done")

    def test_done_via_any_link_state_done(self):
        session = base_session(last_event="Stop", updated_at=iso(NOW - timedelta(hours=1)))
        links = [{"kind": "ticket", "ref": "ABC-1", "state": "DONE"}]
        state, _, _auto_done, _done_reason = reconcile.compute_state(session, None, links, now=NOW, alive=False)
        self.assertEqual(state, "done")

    def test_snoozed_until_future(self):
        session = base_session(last_event="Stop")
        triage = {"status": "snoozed", "until": iso(NOW + timedelta(hours=2))}
        state, _, _auto_done, _done_reason = reconcile.compute_state(session, triage, [], now=NOW, alive=False)
        self.assertEqual(state, "snoozed")

    def test_snooze_expired_falls_through(self):
        session = base_session(last_event="Stop", updated_at=iso(NOW - timedelta(hours=1)))
        triage = {"status": "snoozed", "until": iso(NOW - timedelta(hours=1))}
        state, _, _auto_done, _done_reason = reconcile.compute_state(session, triage, [], now=NOW, alive=False)
        self.assertEqual(state, "review")

    def test_registry_fallback_needs_you_when_no_last_event(self):
        session = base_session(last_event=None)
        registry_entry = {"status": "waiting", "waitingFor": "input needed"}
        state, reason, _auto_done, _done_reason = reconcile.compute_state(
            session, None, [], now=NOW, alive=True, registry_entry=registry_entry
        )
        self.assertEqual(state, "needs_you")
        self.assertEqual(reason, "input needed")

    def test_needs_you_tool_sentinel(self):
        from agentboard.hook import NEEDS_YOU_TOOL_SENTINEL

        session = base_session(last_event=NEEDS_YOU_TOOL_SENTINEL, waiting_reason="question")
        state, reason, _auto_done, _done_reason = reconcile.compute_state(session, None, [], now=NOW, alive=True)
        self.assertEqual(state, "needs_you")
        self.assertEqual(reason, "question")

    def test_running_activity_beats_links_done(self):
        session = base_session(last_event="PreToolUse")
        links = [{"kind": "pr", "ref": "#1", "state": "MERGED"}]
        state, _, _auto_done, _done_reason = reconcile.compute_state(session, None, links, now=NOW, alive=True)
        self.assertEqual(state, "running")

    def test_needs_you_beats_links_done(self):
        session = base_session(last_event="PermissionRequest", waiting_reason="Bash")
        links = [{"kind": "pr", "ref": "#1", "state": "MERGED"}]
        state, _, _auto_done, _done_reason = reconcile.compute_state(session, None, links, now=NOW, alive=True)
        self.assertEqual(state, "needs_you")

    def test_interrupted_beats_links_done(self):
        session = base_session(last_event="PreToolUse", pid=12345)
        links = [{"kind": "pr", "ref": "#1", "state": "MERGED"}]
        state, _, _auto_done, _done_reason = reconcile.compute_state(session, None, links, now=NOW, alive=False)
        self.assertEqual(state, "interrupted")

    def test_links_done_applies_once_idle_again(self):
        session = base_session(last_event="Stop", updated_at=iso(NOW - timedelta(hours=1)))
        links = [{"kind": "pr", "ref": "#1", "state": "MERGED"}]
        state, _, auto_done, done_reason = reconcile.compute_state(session, None, links, now=NOW, alive=False)
        self.assertEqual(state, "done")
        self.assertTrue(auto_done)
        self.assertEqual(done_reason, "pr merged")

    def test_link_done_reason_label(self):
        session = base_session(last_event="Stop", updated_at=iso(NOW - timedelta(hours=1)))
        links = [{"kind": "url", "ref": "x", "state": "DONE"}]
        state, _, auto_done, done_reason = reconcile.compute_state(session, None, links, now=NOW, alive=False)
        self.assertEqual(state, "done")
        self.assertTrue(auto_done)
        self.assertEqual(done_reason, "link done")

    def test_manual_done_reason_label(self):
        session = base_session(last_event="PermissionRequest", waiting_reason="x")
        triage = {"status": "done"}
        state, _, auto_done, done_reason = reconcile.compute_state(session, triage, [], now=NOW, alive=True)
        self.assertEqual(state, "done")
        self.assertFalse(auto_done)
        self.assertEqual(done_reason, "manual")


def clean_exit_session(tool="claude", end_reason="prompt_input_exit", todos_open=None, **overrides):
    session = base_session(
        tool=tool, last_event="SessionEnd", end_reason=end_reason, todos_open=todos_open,
        ended_at=iso(NOW - timedelta(minutes=5)), updated_at=iso(NOW - timedelta(minutes=5)),
    )
    session.update(overrides)
    return session


class AutoDoneTests(AgentBoardTestCase):
    def test_all_conditions_hold_is_clean_exit_done(self):
        session = clean_exit_session()
        state, reason, auto_done, done_reason = reconcile.compute_state(
            session, None, [], now=NOW, alive=False, turn_finished=True,
        )
        self.assertEqual(state, "done")
        self.assertIsNone(reason)
        self.assertTrue(auto_done)
        self.assertEqual(done_reason, "clean exit")

    def test_claude_clear_reason_is_not_clean_exit(self):
        session = clean_exit_session(end_reason="clear")
        state, _, auto_done, done_reason = reconcile.compute_state(
            session, None, [], now=NOW, alive=False, turn_finished=True,
        )
        self.assertNotEqual(state, "done")
        self.assertFalse(auto_done)
        self.assertIsNone(done_reason)

    def test_claude_resume_reason_is_not_clean_exit(self):
        session = clean_exit_session(end_reason="resume")
        state, _, auto_done, _ = reconcile.compute_state(
            session, None, [], now=NOW, alive=False, turn_finished=True,
        )
        self.assertNotEqual(state, "done")
        self.assertFalse(auto_done)

    def test_claude_other_reason_is_not_clean_exit(self):
        session = clean_exit_session(end_reason="other")
        state, _, auto_done, _ = reconcile.compute_state(
            session, None, [], now=NOW, alive=False, turn_finished=True,
        )
        self.assertNotEqual(state, "done")
        self.assertFalse(auto_done)

    def test_claude_logout_is_clean_exit(self):
        session = clean_exit_session(end_reason="logout")
        state, _, auto_done, done_reason = reconcile.compute_state(
            session, None, [], now=NOW, alive=False, turn_finished=True,
        )
        self.assertEqual(state, "done")
        self.assertEqual(done_reason, "clean exit")

    def test_codex_any_session_end_is_clean_exit(self):
        session = clean_exit_session(tool="codex", end_reason="other")
        state, _, auto_done, done_reason = reconcile.compute_state(
            session, None, [], now=NOW, alive=False, turn_finished=True,
        )
        self.assertEqual(state, "done")
        self.assertTrue(auto_done)
        self.assertEqual(done_reason, "clean exit")

    def test_turn_not_finished_blocks_auto_done(self):
        session = clean_exit_session()
        state, _, auto_done, _ = reconcile.compute_state(
            session, None, [], now=NOW, alive=False, turn_finished=False,
        )
        self.assertNotEqual(state, "done")
        self.assertFalse(auto_done)

    def test_open_todos_block_auto_done(self):
        session = clean_exit_session(todos_open=2)
        state, _, auto_done, _ = reconcile.compute_state(
            session, None, [], now=NOW, alive=False, turn_finished=True,
        )
        self.assertNotEqual(state, "done")
        self.assertFalse(auto_done)

    def test_zero_todos_open_does_not_block(self):
        session = clean_exit_session(todos_open=0)
        state, _, auto_done, _ = reconcile.compute_state(
            session, None, [], now=NOW, alive=False, turn_finished=True,
        )
        self.assertEqual(state, "done")
        self.assertTrue(auto_done)

    def test_unknown_todos_open_does_not_block(self):
        session = clean_exit_session(todos_open=None)
        state, _, auto_done, _ = reconcile.compute_state(
            session, None, [], now=NOW, alive=False, turn_finished=True,
        )
        self.assertEqual(state, "done")

    def test_open_pr_link_blocks_auto_done(self):
        session = clean_exit_session()
        links = [{"kind": "pr", "ref": "#1", "state": "OPEN"}]
        state, _, auto_done, _ = reconcile.compute_state(
            session, None, links, now=NOW, alive=False, turn_finished=True,
        )
        self.assertNotEqual(state, "done")
        self.assertFalse(auto_done)

    def test_closed_pr_link_does_not_block(self):
        session = clean_exit_session()
        links = [{"kind": "pr", "ref": "#1", "state": "CLOSED"}]
        state, _, auto_done, done_reason = reconcile.compute_state(
            session, None, links, now=NOW, alive=False, turn_finished=True,
        )
        self.assertEqual(state, "done")
        # all pr links are closed, so the simpler "pr merged" rule already
        # claims this one -- clean exit never even needs to be checked.
        self.assertEqual(done_reason, "pr merged")

    def test_not_yet_ended_session_never_auto_done(self):
        session = clean_exit_session(ended_at=None)
        state, _, auto_done, _ = reconcile.compute_state(
            session, None, [], now=NOW, alive=False, turn_finished=True,
        )
        self.assertNotEqual(state, "done")
        self.assertFalse(auto_done)

    def test_manual_done_takes_priority_over_clean_exit(self):
        session = clean_exit_session()
        triage = {"status": "done"}
        state, _, auto_done, done_reason = reconcile.compute_state(
            session, triage, [], now=NOW, alive=False, turn_finished=True,
        )
        self.assertEqual(state, "done")
        self.assertFalse(auto_done)
        self.assertEqual(done_reason, "manual")

    def test_mark_open_after_end_suppresses_auto_done(self):
        session = clean_exit_session()
        triage = {
            "status": "open",
            "updated_at": iso(NOW - timedelta(minutes=1)),  # after ended_at
        }
        state, _, auto_done, done_reason = reconcile.compute_state(
            session, triage, [], now=NOW, alive=False, turn_finished=True,
        )
        self.assertNotEqual(state, "done")
        self.assertFalse(auto_done)
        self.assertIsNone(done_reason)

    def test_mark_open_before_end_does_not_suppress_auto_done(self):
        # An "open" triage row from *before* the session ended (e.g. just
        # never touched) must not block a later, genuine clean exit.
        session = clean_exit_session()
        triage = {
            "status": "open",
            "updated_at": iso(NOW - timedelta(hours=1)),  # before ended_at
        }
        state, _, auto_done, done_reason = reconcile.compute_state(
            session, triage, [], now=NOW, alive=False, turn_finished=True,
        )
        self.assertEqual(state, "done")
        self.assertTrue(auto_done)
        self.assertEqual(done_reason, "clean exit")


class LastTurnFinishedTests(AgentBoardTestCase):
    def _insert_events(self, events):
        for i, event in enumerate(events):
            self.conn.execute(
                "INSERT INTO events(ts, tool, session_id, event, agent_id) "
                "VALUES (?,?,?,?,NULL)",
                (f"2026-01-01T00:00:{i:02d}.000000Z", "claude", "s1", event),
            )

    def test_stop_before_session_end_is_finished(self):
        self._insert_events(["SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"])
        self.assertTrue(reconcile.last_turn_finished(self.conn, "claude", "s1"))

    def test_interrupt_before_session_end_is_not_finished(self):
        self._insert_events(["SessionStart", "UserPromptSubmit", "PreToolUse", "Interrupt", "SessionEnd"])
        self.assertFalse(reconcile.last_turn_finished(self.conn, "claude", "s1"))

    def test_no_stop_at_all_is_not_finished(self):
        self._insert_events(["SessionStart", "UserPromptSubmit", "PreToolUse", "SessionEnd"])
        self.assertFalse(reconcile.last_turn_finished(self.conn, "claude", "s1"))

    def test_needs_you_after_stop_is_not_finished(self):
        self._insert_events(["UserPromptSubmit", "Stop", "PermissionRequest", "SessionEnd"])
        self.assertFalse(reconcile.last_turn_finished(self.conn, "claude", "s1"))

    def test_no_session_end_is_not_finished(self):
        self._insert_events(["UserPromptSubmit", "Stop"])
        self.assertFalse(reconcile.last_turn_finished(self.conn, "claude", "s1"))

    def test_subagent_events_are_ignored(self):
        self.conn.execute(
            "INSERT INTO events(ts, tool, session_id, event, agent_id) "
            "VALUES ('2026-01-01T00:00:00.000000Z','claude','s1','UserPromptSubmit',NULL)"
        )
        self.conn.execute(
            "INSERT INTO events(ts, tool, session_id, event, agent_id) "
            "VALUES ('2026-01-01T00:00:01.000000Z','claude','s1','Stop',NULL)"
        )
        # A subagent's own Interrupt must not count against the parent's
        # otherwise-clean turn.
        self.conn.execute(
            "INSERT INTO events(ts, tool, session_id, event, agent_id) "
            "VALUES ('2026-01-01T00:00:02.000000Z','claude','s1','Interrupt','sub-1')"
        )
        self.conn.execute(
            "INSERT INTO events(ts, tool, session_id, event, agent_id) "
            "VALUES ('2026-01-01T00:00:03.000000Z','claude','s1','SessionEnd',NULL)"
        )
        self.assertTrue(reconcile.last_turn_finished(self.conn, "claude", "s1"))


class LivenessTests(AgentBoardTestCase):
    def test_is_pid_alive_none(self):
        self.assertIsNone(reconcile.is_pid_alive(None))

    def test_is_pid_alive_self(self):
        import os

        self.assertTrue(reconcile.is_pid_alive(os.getpid()))

    def test_is_pid_alive_dead_pid(self):
        # Extremely unlikely to be a real pid.
        self.assertFalse(reconcile.is_pid_alive(2**30))

    def test_load_claude_session_registry(self):
        sessions_dir = self.fake_home / ".claude" / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        (sessions_dir / "4242.json").write_text(
            '{"pid": 4242, "sessionId": "abc", "status": "waiting", "waitingFor": "input needed"}'
        )
        (sessions_dir / "not-json.json").write_text("garbage")
        registry = reconcile.load_claude_session_registry(sessions_dir)
        self.assertEqual(registry["abc"]["pid"], 4242)
        self.assertEqual(registry["abc"]["waitingFor"], "input needed")
        self.assertNotIn("not-json", registry)

    def test_effective_liveness_prefers_known_pid(self):
        import os

        alive, pid = reconcile.effective_liveness("claude", os.getpid(), "abc", {})
        self.assertTrue(alive)
        self.assertEqual(pid, os.getpid())

    def test_effective_liveness_falls_back_to_registry(self):
        import os

        registry = {"abc": {"pid": os.getpid()}}
        alive, pid = reconcile.effective_liveness("claude", None, "abc", registry)
        self.assertTrue(alive)
        self.assertEqual(pid, os.getpid())

    def test_effective_liveness_unknown(self):
        alive, pid = reconcile.effective_liveness("claude", None, "abc", {})
        self.assertIsNone(alive)
        self.assertIsNone(pid)

    def test_codex_no_pid_alive_when_rollout_recently_modified(self):
        path = self.tmp_path / "rollout.jsonl"
        path.write_text("{}\n")
        alive, pid = reconcile.effective_liveness(
            "codex", None, "abc", {}, transcript_path=str(path), now=NOW
        )
        self.assertTrue(alive)
        self.assertIsNone(pid)

    def test_codex_no_pid_not_alive_when_rollout_stale(self):
        import os

        path = self.tmp_path / "rollout.jsonl"
        path.write_text("{}\n")
        stale = (NOW - timedelta(hours=1)).timestamp()
        os.utime(path, (stale, stale))
        alive, pid = reconcile.effective_liveness(
            "codex", None, "abc", {}, transcript_path=str(path), now=NOW
        )
        self.assertFalse(alive)
        self.assertIsNone(pid)

    def test_codex_no_pid_no_transcript_resolves_not_alive(self):
        # Unlike Claude's unknown-pid case (None/"ambiguous"), a pid-less
        # Codex session must resolve definitively -- stale/missing means
        # closed, never left ambiguous.
        alive, pid = reconcile.effective_liveness("codex", None, "abc", {}, now=NOW)
        self.assertFalse(alive)
        self.assertIsNone(pid)

    def test_codex_known_pid_still_takes_priority_over_rollout(self):
        import os

        alive, pid = reconcile.effective_liveness(
            "codex", os.getpid(), "abc", {}, transcript_path="/no/such/file", now=NOW
        )
        self.assertTrue(alive)
        self.assertEqual(pid, os.getpid())


if __name__ == "__main__":
    import unittest

    unittest.main()
