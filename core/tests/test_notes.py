import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentboard import notes
from tests.helpers import AgentBoardTestCase

CORPUS = Path(__file__).resolve().parent / "fixtures" / "stop-corpus.jsonl"

T1 = "2026-10-20T10:00:00.000000Z"
T2 = "2026-10-20T11:00:00.000000Z"
T3 = "2026-10-20T12:00:00.000000Z"


def open_notes(conn):
    return conn.execute(
        "SELECT * FROM notes WHERE dismissed_at IS NULL ORDER BY id"
    ).fetchall()


class CorpusTests(unittest.TestCase):
    def test_corpus_replay_matches_expected_kinds(self):
        rows = [json.loads(line) for line in CORPUS.read_text().splitlines() if line.strip()]
        self.assertGreaterEqual(len(rows), 40)
        mismatches = []
        for row in rows:
            got = notes.classify(
                row["text"], unattended=row["unattended"],
                seen_refs=set(row.get("seen_refs", ())),
            )
            if got != row["expected"]:
                mismatches.append((row["why"], got, row["expected"]))
        self.assertEqual(mismatches, [])

    def test_corpus_covers_every_outcome(self):
        rows = [json.loads(line) for line in CORPUS.read_text().splitlines() if line.strip()]
        self.assertEqual(
            {row["expected"] for row in rows}, {"needs_you", "update", "error", None}
        )


class ClassifyTests(unittest.TestCase):
    MILESTONE = (
        "Opened PR #31 for the fade task and pushed the follow-up commit. " * 8
    ).strip()

    def test_attended_marker_is_needs_you(self):
        self.assertEqual(
            notes.classify("USER_ACTION: approve the plan", unattended=False), "needs_you"
        )

    def test_attended_long_milestone_is_not_kept(self):
        self.assertGreaterEqual(len(self.MILESTONE), 400)
        self.assertIsNone(notes.classify(self.MILESTONE, unattended=False))

    def test_unattended_long_milestone_is_update(self):
        self.assertEqual(notes.classify(self.MILESTONE, unattended=True), "update")

    def test_bold_api_error_is_error(self):
        self.assertEqual(
            notes.classify("**API Error**: Can't reach the API", unattended=True), "error"
        )
        self.assertEqual(
            notes.classify("API Error: Can't reach the API", unattended=True), "error"
        )

    def test_marker_config_override(self):
        text = "NEEDS HUMAN: rotate the token"
        self.assertIsNone(notes.classify(text, unattended=False))
        config = {"note_markers": ["NEEDS HUMAN:"]}
        self.assertEqual(
            notes.classify(text, markers=notes.markers(config), unattended=False),
            "needs_you",
        )
        # The default marker no longer counts once overridden.
        self.assertIsNone(
            notes.classify("USER_ACTION: x", markers=notes.markers(config), unattended=False)
        )

    def test_empty_marker_list_disables_markers(self):
        self.assertIsNone(
            notes.classify("USER_ACTION: x", markers=notes.markers({"note_markers": []}),
                           unattended=False)
        )

    def test_default_markers_and_prefixes(self):
        self.assertEqual(notes.markers({}), ("USER_ACTION:",))
        self.assertEqual(notes.unattended_prefixes({}), ("# Autonomous loop",))
        self.assertEqual(notes.markers({"note_markers": "bad"}), ("USER_ACTION:",))

    def test_unattended_prefix_override(self):
        prompt = "[cron] nightly sweep"
        self.assertFalse(notes.is_unattended(prompt, notes.unattended_prefixes({})))
        prefixes = notes.unattended_prefixes({"note_unattended_prefixes": ["[cron]"]})
        self.assertTrue(notes.is_unattended(prompt, prefixes))
        self.assertFalse(notes.is_unattended("# Autonomous loop tick", prefixes))

    def test_default_unattended_prefix(self):
        prefixes = notes.unattended_prefixes({})
        self.assertTrue(notes.is_unattended("# Autonomous loop tick (dynamic pacing)\n\nRun", prefixes))
        self.assertFalse(notes.is_unattended("please fix the bug", prefixes))
        self.assertFalse(notes.is_unattended(None, prefixes))


class QuietFormTests(unittest.TestCase):
    QUIET = [
        "Nothing changed this tick.",
        "Nothing changed. PR #5 still needs you to post its review drafts.",
        "Nothing else changed: the plan still needs your approval.",
        "No changes since the last check.",
        "No change since the previous tick.",
        "Quiet tick, backoff window.",
        "Still quiet; nothing to act on.",
        "Still waiting on the human review of PR #9.",
        "Two things are still waiting on you:\n- **PR #5.** Approve it.\n- **Plan.** Approve it.",
        "Both items are still waiting on you: PR #5 and the lifecycle plan.",
        "PR #3 still needs your `/ack-tested` after you test the installed build.",
        "The export plan is still waiting for your approval.",
        "**PR #5 is still waiting on you.** Post its saved review drafts first.",
    ]

    def test_each_quiet_form_is_dropped(self):
        for text in self.QUIET:
            with self.subTest(text=text):
                self.assertIsNone(notes.classify(text, unattended=True))

    def test_long_quiet_restatement_without_news_is_dropped(self):
        text = ("Nothing changed this tick. PR #5 still needs you to post its saved review "
                "drafts, approve it and run the manual checks. " * 4)
        self.assertGreaterEqual(len(text), notes.UPDATE_MIN_CHARS)
        self.assertIsNone(notes.classify(text, unattended=True))

    def test_repair_or_bug_news_is_still_an_update(self):
        repair = ("Nothing changed this tick, but one repair: the cron-id marker had vanished "
                  "again, so I restored it and the handler is back on the local scope for the "
                  "next tick.")
        bug = ("Two things are still waiting on you. Separately I found a bug: the backoff "
               "timer read the wrong marker file, so it never fired on any of the submodule "
               "tasks at all.")
        for text in (repair, bug):
            with self.subTest(text=text):
                self.assertGreaterEqual(len(text), notes.NEWS_MIN_CHARS)
                self.assertEqual(notes.classify(text, unattended=True), "update")

    def test_short_news_word_stays_quiet(self):
        self.assertIsNone(notes.classify("Nothing changed; found nothing new.", unattended=True))

    def test_event_in_the_lead_sentence_is_not_a_restatement(self):
        self.assertEqual(
            notes.classify("Opened PR #12; it is waiting for your review.", unattended=True),
            "update",
        )
        self.assertEqual(
            notes.classify("PR #9 merged. The plan is still waiting on you.", unattended=True),
            "update",
        )

    def test_waiting_phrase_later_in_the_message_does_not_quiet_it(self):
        text = "Opened the export task's plan in Pending. PR #9 is still waiting on you."
        self.assertEqual(notes.classify(text, unattended=True), "update")


class SeenRefsTests(unittest.TestCase):
    def test_refs_normalization(self):
        self.assertEqual(
            notes.refs("PR #5, PRs #6 and #7, acme/Widgets#8, issue #9, abc#x"),
            {"#5", "#6", "#7", "widgets#8", "#9"},
        )
        self.assertEqual(notes.refs("no refs here, 3# or a#"), set())
        self.assertEqual(notes.refs(None), set())

    def test_first_mention_is_update_repeat_is_not(self):
        text = "The builder is still fixing PR #4's review findings."
        self.assertEqual(notes.classify(text, unattended=True), "update")
        self.assertEqual(notes.classify(text, unattended=True, seen_refs=set()), "update")
        self.assertIsNone(notes.classify(text, unattended=True, seen_refs={"#4"}))

    def test_event_word_counts_for_a_seen_ref(self):
        for text in ("PR #4 merged as abc1234.", "Opened PR #4.", "PR #4 is deployed.",
                     "CI went red on PR #4.", "Filed a follow-up for PR #4."):
            with self.subTest(text=text):
                self.assertEqual(
                    notes.classify(text, unattended=True, seen_refs={"#4"}), "update"
                )

    def test_new_ref_among_seen_ones_is_update(self):
        text = "Two subagents are in flight on PR #4 and PR #5."
        self.assertIsNone(notes.classify(text, unattended=True, seen_refs={"#4", "#5"}))
        self.assertEqual(notes.classify(text, unattended=True, seen_refs={"#4"}), "update")

    def test_status_word_with_only_seen_refs_is_dropped(self):
        text = "Tick: scope intact; PR #4 is ready for your review."
        self.assertEqual(notes.classify(text, unattended=True), "update")
        self.assertIsNone(notes.classify(text, unattended=True, seen_refs={"#4"}))
        # Without any reference a status word still counts.
        self.assertEqual(
            notes.classify("The export plan is in Pending.", unattended=True, seen_refs={"#4"}),
            "update",
        )

    def test_url_counts_for_a_seen_ref(self):
        text = "See https://github.com/acme/widgets/pull/4 for PR #4."
        self.assertEqual(notes.classify(text, unattended=True, seen_refs={"#4"}), "update")

    def test_long_message_with_seen_ref_is_still_update(self):
        text = ("The builder is still fixing PR #4's review findings in the sidebar layout. "
                * 5)
        self.assertGreaterEqual(len(text), notes.UPDATE_MIN_CHARS)
        self.assertEqual(notes.classify(text, unattended=True, seen_refs={"#4"}), "update")


class SessionRefsTests(AgentBoardTestCase):
    def test_session_refs_cover_open_and_dismissed_notes_of_that_session(self):
        self.conn.execute("BEGIN")
        notes.record_note(self.conn, "claude", "s1", "update", "Opened PR #4", "stop", T1)
        dismissed = notes.record_note(self.conn, "claude", "s1", "update",
                                      "Pushed acme#7", "stop", T2)
        notes.record_note(self.conn, "claude", "s2", "update", "Opened PR #9", "stop", T1)
        notes.dismiss(self.conn, T3, ids=[dismissed])
        self.conn.execute("COMMIT")
        self.assertEqual(notes.session_refs(self.conn, "claude", "s1"), {"#4", "acme#7"})
        self.assertEqual(notes.session_refs(self.conn, "codex", "s1"), set())


class FingerprintTests(unittest.TestCase):
    def test_same_request_with_different_timestamp_matches(self):
        a = notes.fingerprint("needs_you", "USER_ACTION: plan ready (2026-10-20T10:00:00Z), checked 10:15")
        b = notes.fingerprint("needs_you", "USER_ACTION: plan ready (2026-10-21T04:31:07Z), checked 04:40")
        self.assertEqual(a, b)

    def test_durations_and_tick_counts_are_ignored(self):
        a = notes.fingerprint("update", "Backing off 20 min after 3 ticks; PR #4 merged")
        b = notes.fingerprint("update", "Backing off 45 min after 7 ticks; PR #4 merged")
        self.assertEqual(a, b)

    def test_pr_numbers_stay_distinct(self):
        self.assertNotEqual(
            notes.fingerprint("update", "PR #12 merged"),
            notes.fingerprint("update", "PR #13 merged"),
        )

    def test_kind_is_part_of_the_key(self):
        self.assertNotEqual(
            notes.fingerprint("update", "PR #12 merged"),
            notes.fingerprint("needs_you", "PR #12 merged"),
        )

    def test_error_fingerprints_on_first_line_only(self):
        a = notes.fingerprint("error", "API Error: Can't reach the API. Retrying in 30 seconds.\nfirst detail")
        b = notes.fingerprint("error", "API Error: Can't reach the API. Retrying in 4 seconds.\nother detail")
        self.assertEqual(a, b)

    def test_only_first_200_normalized_chars_count(self):
        head = "x" * 200
        self.assertEqual(
            notes.fingerprint("update", head + " tail one"),
            notes.fingerprint("update", head + " tail two"),
        )


class RecordNoteTests(AgentBoardTestCase):
    def record(self, text="USER_ACTION: approve", kind="needs_you", now=T1, event_id=None,
               source="stop", session="s1"):
        self.conn.execute("BEGIN")
        note_id = notes.record_note(self.conn, "claude", session, kind, text, source, now,
                                    event_id=event_id)
        self.conn.execute("COMMIT")
        return note_id

    def test_insert_returns_id_and_stores_fields(self):
        note_id = self.record(event_id=7)
        (row,) = open_notes(self.conn)
        self.assertEqual(row["id"], note_id)
        self.assertEqual(row["kind"], "needs_you")
        self.assertEqual(row["count"], 1)
        self.assertEqual(row["event_id"], 7)
        self.assertEqual(row["last_event_id"], 7)
        self.assertEqual(row["ts"], T1)
        self.assertEqual(row["last_ts"], T1)
        self.assertEqual(row["source"], "stop")

    def test_repeat_bumps_count_and_last_ts(self):
        first = self.record(event_id=1)
        second = self.record(now=T2, event_id=2)
        self.assertEqual(first, second)
        (row,) = open_notes(self.conn)
        self.assertEqual(row["count"], 2)
        self.assertEqual(row["ts"], T1)
        self.assertEqual(row["last_ts"], T2)
        self.assertEqual(row["event_id"], 1)
        self.assertEqual(row["last_event_id"], 2)

    def test_older_replay_never_moves_note_backwards(self):
        self.record(now=T3, event_id=30)
        self.record(now=T1, event_id=10)
        (row,) = open_notes(self.conn)
        self.assertEqual(row["count"], 2)
        self.assertEqual(row["last_ts"], T3)
        self.assertEqual(row["last_event_id"], 30)
        self.assertEqual(row["ts"], T1)
        self.assertEqual(row["event_id"], 10)

    def test_newer_repeat_refreshes_text_older_replay_keeps_it(self):
        first = "API Error: Can't reach the API. Retrying in 1 seconds (attempt 1/10)"
        newer = "API Error: Can't reach the API. Retrying in 1 seconds (attempt 1/10)\nstill down"
        older = "API Error: Can't reach the API. Retrying in 1 seconds (attempt 1/10)\nolder body"
        self.record(text=first, kind="error", now=T2, event_id=20)
        self.record(text=newer, kind="error", now=T3, event_id=30)
        (row,) = open_notes(self.conn)
        self.assertEqual(row["text"], newer)
        self.record(text=older, kind="error", now=T1, event_id=10)
        (row,) = open_notes(self.conn)
        self.assertEqual(row["count"], 3)
        self.assertEqual(row["text"], newer)

    def test_repeat_without_event_id_keeps_event_ids(self):
        self.record(event_id=5)
        self.record(now=T2, source="cli")
        (row,) = open_notes(self.conn)
        self.assertEqual(row["last_event_id"], 5)
        self.assertEqual(row["event_id"], 5)

    def test_dismissed_then_recur_makes_new_row(self):
        first = self.record()
        self.conn.execute("UPDATE notes SET dismissed_at=? WHERE id=?", (T2, first))
        try:
            second = self.record(now=T3)
        except sqlite3.IntegrityError as exc:  # pragma: no cover - the regression
            self.fail(f"recurring after dismiss raised {exc}")
        self.assertNotEqual(first, second)
        rows = self.conn.execute("SELECT * FROM notes ORDER BY id").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["count"], 1)
        self.assertIsNone(rows[1]["dismissed_at"])

    def test_sessions_do_not_share_notes(self):
        self.record(session="a")
        self.record(session="b")
        self.assertEqual(len(open_notes(self.conn)), 2)

    def test_text_is_capped(self):
        self.record(text="USER_ACTION: " + "y" * 5000)
        (row,) = open_notes(self.conn)
        self.assertEqual(len(row["text"]), notes.MAX_TEXT_CHARS)

    def test_rejects_unknown_kind_and_empty_text(self):
        with self.assertRaises(ValueError):
            notes.record_note(self.conn, "claude", "s1", "chatter", "x", "cli", T1)
        with self.assertRaises(ValueError):
            notes.record_note(self.conn, "claude", "s1", "update", "  ", "cli", T1)


class ConfigTests(AgentBoardTestCase):
    def test_load_config_missing_and_bad(self):
        self.assertEqual(notes.load_config(self.agentboard_home), {})
        (self.agentboard_home / "config.json").write_text("{not json")
        self.assertEqual(notes.load_config(self.agentboard_home), {})
        (self.agentboard_home / "config.json").write_text("[1]")
        self.assertEqual(notes.load_config(self.agentboard_home), {})

    def test_load_markers_reads_config(self):
        (self.agentboard_home / "config.json").write_text(json.dumps({"note_markers": ["ASK:"]}))
        self.assertEqual(notes.load_markers(self.agentboard_home), ("ASK:",))

    def test_is_valid_note(self):
        self.assertTrue(notes.is_valid_note({"kind": "update", "text": "x"}))
        self.assertFalse(notes.is_valid_note({"kind": "chatter", "text": "x"}))
        self.assertFalse(notes.is_valid_note({"kind": "update", "text": ""}))
        self.assertFalse(notes.is_valid_note({"kind": "update", "text": 3}))
        self.assertFalse(notes.is_valid_note(["update", "x"]))


if __name__ == "__main__":
    unittest.main()
