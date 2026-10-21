import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentboard import links
from tests.helpers import AgentBoardTestCase


class TicketPatternTests(AgentBoardTestCase):
    def test_empty_by_default(self):
        self.assertEqual(links.compile_ticket_patterns({}), [])
        self.assertEqual(links.collect_offline_links({"branch": "ABC-123"}, {}), [])

    def test_extracts_configured_pattern_from_branch(self):
        config = {
            "ticket_patterns": [
                {"regex": r"[A-Z]+-\d+", "url": "https://example.atlassian.net/browse/{ref}"}
            ]
        }
        session = {"branch": "ABC-123-fix", "first_prompt": None}
        result = links.collect_offline_links(session, config)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["ref"], "ABC-123")
        self.assertEqual(result[0]["url"], "https://example.atlassian.net/browse/ABC-123")
        self.assertEqual(result[0]["kind"], "ticket")

    def test_matches_first_prompt_too(self):
        config = {"ticket_patterns": [{"regex": r"XYZ-\d+", "url": "https://x/{ref}"}]}
        session = {"branch": "main", "first_prompt": "please look at XYZ-9"}
        result = links.collect_offline_links(session, config)
        self.assertEqual([link["ref"] for link in result], ["XYZ-9"])

    def test_invalid_regex_is_skipped(self):
        config = {"ticket_patterns": [{"regex": "[", "url": "https://x/{ref}"}]}
        self.assertEqual(links.compile_ticket_patterns(config), [])


class LinksSayDoneTests(AgentBoardTestCase):
    def test_all_pr_links_merged(self):
        rows = [{"kind": "pr", "ref": "#1", "state": "MERGED"}]
        self.assertTrue(links.links_say_done(rows))

    def test_mixed_pr_states_not_done(self):
        rows = [
            {"kind": "pr", "ref": "#1", "state": "MERGED"},
            {"kind": "pr", "ref": "#2", "state": "OPEN"},
        ]
        self.assertFalse(links.links_say_done(rows))

    def test_any_link_state_done(self):
        rows = [{"kind": "url", "ref": "x", "state": "DONE"}]
        self.assertTrue(links.links_say_done(rows))

    def test_no_links_not_done(self):
        self.assertFalse(links.links_say_done([]))


class GhDetectorTests(AgentBoardTestCase):
    def test_skips_default_branches(self):
        self.assertIsNone(links.fetch_generic_pr("main", "/tmp"))
        self.assertIsNone(links.fetch_generic_pr(None, "/tmp"))

    def test_parses_gh_output(self):
        def fake_run(args, cwd, timeout=links.GH_TIMEOUT_SECS):
            return mock.Mock(
                returncode=0,
                stdout=json.dumps([{"number": 7, "state": "OPEN", "url": "u", "title": "t"}]),
            )
        result = links.fetch_generic_pr("feature-x", "/tmp", run_gh=fake_run)
        self.assertEqual(result["ref"], "#7")
        self.assertEqual(result["state"], "OPEN")

    def test_empty_result_returns_none(self):
        def fake_run(args, cwd, timeout=links.GH_TIMEOUT_SECS):
            return mock.Mock(returncode=0, stdout="[]")
        self.assertIsNone(links.fetch_generic_pr("feature-x", "/tmp", run_gh=fake_run))

    def test_should_check_generic_pr_caching(self):
        now = datetime.now(timezone.utc)
        self.assertTrue(links.should_check_generic_pr(None, now))
        merged = {"state": "MERGED", "checked_at": None}
        self.assertFalse(links.should_check_generic_pr(merged, now))
        recent_open = {
            "state": "OPEN",
            "checked_at": (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        }
        self.assertFalse(links.should_check_generic_pr(recent_open, now))
        stale_open = {
            "state": "OPEN",
            "checked_at": (now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        }
        self.assertTrue(links.should_check_generic_pr(stale_open, now))


class SessionResolutionTests(AgentBoardTestCase):
    def _insert_session(self, tool, session_id, cwd=None, pid=None):
        self.conn.execute(
            "INSERT INTO sessions(tool, session_id, cwd, pid, origin, updated_at) "
            "VALUES (?,?,?,?,'hook',?)",
            (tool, session_id, cwd, pid, "2026-01-01T00:00:00.000000Z"),
        )

    def test_explicit_session_wins(self):
        result = links.resolve_session(self.conn, explicit="claude:abc")
        self.assertEqual(result, ("claude", "abc"))

    def test_env_var_resolution(self):
        self._insert_session("claude", "env-sess")
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "env-sess"}):
            result = links.resolve_session(self.conn)
        self.assertEqual(result, ("claude", "env-sess"))

    def test_cwd_resolution_exact_and_subdir(self):
        self._insert_session("claude", "cwd-sess", cwd="/tmp/project")
        result = links.resolve_session(self.conn, cwd="/tmp/project")
        self.assertEqual(result, ("claude", "cwd-sess"))
        result2 = links.resolve_session(self.conn, cwd="/tmp/project/sub/dir")
        self.assertEqual(result2, ("claude", "cwd-sess"))

    def test_unresolvable_returns_none(self):
        result = links.resolve_session(self.conn, cwd="/no/such/path")
        self.assertIsNone(result)


class InboxTests(AgentBoardTestCase):
    def test_ingest_inbox_consumes_and_deletes(self):
        self.conn.execute(
            "INSERT INTO sessions(tool, session_id, cwd, origin, updated_at) "
            "VALUES ('claude','s1','/tmp/proj','hook','2026-01-01T00:00:00.000000Z')"
        )
        inbox = links.inbox_dir(self.agentboard_home)
        inbox.mkdir(parents=True)
        (inbox / "a.json").write_text(json.dumps({
            "cwd": "/tmp/proj",
            "links": [{"kind": "ticket", "ref": "T-1", "url": "https://x/T-1", "state": "OPEN"}],
            "status": "working on it",
        }))

        count = links.ingest_inbox(self.conn, self.agentboard_home, "2026-01-01T00:00:00.000000Z")
        self.assertEqual(count, 1)
        self.assertEqual(list(inbox.glob("*.json")), [])

        link_row = self.conn.execute(
            "SELECT * FROM links WHERE tool='claude' AND session_id='s1'"
        ).fetchone()
        self.assertEqual(link_row["ref"], "T-1")
        session_row = self.conn.execute(
            "SELECT status_text FROM sessions WHERE tool='claude' AND session_id='s1'"
        ).fetchone()
        self.assertEqual(session_row["status_text"], "working on it")

    def test_ingest_inbox_deletes_unparseable_file(self):
        inbox = links.inbox_dir(self.agentboard_home)
        inbox.mkdir(parents=True)
        (inbox / "bad.json").write_text("not json")
        links.ingest_inbox(self.conn, self.agentboard_home, "2026-01-01T00:00:00.000000Z")
        self.assertEqual(list(inbox.glob("*.json")), [])

    def test_ingest_inbox_skips_malformed_shapes_without_raising(self):
        # Valid JSON that isn't the documented shape -- a top-level list, a
        # link that isn't an object, a link with no ref, a non-string
        # status -- must be skipped (and the file still consumed), never
        # raise out of `snapshot`, and never block the valid parts of the
        # same drop.
        self.conn.execute(
            "INSERT INTO sessions(tool, session_id, cwd, origin, updated_at) "
            "VALUES ('claude','s1','/tmp/proj','hook','2026-01-01T00:00:00.000000Z')"
        )
        inbox = links.inbox_dir(self.agentboard_home)
        inbox.mkdir(parents=True)
        (inbox / "a-list.json").write_text(json.dumps([{"session": "claude:s1"}]))
        (inbox / "b-scalar.json").write_text(json.dumps("just a string"))
        (inbox / "c-mixed.json").write_text(json.dumps({
            "session": "claude:s1",
            "links": [
                "not-an-object",
                {"kind": "ticket"},
                {"kind": "ticket", "ref": "OK-1", "url": "https://x/OK-1"},
            ],
            "status": {"nested": "object"},
        }))
        (inbox / "d-links-not-list.json").write_text(json.dumps({
            "session": "claude:s1", "links": {"kind": "pr", "ref": "#1"}, "status": 42,
        }))

        count = links.ingest_inbox(self.conn, self.agentboard_home, "2026-01-01T00:00:00.000000Z")

        self.assertEqual(list(inbox.glob("*.json")), [], "every drop file must be consumed")
        self.assertEqual(count, 2, "the two object-shaped drops resolve to a session")
        refs = [r["ref"] for r in self.conn.execute(
            "SELECT ref FROM links WHERE tool='claude' AND session_id='s1'"
        ).fetchall()]
        self.assertEqual(refs, ["OK-1"])
        status = self.conn.execute(
            "SELECT status_text FROM sessions WHERE tool='claude' AND session_id='s1'"
        ).fetchone()["status_text"]
        self.assertIsNone(status, "a non-string status must not be stored")

    def test_ingest_inbox_noop_without_dir(self):
        count = links.ingest_inbox(self.conn, self.agentboard_home, "2026-01-01T00:00:00.000000Z")
        self.assertEqual(count, 0)


class ProviderTests(AgentBoardTestCase):
    def test_run_providers_upserts_links(self):
        self.conn.execute(
            "INSERT INTO sessions(tool, session_id, cwd, origin, updated_at) "
            "VALUES ('claude','s1','/tmp/proj','hook','2026-01-01T00:00:00.000000Z')"
        )
        config = {"providers": [{"name": "fake", "command": ["echo", "ignored"]}]}

        fake_result = mock.Mock(
            returncode=0,
            stdout=json.dumps([
                {"session": "claude:s1", "links": [{"kind": "pr", "ref": "#9", "state": "OPEN"}]}
            ]).encode(),
        )
        with mock.patch("agentboard.links.subprocess.run", return_value=fake_result):
            checked = links.run_providers(self.conn, config, "2026-01-01T00:00:00.000000Z")
        self.assertEqual(checked, 1)
        row = self.conn.execute(
            "SELECT * FROM links WHERE tool='claude' AND session_id='s1' AND kind='pr'"
        ).fetchone()
        self.assertEqual(row["ref"], "#9")

    def test_run_providers_skips_malformed_output_without_raising(self):
        # A provider is arbitrary external code: an object instead of a
        # list, rows that aren't objects, a session without TOOL:ID, or
        # links missing kind/ref must all be skipped, never raise out of
        # refresh-links, and never block a well-formed row from the same
        # provider.
        self.conn.execute(
            "INSERT INTO sessions(tool, session_id, cwd, origin, updated_at) "
            "VALUES ('claude','s1','/tmp/proj','hook','2026-01-01T00:00:00.000000Z')"
        )
        config = {"providers": [
            {"name": "obj", "command": ["echo"]},
            {"name": "mixed", "command": ["echo"]},
        ]}
        outputs = [
            mock.Mock(returncode=0, stdout=json.dumps({"session": "claude:s1"}).encode()),
            mock.Mock(returncode=0, stdout=json.dumps([
                "not-an-object",
                {"session": 12, "links": []},
                {"session": "no-colon", "links": [{"kind": "pr", "ref": "#1"}]},
                {"session": "claude:", "links": [{"kind": "pr", "ref": "#2"}]},
                {"session": "claude:s1", "links": "not-a-list"},
                {"session": "claude:s1", "links": [
                    None, {"ref": "#3"}, {"kind": "pr", "ref": "#4", "state": "OPEN"},
                ]},
            ]).encode()),
        ]
        with mock.patch("agentboard.links.subprocess.run", side_effect=outputs):
            checked = links.run_providers(self.conn, config, "2026-01-01T00:00:00.000000Z")
        self.assertEqual(checked, 2, "both well-formed claude:s1 rows count")
        refs = [r["ref"] for r in self.conn.execute(
            "SELECT ref FROM links WHERE tool='claude' AND session_id='s1'"
        ).fetchall()]
        self.assertEqual(refs, ["#4"])

    def test_run_providers_tolerates_timeout(self):
        import subprocess

        config = {"providers": [{"name": "slow", "command": ["sleep", "99"]}]}
        with mock.patch(
            "agentboard.links.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="sleep", timeout=30),
        ):
            checked = links.run_providers(self.conn, config, "2026-01-01T00:00:00.000000Z")
        self.assertEqual(checked, 0)


class LinksDiagTests(AgentBoardTestCase):
    """refresh-links decisions land in agentboard.log."""

    def _lines(self):
        path = self.agentboard_home / "agentboard.log"
        return path.read_text().splitlines() if path.exists() else []

    def test_provider_outcomes_are_logged_with_name_rc_rows_and_ms(self):
        config = {"providers": [
            {"name": "good", "command": ["echo"]},
            {"name": "broken", "command": ["false"]},
        ]}
        outputs = [
            mock.Mock(returncode=0, stdout=json.dumps([]).encode(), stderr=b""),
            mock.Mock(returncode=3, stdout=b"", stderr=b"provider exploded"),
        ]
        with mock.patch("agentboard.links.subprocess.run", side_effect=outputs):
            links.run_providers(self.conn, config, "2026-01-01T00:00:00.000000Z")
        lines = self._lines()
        good = [l for l in lines if "name=good" in l]
        broken = [l for l in lines if "name=broken" in l]
        self.assertEqual(len(good), 1)
        self.assertIn("INFO links.provider name=good outcome=ok", good[0])
        self.assertIn("rows=0", good[0])
        self.assertIn("ms=", good[0])
        self.assertEqual(len(broken), 1)
        self.assertIn("WARN links.provider name=broken outcome=nonzero", broken[0])
        self.assertIn("rc=3", broken[0])
        self.assertIn("provider exploded", broken[0])

    def test_gh_failure_is_logged(self):
        def fake_run(args, cwd, timeout=links.GH_TIMEOUT_SECS):
            return mock.Mock(returncode=1, stdout="", stderr="gh: not logged in")
        self.assertIsNone(links.fetch_generic_pr("feature-x", "/tmp", run_gh=fake_run))
        [line] = self._lines()
        self.assertIn("WARN links.gh branch=feature-x", line)
        self.assertIn("outcome=nonzero", line)
        self.assertIn("rc=1", line)
        self.assertIn("not logged in", line)

    def test_unparseable_inbox_file_is_logged(self):
        inbox = self.agentboard_home / "inbox"
        inbox.mkdir()
        (inbox / "bad.json").write_text("{nope")
        links.ingest_inbox(self.conn, self.agentboard_home, "2026-01-01T00:00:00.000000Z")
        [line] = self._lines()
        self.assertIn("WARN links.inbox_drop file=bad.json reason=unreadable", line)


class UpsertLinkTests(AgentBoardTestCase):
    def test_partial_update_preserves_existing_fields(self):
        now = "2026-01-01T00:00:00.000000Z"
        links.upsert_link(self.conn, "claude", "s1", {
            "kind": "pr", "ref": "#1", "url": "https://x/1", "state": "OPEN", "title": "my pr",
        }, now)
        links.upsert_link(self.conn, "claude", "s1", {
            "kind": "pr", "ref": "#1", "url": None, "state": "MERGED", "title": None,
        }, now)
        row = self.conn.execute(
            "SELECT * FROM links WHERE tool='claude' AND session_id='s1' AND kind='pr' AND ref='#1'"
        ).fetchone()
        self.assertEqual(row["url"], "https://x/1")
        self.assertEqual(row["title"], "my pr")
        self.assertEqual(row["state"], "MERGED")


if __name__ == "__main__":
    import unittest

    unittest.main()
