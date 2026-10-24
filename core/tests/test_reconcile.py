import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentboard import reconcile
from tests.helpers import AgentBoardTestCase

NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
CLOSED = "2026-06-15T09:00:00.000000Z"


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def merged_pr(ref="#1", branch="feature-x", state="MERGED", **extra):
    link = {"kind": "pr", "ref": ref, "state": state, "branch": branch,
            "closed_at": CLOSED if state in ("MERGED", "CLOSED") else None,
            "checked_at": CLOSED}
    link.update(extra)
    return link


def owned_open_pr(ref="#3", branch="feature-x", **extra):
    link = {"kind": "pr", "ref": ref, "state": "OPEN", "branch": branch, "source": "cli",
            "url": f"https://github.com/acme/widgets/pull/{ref.lstrip('#')}"}
    link.update(extra)
    return link


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
        session = base_session(last_event="Notification", waiting_reason="idle",
                               last_assistant="Which option do you want?")
        state, reason, _auto_done, _done_reason = reconcile.compute_state(
            session, None, [], now=NOW, alive=True, notification_type="idle_prompt"
        )
        self.assertEqual(state, "review")
        self.assertEqual(reason, "Your turn: Which option do you want?")

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
        session = base_session(last_event="Stop", updated_at=iso(NOW - timedelta(hours=1)),
                               last_assistant="\n\n**Done.** Tests pass.\nMore detail")
        state, reason, _auto_done, _done_reason = reconcile.compute_state(session, None, [], now=NOW, alive=False)
        self.assertEqual(state, "review")
        self.assertEqual(reason, "Your turn: Done. Tests pass.")

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
        session = base_session(last_event="Stop", updated_at=iso(NOW - timedelta(hours=100)),
                               branch="feature-x")
        links = [merged_pr()]
        state, _, _auto_done, _done_reason = reconcile.compute_state(session, None, links, now=NOW, alive=False)
        self.assertEqual(state, "done")

    def test_merged_pr_with_no_recorded_branch_does_not_retire(self):
        session = base_session(last_event="Stop", updated_at=iso(NOW - timedelta(hours=100)),
                               branch="feature-x")
        state, *_ = reconcile.compute_state(session, None, [merged_pr(branch=None)], now=NOW, alive=False)
        self.assertNotEqual(state, "done")

    def test_merged_pr_on_another_branch_does_not_retire(self):
        session = base_session(last_event="Stop", updated_at=iso(NOW - timedelta(hours=1)),
                               branch="feature-y")
        state, *_ = reconcile.compute_state(session, None, [merged_pr()], now=NOW, alive=False)
        self.assertEqual(state, "review")

    def test_merged_pr_on_main_never_retires(self):
        session = base_session(last_event="Stop", updated_at=iso(NOW - timedelta(hours=1)),
                               branch="main")
        state, *_ = reconcile.compute_state(session, None, [merged_pr(branch="main")], now=NOW, alive=False)
        self.assertNotEqual(state, "done")

    def test_prompt_after_merge_reopens(self):
        session = base_session(last_event="Stop", updated_at=iso(NOW - timedelta(hours=1)),
                               branch="feature-x")
        newer = iso(NOW - timedelta(hours=2))
        state, *_ = reconcile.compute_state(session, None, [merged_pr()], now=NOW, alive=False,
                                           last_prompt_at=newer)
        self.assertEqual(state, "review")
        older = iso(NOW - timedelta(hours=4))
        state, *_ = reconcile.compute_state(session, None, [merged_pr()], now=NOW, alive=False,
                                           last_prompt_at=older)
        self.assertEqual(state, "done")

    def test_owned_awaiting_pr_beats_a_merged_pr_on_the_current_branch(self):
        session = base_session(last_event="Stop", updated_at=iso(NOW - timedelta(hours=1)),
                               branch="feature-x")
        links = [merged_pr(), owned_open_pr(ref="#4", branch="feature-y")]
        state, reason, auto_done, done_reason = reconcile.compute_state(
            session, None, links, now=NOW, alive=False)
        self.assertEqual((state, reason), ("review", "PR #4 awaits your review"))
        self.assertFalse(auto_done)
        self.assertIsNone(done_reason)

    def test_not_done_if_one_pr_link_still_open(self):
        session = base_session(last_event="Stop", updated_at=iso(NOW - timedelta(hours=1)),
                               branch="feature-x")
        links = [merged_pr(), merged_pr(ref="#2", state="OPEN")]
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
        state, reason, _auto_done, _done_reason = reconcile.compute_state(session, triage, [], now=NOW, alive=False)
        self.assertEqual(state, "review")
        self.assertEqual(reason, "Your turn")

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
        session["branch"] = "feature-x"
        links = [merged_pr()]
        state, _, _auto_done, _done_reason = reconcile.compute_state(session, None, links, now=NOW, alive=True)
        self.assertEqual(state, "running")

    def test_needs_you_beats_links_done(self):
        session = base_session(last_event="PermissionRequest", waiting_reason="Bash")
        session["branch"] = "feature-x"
        links = [merged_pr()]
        state, _, _auto_done, _done_reason = reconcile.compute_state(session, None, links, now=NOW, alive=True)
        self.assertEqual(state, "needs_you")

    def test_interrupted_beats_links_done(self):
        session = base_session(last_event="PreToolUse", pid=12345)
        session["branch"] = "feature-x"
        links = [merged_pr()]
        state, _, _auto_done, _done_reason = reconcile.compute_state(session, None, links, now=NOW, alive=False)
        self.assertEqual(state, "interrupted")

    def test_links_done_applies_once_idle_again(self):
        session = base_session(last_event="Stop", updated_at=iso(NOW - timedelta(hours=1)),
                               branch="feature-x")
        links = [merged_pr()]
        state, _, auto_done, done_reason = reconcile.compute_state(session, None, links, now=NOW, alive=False)
        self.assertEqual(state, "done")
        self.assertTrue(auto_done)
        self.assertEqual(done_reason, "pr merged")
        # Counter-case: a legacy link with no branch never retires it.
        state, *_ = reconcile.compute_state(session, None, [merged_pr(branch=None)], now=NOW, alive=False)
        self.assertEqual(state, "review")

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


class NeedsYouNoteTests(AgentBoardTestCase):
    NOTE = {"last_ts": iso(NOW - timedelta(minutes=5)),
            "text": "USER_ACTION: approve the plan\nmore detail"}

    def state(self, session=None, triage=None, links=None, note=NOTE, prompt_at=None, alive=True):
        return reconcile.compute_state(
            session or base_session(last_event="Stop"), triage, links or [], now=NOW,
            alive=alive, open_needs_you_note=note, last_prompt_at=prompt_at,
        )

    def test_open_note_raises_needs_you_with_first_line(self):
        self.assertEqual(self.state(), ("needs_you", "USER_ACTION: approve the plan", False, None))

    def test_headline_wins_over_first_line(self):
        note = dict(self.NOTE, headline="USER_ACTION: deploy")
        self.assertEqual(self.state(note=note)[1], "USER_ACTION: deploy")

    def test_no_note_keeps_prior_state(self):
        self.assertEqual(self.state(note=None)[:2], ("review", "Your turn"))

    def test_newer_prompt_releases(self):
        later = iso(NOW - timedelta(minutes=1))
        self.assertEqual(self.state(prompt_at=later)[:2], ("review", "Your turn"))

    def test_older_prompt_keeps_needs_you(self):
        earlier = iso(NOW - timedelta(minutes=10))
        self.assertEqual(self.state(prompt_at=earlier)[0], "needs_you")

    def test_prompt_comparison_parses_mixed_precision_stamps(self):
        # As strings "...:04Z" > "...:04.500000Z" though it is earlier.
        note = dict(self.NOTE, last_ts="2026-06-15T11:00:04.500000Z")
        self.assertEqual(self.state(note=note, prompt_at="2026-06-15T11:00:04Z")[0], "needs_you")
        self.assertEqual(self.state(note=note, prompt_at="2026-06-15T11:00:05Z")[:2],
                         ("review", "Your turn"))

    def test_hook_needs_you_reason_wins(self):
        session = base_session(last_event="PermissionRequest", waiting_reason="Bash")
        self.assertEqual(self.state(session=session)[:2], ("needs_you", "Bash"))

    def test_snooze_wins(self):
        triage = {"status": "snoozed", "until": iso(NOW + timedelta(hours=1))}
        self.assertEqual(self.state(triage=triage)[0], "snoozed")

    def test_manual_done_wins(self):
        self.assertEqual(self.state(triage={"status": "done"})[0], "done")

    def test_note_beats_running_and_links_done(self):
        self.assertEqual(self.state(session=base_session(last_event="PreToolUse"))[0], "needs_you")
        session = base_session(last_event="Stop", branch="feature-x")
        self.assertEqual(self.state(session=session, links=[merged_pr()])[0], "needs_you")
        self.assertEqual(self.state(session=session, links=[merged_pr()], note=None)[0], "done")


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
        state, reason, auto_done, _ = reconcile.compute_state(
            session, None, [], now=NOW, alive=False, turn_finished=True,
        )
        self.assertEqual((state, reason), ("unconcluded", "Ended (other)"))
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
        # A clean Claude exit is the user typing /exit at the prompt: the
        # turn was over whatever the events table holds.
        session = clean_exit_session()
        state, _, auto_done, done_reason = reconcile.compute_state(
            session, None, [], now=NOW, alive=False, turn_finished=False,
        )
        self.assertEqual((state, done_reason), ("done", "clean exit"))
        self.assertTrue(auto_done)
        # Codex still needs the finished turn.
        session = clean_exit_session(tool="codex", end_reason="other")
        state, reason, auto_done, _ = reconcile.compute_state(
            session, None, [], now=NOW, alive=False, turn_finished=False,
        )
        self.assertEqual((state, reason), ("unconcluded", "Ended mid-turn"))
        self.assertFalse(auto_done)

    def test_only_a_session_end_event_is_done(self):
        # b263c21f: hooks installed mid-session, so the events table holds
        # only the final SessionEnd.
        session = clean_exit_session(last_event="SessionEnd", branch="feature-x")
        state, _, auto_done, done_reason = reconcile.compute_state(
            session, None, [], now=NOW, alive=False, turn_finished=False,
        )
        self.assertEqual((state, done_reason), ("done", "clean exit"))

    def test_inherited_open_pr_does_not_block(self):
        # 3647203a: PR #2 was opened on the branch before the session began.
        session = clean_exit_session(branch="claude/units", started_at=iso(NOW - timedelta(hours=2)))
        links = [{"kind": "pr", "ref": "#2", "state": "OPEN", "branch": "claude/units",
                  "source": "gh", "url": "https://github.com/a/b/pull/2",
                  "created_at": iso(NOW - timedelta(days=3))}]
        state, _, auto_done, done_reason = reconcile.compute_state(
            session, None, links, now=NOW, alive=False, turn_finished=True,
        )
        self.assertEqual((state, done_reason), ("done", "clean exit"))
        self.assertTrue(auto_done)

    def test_open_todos_block_auto_done(self):
        session = clean_exit_session(todos_open=2)
        state, reason, auto_done, _ = reconcile.compute_state(
            session, None, [], now=NOW, alive=False, turn_finished=True,
        )
        self.assertEqual((state, reason), ("unconcluded", "2 todos left open"))
        self.assertFalse(auto_done)
        session = clean_exit_session(todos_open=1)
        self.assertEqual(reconcile.compute_state(
            session, None, [], now=NOW, alive=False, turn_finished=True)[1], "1 todo left open")

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

    def test_unowned_open_pr_link_does_not_block_auto_done(self):
        session = clean_exit_session()
        links = [{"kind": "pr", "ref": "#1", "state": "OPEN", "url": "https://github.com/a/b/pull/1"}]
        state, _, auto_done, done_reason = reconcile.compute_state(
            session, None, links, now=NOW, alive=False, turn_finished=True,
        )
        self.assertEqual((state, done_reason), ("done", "clean exit"))
        self.assertTrue(auto_done)

    def test_owned_awaiting_pr_keeps_a_clean_exit_in_review(self):
        session = clean_exit_session()
        state, reason, auto_done, done_reason = reconcile.compute_state(
            session, None, [owned_open_pr()], now=NOW, alive=False, turn_finished=True,
        )
        self.assertEqual((state, reason), ("review", "PR #3 awaits your review"))
        self.assertFalse(auto_done)
        self.assertIsNone(done_reason)

    def test_owned_pr_the_user_responded_to_is_done(self):
        session = clean_exit_session()
        links = [owned_open_pr(responded_at=iso(NOW - timedelta(minutes=1)))]
        state, _, auto_done, done_reason = reconcile.compute_state(
            session, None, links, now=NOW, alive=False, turn_finished=True,
        )
        self.assertEqual((state, done_reason), ("done", "clean exit"))

    def test_one_account_awaiting_pr_reason(self):
        session = clean_exit_session()
        state, reason, *_ = reconcile.compute_state(
            session, None, [owned_open_pr(self_authored=1)], now=NOW, alive=False,
            turn_finished=True,
        )
        self.assertEqual((state, reason), ("review", "PR #3 open, no response yet"))

    def test_clean_exit_with_open_needs_you_note_is_needs_you(self):
        session = clean_exit_session()
        note = {"last_ts": iso(NOW - timedelta(minutes=10)), "text": "USER_ACTION: approve"}
        state, reason, *_ = reconcile.compute_state(
            session, None, [], now=NOW, alive=False, turn_finished=True,
            open_needs_you_note=note,
        )
        self.assertEqual((state, reason), ("needs_you", "USER_ACTION: approve"))

    def test_update_note_does_not_hold_an_ended_session(self):
        session = clean_exit_session()
        note = {"last_ts": iso(NOW - timedelta(minutes=10)), "text": "PR #4 merged"}
        state, *_ = reconcile.compute_state(
            session, None, [], now=NOW, alive=False, turn_finished=True, open_update_note=note,
        )
        self.assertEqual(state, "done")

    def test_closed_pr_link_does_not_block(self):
        session = clean_exit_session()
        links = [{"kind": "pr", "ref": "#1", "state": "CLOSED"}]
        state, _, auto_done, done_reason = reconcile.compute_state(
            session, None, links, now=NOW, alive=False, turn_finished=True,
        )
        self.assertEqual(state, "done")
        # The closed link records no branch, so the done-by-link rule does
        # not claim it; the clean exit does.
        self.assertEqual(done_reason, "clean exit")

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
        state, reason, auto_done, done_reason = reconcile.compute_state(
            session, triage, [], now=NOW, alive=False, turn_finished=True,
        )
        self.assertEqual((state, reason), ("unconcluded", "Reopened"))
        self.assertFalse(auto_done)
        self.assertIsNone(done_reason)
        # Done-by-link does not retire it either.
        session["branch"] = "feature-x"
        state, reason, *_ = reconcile.compute_state(
            session, triage, [merged_pr()], now=NOW, alive=False, turn_finished=True,
        )
        self.assertEqual((state, reason), ("unconcluded", "Reopened"))

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


class ReviewReasonTests(AgentBoardTestCase):
    def state(self, session=None, links=None, alive=True, update_note=None, prompt_at=None,
              unattended=False, **kwargs):
        return reconcile.compute_state(
            session or base_session(last_event="Stop", last_assistant="Pick A or B?"),
            None, links or [], now=NOW, alive=alive, open_update_note=update_note,
            last_prompt_at=prompt_at, unattended=unattended, **kwargs,
        )

    def update_note(self, minutes_ago=5, text="PR #4 merged\nmore"):
        return {"last_ts": iso(NOW - timedelta(minutes=minutes_ago)), "text": text}

    def test_owned_awaiting_pr_reason_on_a_live_session(self):
        self.assertEqual(self.state(links=[owned_open_pr()])[:2],
                         ("review", "PR #3 awaits your review"))

    def test_update_note_newer_than_last_prompt_is_review_with_headline(self):
        note = dict(self.update_note(), headline="PR #4 merged")
        prompt = iso(NOW - timedelta(minutes=10))
        self.assertEqual(self.state(update_note=note, prompt_at=prompt)[:2],
                         ("review", "PR #4 merged"))

    def test_update_note_first_line_without_precomputed_headline(self):
        self.assertEqual(self.state(update_note=self.update_note())[1], "PR #4 merged")

    def test_update_note_older_than_the_latest_prompt_is_not_the_reason(self):
        prompt = iso(NOW - timedelta(minutes=1))
        self.assertEqual(self.state(update_note=self.update_note(), prompt_at=prompt)[:2],
                         ("review", "Your turn: Pick A or B?"))
        unattended = self.state(update_note=self.update_note(), prompt_at=prompt,
                                unattended=True, alive=False)
        self.assertEqual(unattended[0], "unconcluded")

    def test_update_note_older_than_a_day_on_a_dead_session_is_unconcluded(self):
        session = base_session(last_event="Stop", updated_at=iso(NOW - timedelta(hours=30)))
        note = self.update_note(minutes_ago=30 * 60)
        self.assertEqual(self.state(session=session, update_note=note, alive=False)[:2],
                         ("unconcluded", None))

    def test_update_note_beats_your_turn(self):
        self.assertEqual(self.state(update_note=self.update_note())[1], "PR #4 merged")

    def test_attended_stop_is_your_turn_with_first_line(self):
        self.assertEqual(self.state()[:2], ("review", "Your turn: Pick A or B?"))

    def test_your_turn_without_a_message_has_no_trailing_colon(self):
        for text in (None, "", "   \n\n  "):
            session = base_session(last_event="Stop", last_assistant=text)
            self.assertEqual(self.state(session=session)[1], "Your turn", repr(text))

    def test_your_turn_is_capped(self):
        session = base_session(last_event="Stop", last_assistant="x" * 500)
        self.assertEqual(len(self.state(session=session)[1]), reconcile.NOTE_HEADLINE_CHARS)

    def test_interrupt_on_an_attended_live_session(self):
        session = base_session(last_event="Interrupt", last_assistant="half done")
        self.assertEqual(self.state(session=session)[:2], ("review", "Interrupted: your turn"))

    def test_idle_prompt_notification_is_your_turn(self):
        session = base_session(last_event="Notification", last_assistant="Ready.")
        self.assertEqual(self.state(session=session, notification_type="idle_prompt")[:2],
                         ("review", "Your turn: Ready."))

    def test_unattended_idle_with_a_live_process_is_loop_idle(self):
        self.assertEqual(self.state(unattended=True, alive=True)[:2],
                         ("running", "Loop idle between ticks"))

    def test_unattended_idle_with_a_dead_or_unknown_process_is_unconcluded(self):
        self.assertEqual(self.state(unattended=True, alive=False)[:2], ("unconcluded", None))
        self.assertEqual(self.state(unattended=True, alive=None)[:2], ("unconcluded", None))

    def test_your_turn_ages_out_after_the_review_window(self):
        session = base_session(last_event="Stop", updated_at=iso(NOW - timedelta(hours=25)))
        self.assertEqual(self.state(session=session, alive=False)[:2], ("unconcluded", None))

    def test_every_review_has_a_reason(self):
        cases = [
            dict(),
            dict(session=base_session(last_event="Stop", last_assistant=None)),
            dict(session=base_session(last_event="Interrupt")),
            dict(links=[owned_open_pr()]),
            dict(update_note=self.update_note()),
        ]
        for case in cases:
            state, reason, *_ = self.state(**case)
            self.assertEqual(state, "review")
            self.assertTrue(reason, case)


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
