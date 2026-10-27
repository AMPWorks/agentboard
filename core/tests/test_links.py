import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentboard import cli, links, reconcile
from tests.helpers import AgentBoardTestCase

NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def gh_iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeGh:
    """A fake `run_gh` that answers from per-PR fixtures shaped like gh's
    real output, and records every call."""

    def __init__(self, login="session-account"):
        self.login = login
        self.prs = {}          # url -> gh pr JSON object
        self.by_branch = {}    # branch -> url
        self.by_number = {}    # (cwd, number) -> url
        self.reviews = {}      # url -> REST review objects
        self.comments = {}     # url -> REST issue comment objects
        self.calls = []

    def add_pr(self, number, state="OPEN", branch="feature-x", created=None, author="session-account",
               merged=None, closed=None, repo="acme/widgets", cwd=None):
        url = f"https://github.com/{repo}/pull/{number}"
        self.prs[url] = {
            "number": number, "state": state, "url": url, "title": f"PR {number}",
            "createdAt": gh_iso(created or NOW - timedelta(hours=1)),
            "mergedAt": gh_iso(merged) if merged else None,
            "closedAt": gh_iso(closed or merged) if (closed or merged) else None,
            "headRefName": branch, "author": {"login": author},
        }
        self.by_branch[branch] = url
        if cwd:
            self.by_number[(cwd, str(number))] = url
        return url

    def review(self, url, login, when, user_type="User"):
        self.reviews.setdefault(url, []).append(
            {"user": {"login": login, "type": user_type}, "state": "COMMENTED",
             "submitted_at": gh_iso(when)}
        )

    def comment(self, url, login, when, user_type="User"):
        self.comments.setdefault(url, []).append(
            {"user": {"login": login, "type": user_type}, "created_at": gh_iso(when)}
        )

    @staticmethod
    def ok(value):
        return mock.Mock(returncode=0, stdout=json.dumps(value), stderr="")

    def __call__(self, args, cwd, timeout=links.GH_TIMEOUT_SECS):
        self.calls.append((list(args), cwd))
        if args[:2] == ["pr", "list"]:
            url = self.by_branch.get(args[args.index("--head") + 1])
            return self.ok([self.prs[url]] if url else [])
        if args[:2] == ["pr", "view"]:
            target = args[2]
            url = target if target.startswith("http") else self.by_number.get((cwd, target))
            if url not in self.prs:
                return mock.Mock(returncode=1, stdout="", stderr="no pull requests found")
            return self.ok(self.prs[url])
        if args[:2] == ["api", "user"]:
            return self.ok({"login": self.login})
        if args[0] == "api":
            path = args[1]
            _, owner, repo, kind, number, endpoint = path.split("/")
            url = f"https://github.com/{owner}/{repo}/pull/{number}"
            items = self.reviews.get(url, []) if endpoint == "reviews" else self.comments.get(url, [])
            # --paginate prints one array per page; emulate two pages.
            half = len(items) // 2
            return mock.Mock(returncode=0, stdout=json.dumps(items[:half]) + json.dumps(items[half:]),
                             stderr="")
        raise AssertionError(f"unexpected gh call {args}")

    def count(self, prefix):
        return sum(1 for args, _cwd in self.calls if args[:len(prefix)] == prefix)


def add_session(conn, tool="claude", session_id="s1", **overrides):
    row = {
        "tool": tool, "session_id": session_id, "cwd": "/tmp/proj", "branch": "feature-x",
        "started_at": iso(NOW - timedelta(hours=3)), "updated_at": iso(NOW - timedelta(minutes=5)),
        "last_event": "Stop", "last_event_at": iso(NOW - timedelta(minutes=5)), "origin": "hook",
        "interactive": 1,
    }
    row.update(overrides)
    cols = ",".join(row)
    conn.execute(f"INSERT INTO sessions({cols}) VALUES ({','.join('?' for _ in row)})",
                 list(row.values()))


def link_row(conn, ref="#1", session_id="s1", tool="claude"):
    row = conn.execute(
        "SELECT * FROM links WHERE tool=? AND session_id=? AND kind='pr' AND ref=?",
        (tool, session_id, ref),
    ).fetchone()
    return dict(row) if row else None


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
    CLOSED = iso(NOW - timedelta(hours=2))

    def pr(self, ref="#1", state="MERGED", branch="feature-x", closed_at=CLOSED, **extra):
        return dict(kind="pr", ref=ref, state=state, branch=branch, closed_at=closed_at,
                    checked_at=self.CLOSED, **extra)

    def test_all_pr_links_on_the_current_branch_merged(self):
        self.assertTrue(links.links_say_done([self.pr()], "feature-x"))
        self.assertEqual(links.links_done_reason([self.pr()], "feature-x"), "pr merged")

    def test_null_branch_link_never_counts(self):
        self.assertFalse(links.links_say_done([self.pr(branch=None)], "feature-x"))

    def test_merged_link_on_another_branch_does_not_retire(self):
        self.assertFalse(links.links_say_done([self.pr(branch="old-branch")], "feature-x"))

    def test_default_branch_or_no_branch_never_retires(self):
        for branch in ("main", "master", "develop", None):
            self.assertFalse(links.links_say_done([self.pr(branch=branch)], branch), branch)

    def test_mixed_pr_states_not_done(self):
        rows = [self.pr(), self.pr(ref="#2", state="OPEN", closed_at=None)]
        self.assertFalse(links.links_say_done(rows, "feature-x"))

    def test_open_pr_on_another_branch_does_not_block(self):
        rows = [self.pr(), self.pr(ref="#2", state="OPEN", branch="other", closed_at=None)]
        self.assertTrue(links.links_say_done(rows, "feature-x"))

    def test_newer_prompt_reopens(self):
        newer = iso(NOW - timedelta(hours=1))
        older = iso(NOW - timedelta(hours=3))
        self.assertFalse(links.links_say_done([self.pr()], "feature-x", newer))
        self.assertTrue(links.links_say_done([self.pr()], "feature-x", older))

    def test_legacy_null_closed_at_falls_back_to_checked_at(self):
        link = self.pr(closed_at=None)
        self.assertTrue(links.links_say_done([link], "feature-x", iso(NOW - timedelta(hours=3))))
        self.assertFalse(links.links_say_done([link], "feature-x", iso(NOW - timedelta(hours=1))))

    def test_times_compare_parsed_not_as_strings(self):
        # As strings "...:04Z" > "...:04.500000Z" though it is earlier.
        link = self.pr(closed_at="2026-06-15T11:00:04.500000Z")
        self.assertTrue(links.links_say_done([link], "feature-x", "2026-06-15T11:00:04Z"))
        self.assertFalse(links.links_say_done([link], "feature-x", "2026-06-15T11:00:05Z"))

    def test_any_link_state_done(self):
        rows = [{"kind": "url", "ref": "x", "state": "DONE"}]
        self.assertTrue(links.links_say_done(rows))
        self.assertEqual(links.links_done_reason(rows, "main"), "link done")

    def test_no_links_not_done(self):
        self.assertFalse(links.links_say_done([], "feature-x"))


class OwnershipTests(AgentBoardTestCase):
    SESSION = {"started_at": iso(NOW - timedelta(hours=3)), "ended_at": None}

    def pr(self, **extra):
        link = {"kind": "pr", "ref": "#1", "url": "https://github.com/a/b/pull/1", "state": "OPEN"}
        link.update(extra)
        return link

    def test_reported_links_are_owned(self):
        for source in ("cli", "inbox", "provider"):
            self.assertTrue(links.pr_is_owned(self.pr(source=source), self.SESSION, NOW), source)

    def test_gh_link_created_in_lifetime_is_owned(self):
        link = self.pr(source="gh", created_at=gh_iso(NOW - timedelta(hours=1)))
        self.assertTrue(links.pr_is_owned(link, self.SESSION, NOW))

    def test_gh_link_created_before_the_session_is_inherited(self):
        link = self.pr(source="gh", created_at=gh_iso(NOW - timedelta(days=2)))
        self.assertFalse(links.pr_is_owned(link, self.SESSION, NOW))

    def test_gh_link_created_after_the_session_ended_is_not_owned(self):
        session = dict(self.SESSION, ended_at=iso(NOW - timedelta(hours=2)))
        link = self.pr(source="gh", created_at=gh_iso(NOW - timedelta(hours=1)))
        self.assertFalse(links.pr_is_owned(link, session, NOW))

    def test_unknown_creation_time_is_not_owned(self):
        self.assertFalse(links.pr_is_owned(self.pr(source="gh"), self.SESSION, NOW))
        self.assertFalse(links.pr_is_owned(self.pr(source=None), self.SESSION, NOW))

    def test_awaits_you_needs_owned_open_unanswered_with_url(self):
        owned = self.pr(source="cli")
        self.assertTrue(links.pr_awaits_you(owned, self.SESSION, NOW))
        self.assertFalse(links.pr_awaits_you(dict(owned, responded_at="t"), self.SESSION, NOW))
        self.assertFalse(links.pr_awaits_you(dict(owned, state="MERGED"), self.SESSION, NOW))
        self.assertFalse(links.pr_awaits_you(dict(owned, url=None), self.SESSION, NOW))
        self.assertFalse(links.pr_awaits_you(self.pr(source="gh"), self.SESSION, NOW))

    def test_awaiting_reason_wording(self):
        one = self.pr(source="cli", ref="#3")
        two = self.pr(source="cli", ref="#4")
        self.assertEqual(links.awaiting_reason([one]), "PR #3 awaits your review")
        self.assertEqual(links.awaiting_reason([one, two]), "PRs #3, #4 await your review")
        mine = dict(one, self_authored=1)
        self.assertEqual(links.awaiting_reason([mine]), "PR #3 open, no response yet")
        self.assertIsNone(links.awaiting_reason([]))


class GhDetectorTests(AgentBoardTestCase):
    def test_skips_default_branches(self):
        self.assertIsNone(links.fetch_generic_pr("main", "/tmp"))
        self.assertIsNone(links.fetch_generic_pr(None, "/tmp"))

    def test_parses_gh_output(self):
        def fake_run(args, cwd, timeout=links.GH_TIMEOUT_SECS):
            return mock.Mock(
                returncode=0,
                stdout=json.dumps([{
                    "number": 7, "state": "MERGED", "url": "u", "title": "t",
                    "createdAt": "2026-06-01T00:00:00Z", "mergedAt": "2026-06-02T00:00:00Z",
                    "closedAt": "2026-06-02T00:00:00Z", "headRefName": "feature-x",
                    "author": {"login": "someone"},
                }]),
            )
        result = links.fetch_generic_pr("feature-x", "/tmp", run_gh=fake_run)
        self.assertEqual(result["ref"], "#7")
        self.assertEqual(result["state"], "MERGED")
        self.assertEqual(result["branch"], "feature-x")
        self.assertEqual(result["created_at"], "2026-06-01T00:00:00Z")
        self.assertEqual(result["closed_at"], "2026-06-02T00:00:00Z")
        self.assertEqual(result["author"], "someone")

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


class ExternalNotesTests(AgentBoardTestCase):
    NOW = "2026-01-01T00:00:00.000000Z"

    def setUp(self):
        super().setUp()
        self.conn.execute(
            "INSERT INTO sessions(tool, session_id, cwd, origin, updated_at) "
            "VALUES ('claude','s1','/tmp/proj','hook','2026-01-01T00:00:00.000000Z')"
        )

    def _notes(self):
        return self.conn.execute(
            "SELECT tool, session_id, kind, text, source, count FROM notes ORDER BY id"
        ).fetchall()

    def test_inbox_drop_records_notes(self):
        inbox = links.inbox_dir(self.agentboard_home)
        inbox.mkdir(parents=True)
        (inbox / "a.json").write_text(json.dumps({
            "session": "claude:s1",
            "notes": [
                {"kind": "needs_you", "text": "approve the deploy"},
                {"kind": "update", "text": "PR #7 merged"},
            ],
        }))
        count = links.ingest_inbox(self.conn, self.agentboard_home, self.NOW)
        self.assertEqual(count, 1)
        rows = [tuple(r) for r in self._notes()]
        self.assertEqual(rows, [
            ("claude", "s1", "needs_you", "approve the deploy", "inbox", 1),
            ("claude", "s1", "update", "PR #7 merged", "inbox", 1),
        ])

    def test_inbox_skips_malformed_notes_without_blocking_the_rest(self):
        inbox = links.inbox_dir(self.agentboard_home)
        inbox.mkdir(parents=True)
        (inbox / "a.json").write_text(json.dumps({
            "session": "claude:s1",
            "links": [{"kind": "ticket", "ref": "T-1"}],
            "notes": [
                "not-an-object",
                {"kind": "chatter", "text": "x"},
                {"kind": "update", "text": ""},
                {"kind": "update", "text": 12},
                {"kind": "error"},
                {"kind": "error", "text": "API Error: down"},
            ],
        }))
        (inbox / "b.json").write_text(json.dumps({
            "session": "claude:s1", "notes": {"kind": "update", "text": "not a list"},
        }))
        links.ingest_inbox(self.conn, self.agentboard_home, self.NOW)
        self.assertEqual([r["text"] for r in self._notes()], ["API Error: down"])
        self.assertEqual(list(inbox.glob("*.json")), [])
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM links WHERE ref='T-1'").fetchone())

    def test_provider_rows_record_notes(self):
        config = {"providers": [{"name": "fake", "command": ["echo"]}]}
        fake_result = mock.Mock(returncode=0, stdout=json.dumps([
            {"session": "claude:s1", "notes": [
                {"kind": "update", "text": "CI went red on main"},
                {"kind": "bogus", "text": "skipped"},
            ]},
            {"session": "claude:s1", "notes": "not-a-list"},
        ]).encode())
        with mock.patch("agentboard.links.subprocess.run", return_value=fake_result):
            links.run_providers(self.conn, config, self.NOW)
        rows = [tuple(r) for r in self._notes()]
        self.assertEqual(rows, [("claude", "s1", "update", "CI went red on main", "provider", 1)])

    def test_provider_repeat_bumps_count(self):
        config = {"providers": [{"name": "fake", "command": ["echo"]}]}
        payload = json.dumps([
            {"session": "claude:s1", "notes": [{"kind": "update", "text": "CI went red"}]},
        ]).encode()
        with mock.patch("agentboard.links.subprocess.run",
                        return_value=mock.Mock(returncode=0, stdout=payload)):
            links.run_providers(self.conn, config, self.NOW)
            links.run_providers(self.conn, config, "2026-01-01T00:05:00.000000Z")
        (row,) = self._notes()
        self.assertEqual(row["count"], 2)


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


class UpsertLinkLifecycleTests(AgentBoardTestCase):
    T1 = "2026-06-15T10:00:00.000000Z"
    T2 = "2026-06-15T11:00:00.000000Z"

    def setUp(self):
        super().setUp()
        add_session(self.conn, branch="feature-x")

    def test_five_argument_call_still_works(self):
        links.upsert_link(self.conn, "claude", "s1", {"kind": "pr", "ref": "#1"}, self.T1)
        row = link_row(self.conn)
        self.assertEqual(row["checked_at"], self.T1)
        self.assertIsNone(row["branch"])
        self.assertIsNone(row["source"])

    def test_closed_at_is_stamped_once(self):
        link = {"kind": "pr", "ref": "#1", "state": "OPEN"}
        links.upsert_link(self.conn, "claude", "s1", link, self.T1)
        self.assertIsNone(link_row(self.conn)["closed_at"])
        merged = dict(link, state="MERGED")
        links.upsert_link(self.conn, "claude", "s1", merged, self.T1)
        self.assertEqual(link_row(self.conn)["closed_at"], self.T1)
        links.upsert_link(self.conn, "claude", "s1", merged, self.T2)
        self.assertEqual(link_row(self.conn)["closed_at"], self.T1, "a re-report never restamps")
        links.upsert_link(self.conn, "claude", "s1", {"kind": "pr", "ref": "#1"}, self.T2)
        self.assertEqual(link_row(self.conn)["closed_at"], self.T1)

    def test_insert_already_merged_stamps_now_or_gh_time(self):
        links.upsert_link(self.conn, "claude", "s1",
                          {"kind": "pr", "ref": "#1", "state": "MERGED"}, self.T1)
        self.assertEqual(link_row(self.conn)["closed_at"], self.T1)
        links.upsert_link(self.conn, "claude", "s1",
                          {"kind": "pr", "ref": "#2", "state": "MERGED",
                           "closed_at": "2026-06-14T00:00:00Z"}, self.T1)
        self.assertEqual(link_row(self.conn, "#2")["closed_at"], "2026-06-14T00:00:00Z")

    def test_gh_close_time_replaces_a_stamp(self):
        links.upsert_link(self.conn, "claude", "s1",
                          {"kind": "pr", "ref": "#1", "state": "MERGED"}, self.T2)
        links.upsert_link(self.conn, "claude", "s1",
                          {"kind": "pr", "ref": "#1", "state": "MERGED",
                           "closed_at": "2026-06-15T09:00:00Z"}, self.T2)
        self.assertEqual(link_row(self.conn)["closed_at"], "2026-06-15T09:00:00Z")

    def test_reopened_pr_clears_closed_at(self):
        links.upsert_link(self.conn, "claude", "s1",
                          {"kind": "pr", "ref": "#1", "state": "CLOSED"}, self.T1)
        links.upsert_link(self.conn, "claude", "s1",
                          {"kind": "pr", "ref": "#1", "state": "OPEN"}, self.T2)
        self.assertIsNone(link_row(self.conn)["closed_at"])

    def test_session_branch_fills_but_never_moves_a_branch(self):
        links.upsert_link(self.conn, "claude", "s1", {"kind": "pr", "ref": "#1"}, self.T1,
                          branch="feature-x")
        self.assertEqual(link_row(self.conn)["branch"], "feature-x")
        links.upsert_link(self.conn, "claude", "s1", {"kind": "pr", "ref": "#1"}, self.T2,
                          branch="feature-y")
        self.assertEqual(link_row(self.conn)["branch"], "feature-x")

    def test_explicit_branch_wins(self):
        links.upsert_link(self.conn, "claude", "s1", {"kind": "pr", "ref": "#1"}, self.T1,
                          branch="feature-x")
        links.upsert_link(self.conn, "claude", "s1",
                          {"kind": "pr", "ref": "#1", "branch": "pr-head"}, self.T2,
                          branch="feature-x")
        self.assertEqual(link_row(self.conn)["branch"], "pr-head")

    def test_source_never_downgrades(self):
        links.upsert_link(self.conn, "claude", "s1", {"kind": "pr", "ref": "#1"}, self.T1,
                          source="cli")
        links.upsert_link(self.conn, "claude", "s1", {"kind": "pr", "ref": "#1"}, self.T2,
                          source="gh")
        self.assertEqual(link_row(self.conn)["source"], "cli")

    def test_gh_source_upgrades_to_reported(self):
        links.upsert_link(self.conn, "claude", "s1", {"kind": "pr", "ref": "#1"}, self.T1,
                          source="gh")
        links.upsert_link(self.conn, "claude", "s1", {"kind": "pr", "ref": "#1"}, self.T2,
                          source="cli")
        self.assertEqual(link_row(self.conn)["source"], "cli")

    def test_created_and_responded_at_coalesce(self):
        links.upsert_link(self.conn, "claude", "s1",
                          {"kind": "pr", "ref": "#1", "created_at": "c1", "responded_at": "r1"},
                          self.T1)
        links.upsert_link(self.conn, "claude", "s1",
                          {"kind": "pr", "ref": "#1", "responded_at": "r2"}, self.T2)
        row = link_row(self.conn)
        self.assertEqual((row["created_at"], row["responded_at"]), ("c1", "r1"))

    def test_link_command_records_cli_source_and_session_branch(self):
        rc = cli.main(["link", "--kind", "pr", "--ref", "#5", "--url",
                       "https://github.com/a/b/pull/5", "--session", "claude:s1"])
        self.assertEqual(rc, 0)
        row = link_row(self.conn, "#5")
        self.assertEqual((row["source"], row["branch"]), ("cli", "feature-x"))

    def test_link_command_explicit_branch(self):
        cli.main(["link", "--kind", "pr", "--ref", "#5", "--branch", "pr-head",
                  "--session", "claude:s1"])
        self.assertEqual(link_row(self.conn, "#5")["branch"], "pr-head")

    def test_inbox_and_provider_record_their_source_and_branch(self):
        inbox = links.inbox_dir(self.agentboard_home)
        inbox.mkdir(parents=True)
        (inbox / "a.json").write_text(json.dumps({
            "session": "claude:s1",
            "links": [{"kind": "pr", "ref": "#7", "responded_at": "forged", "state": "OPEN"}],
        }))
        links.ingest_inbox(self.conn, self.agentboard_home, self.T1)
        row = link_row(self.conn, "#7")
        self.assertEqual((row["source"], row["branch"]), ("inbox", "feature-x"))
        self.assertIsNone(row["responded_at"], "only AgentBoard works out responses")

        config = {"providers": [{"name": "fake", "command": ["echo"]}]}
        payload = json.dumps([{"session": "claude:s1", "links": [
            {"kind": "pr", "ref": "#8", "branch": "their-head"}]}]).encode()
        with mock.patch("agentboard.links.subprocess.run",
                        return_value=mock.Mock(returncode=0, stdout=payload)):
            links.run_providers(self.conn, config, self.T1)
        row = link_row(self.conn, "#8")
        self.assertEqual((row["source"], row["branch"]), ("provider", "their-head"))

    def test_ticket_links_record_ticket_source(self):
        (self.agentboard_home / "config.json").write_text(json.dumps({
            "ticket_patterns": [{"regex": r"feature-(\w)", "url": "https://t/{ref}"}],
        }))
        with mock.patch("agentboard.links.gh_available", return_value=False):
            links.refresh_links(self.conn, home=self.agentboard_home, now=NOW)
        row = self.conn.execute("SELECT * FROM links WHERE kind='ticket'").fetchone()
        self.assertEqual((row["ref"], row["source"], row["branch"]), ("x", "ticket", "feature-x"))


class BranchDetectorRefreshTests(AgentBoardTestCase):
    def setUp(self):
        super().setUp()
        self.gh = FakeGh()

    def refresh(self, when=NOW, config=None):
        if config is not None:
            (self.agentboard_home / "config.json").write_text(json.dumps(config))
        return links.refresh_links(self.conn, run_gh=self.gh, home=self.agentboard_home, now=when)

    def test_branch_detector_records_gh_source_branch_and_times(self):
        add_session(self.conn)
        merged = NOW - timedelta(minutes=30)
        self.gh.add_pr(1, state="MERGED", merged=merged)
        self.refresh()
        row = link_row(self.conn)
        self.assertEqual((row["source"], row["branch"], row["state"]), ("gh", "feature-x", "MERGED"))
        self.assertEqual(row["closed_at"], gh_iso(merged))
        self.assertEqual(row["created_at"], gh_iso(NOW - timedelta(hours=1)))

    def test_branch_change_rechecks(self):
        add_session(self.conn)
        self.gh.add_pr(1, state="MERGED", merged=NOW - timedelta(minutes=30))
        self.refresh()
        self.refresh()
        self.assertEqual(self.gh.count(["pr", "list"]), 1, "merged on the current branch: no re-check")
        self.conn.execute("UPDATE sessions SET branch='feature-y'")
        self.gh.add_pr(2, branch="feature-y")
        self.refresh()
        self.assertEqual(self.gh.count(["pr", "list"]), 2)
        self.assertEqual(link_row(self.conn, "#2")["branch"], "feature-y")

    def test_legacy_null_branch_row_rechecks_and_backfills(self):
        add_session(self.conn)
        url = self.gh.add_pr(1, state="MERGED", merged=NOW - timedelta(minutes=30))
        self.conn.execute(
            "INSERT INTO links(tool, session_id, kind, ref, url, state, checked_at) "
            "VALUES ('claude','s1','pr','#1',?,'MERGED',?)", (url, iso(NOW - timedelta(days=1))),
        )
        self.refresh()
        self.assertEqual(self.gh.count(["pr", "list"]), 1)
        row = link_row(self.conn)
        self.assertEqual(row["branch"], "feature-x")
        self.assertEqual(row["closed_at"], gh_iso(NOW - timedelta(minutes=30)))

    def test_cli_reported_pr_found_by_detector_keeps_cli_source(self):
        add_session(self.conn)
        url = self.gh.add_pr(1, state="MERGED", merged=NOW - timedelta(minutes=30))
        links.upsert_link(self.conn, "claude", "s1", {"kind": "pr", "ref": "#1", "url": url},
                          iso(NOW - timedelta(days=1)), source="cli")
        # Legacy-like: no branch on the reported row, so the detector looks.
        self.refresh()
        row = link_row(self.conn)
        self.assertEqual((row["source"], row["branch"]), ("cli", "feature-x"))

    def test_gh_found_pr_reported_later_becomes_cli(self):
        add_session(self.conn)
        self.gh.add_pr(1)
        self.refresh()
        self.assertEqual(link_row(self.conn)["source"], "gh")
        cli.main(["link", "--kind", "pr", "--ref", "#1", "--session", "claude:s1"])
        self.assertEqual(link_row(self.conn)["source"], "cli")

    def test_responded_at_from_a_user_review(self):
        add_session(self.conn)
        url = self.gh.add_pr(1)
        self.gh.review(url, "the-user", NOW - timedelta(minutes=20))
        self.refresh()
        self.assertEqual(link_row(self.conn)["responded_at"], gh_iso(NOW - timedelta(minutes=20)))

    def test_bot_comment_is_ignored(self):
        add_session(self.conn)
        url = self.gh.add_pr(1)
        self.gh.comment(url, "ci-helper[bot]", NOW - timedelta(minutes=20), user_type="Bot")
        self.gh.comment(url, "renamed-bot", NOW - timedelta(minutes=20), user_type="Bot")
        self.refresh()
        self.assertIsNone(link_row(self.conn)["responded_at"])

    def test_author_and_automation_logins_are_ignored(self):
        add_session(self.conn)
        url = self.gh.add_pr(1, author="author-account")
        self.gh.comment(url, "author-account", NOW - timedelta(minutes=30))
        self.gh.review(url, "Reviewer-Account", NOW - timedelta(minutes=20))
        self.refresh(config={"automation_logins": ["reviewer-account"]})
        self.assertIsNone(link_row(self.conn)["responded_at"])
        self.gh.comment(url, "the-user", NOW - timedelta(minutes=10))
        self.refresh(when=NOW + timedelta(minutes=30))
        self.assertEqual(link_row(self.conn)["responded_at"], gh_iso(NOW - timedelta(minutes=10)))
        self.assertEqual(link_row(self.conn)["self_authored"], 0)

    def add_turns(self, *marks):
        for minutes, event in marks:
            self.conn.execute(
                "INSERT INTO events(ts, tool, session_id, event) VALUES (?,'claude','s1',?)",
                (iso(NOW - timedelta(minutes=minutes)), event),
            )

    def test_user_as_default_login_with_a_separate_author_account(self):
        # The user's own account is the default gh login; sessions open PRs
        # through a separate token, so the PR author is another account.
        self.gh = FakeGh(login="example-human")
        add_session(self.conn)
        self.add_turns((60, "UserPromptSubmit"), (40, "Stop"))
        url = self.gh.add_pr(1, author="example-author-bot")
        self.gh.comment(url, "example-author-bot", NOW - timedelta(minutes=50))
        self.refresh()
        self.assertIsNone(link_row(self.conn)["responded_at"], "the author's own comment")
        self.assertEqual(link_row(self.conn)["self_authored"], 0)
        self.gh.review(url, "Example-Human", NOW - timedelta(minutes=30))
        self.refresh(when=NOW + timedelta(minutes=30))
        self.assertEqual(link_row(self.conn)["responded_at"], gh_iso(NOW - timedelta(minutes=30)))

    def test_active_login_as_author_with_a_separate_user_account(self):
        # The session's account is the default gh login and authors the PR;
        # the user reviews from their own account.
        self.gh = FakeGh(login="example-author-bot")
        add_session(self.conn)
        self.add_turns((60, "UserPromptSubmit"), (40, "Stop"), (20, "UserPromptSubmit"))
        url = self.gh.add_pr(1, author="example-author-bot")
        self.gh.comment(url, "example-author-bot", NOW - timedelta(minutes=10))
        self.refresh()
        self.assertIsNone(link_row(self.conn)["responded_at"], "the session's own mid-turn comment")
        self.gh.comment(url, "example-human", NOW - timedelta(minutes=5))
        self.refresh(when=NOW + timedelta(minutes=30))
        self.assertEqual(link_row(self.conn)["responded_at"], gh_iso(NOW - timedelta(minutes=5)))

    def test_one_account_with_no_turn_history_counts_only_after_the_end(self):
        # No parent turn events (backfill origin, or hooks installed late):
        # the session's own comments must not clear its PR while it lives.
        add_session(self.conn)
        url = self.gh.add_pr(1, author="session-account")
        self.gh.comment(url, "session-account", NOW - timedelta(minutes=30))
        self.refresh()
        self.assertIsNone(link_row(self.conn)["responded_at"], "live session, unknown turns")
        self.conn.execute("UPDATE sessions SET ended_at=?", (iso(NOW - timedelta(minutes=20)),))
        self.refresh(when=NOW + timedelta(minutes=30))
        self.assertIsNone(link_row(self.conn)["responded_at"], "before ended_at")
        self.gh.comment(url, "session-account", NOW - timedelta(minutes=10))
        self.refresh(when=NOW + timedelta(minutes=60))
        self.assertEqual(link_row(self.conn)["responded_at"], gh_iso(NOW - timedelta(minutes=10)))

    def test_one_account_with_only_a_session_end_counts_only_after_the_end(self):
        # Hooks installed late: the only parent turn event is the SessionEnd,
        # so the session's comments before it are of unknown turn state.
        add_session(self.conn)
        self.conn.execute("UPDATE sessions SET ended_at=?", (iso(NOW - timedelta(minutes=20)),))
        self.add_turns((20, "SessionEnd"))
        url = self.gh.add_pr(1, author="session-account")
        self.gh.comment(url, "session-account", NOW - timedelta(minutes=30))
        self.refresh()
        self.assertIsNone(link_row(self.conn)["responded_at"], "before the only turn mark")
        self.gh.comment(url, "session-account", NOW - timedelta(minutes=10))
        self.refresh(when=NOW + timedelta(minutes=30))
        self.assertEqual(link_row(self.conn)["responded_at"], gh_iso(NOW - timedelta(minutes=10)))

    def test_mid_turn_checker_is_built_once_per_session_per_refresh(self):
        add_session(self.conn)
        ctx = links.GhContext(self.gh)
        with mock.patch("agentboard.links.mid_turn_checker", wraps=links.mid_turn_checker) as built:
            first = ctx.mid_turn(self.conn, "claude", "s1")
            second = ctx.mid_turn(self.conn, "claude", "s1")
        self.assertIs(first, second)
        self.assertEqual(built.call_count, 1)

    def test_one_account_counts_responses_made_while_not_mid_turn(self):
        add_session(self.conn)
        url = self.gh.add_pr(1, author="session-account")
        for minutes, event in ((60, "UserPromptSubmit"), (40, "Stop"), (20, "UserPromptSubmit")):
            self.conn.execute(
                "INSERT INTO events(ts, tool, session_id, event) VALUES (?,'claude','s1',?)",
                (iso(NOW - timedelta(minutes=minutes)), event),
            )
        # Mid-turn (the session itself commenting): not a response.
        self.gh.comment(url, "session-account", NOW - timedelta(minutes=50))
        self.gh.comment(url, "session-account", NOW - timedelta(minutes=10))
        self.refresh()
        self.assertIsNone(link_row(self.conn)["responded_at"])
        self.assertEqual(link_row(self.conn)["self_authored"], 1)
        # Between the Stop and the next prompt: the user, on the same account.
        self.gh.comment(url, "session-account", NOW - timedelta(minutes=30))
        self.refresh(when=NOW + timedelta(minutes=30))
        self.assertEqual(link_row(self.conn)["responded_at"], gh_iso(NOW - timedelta(minutes=30)))


class RefreshPrLinkTests(AgentBoardTestCase):
    def setUp(self):
        super().setUp()
        self.gh = FakeGh()

    def refresh(self, when=NOW):
        return links.refresh_links(self.conn, run_gh=self.gh, home=self.agentboard_home, now=when)

    def report(self, ref, url=None, session_id="s1", when=NOW - timedelta(hours=1)):
        links.upsert_link(self.conn, "claude", session_id,
                          {"kind": "pr", "ref": ref, "url": url, "state": "OPEN"}, iso(when),
                          branch=links.session_branch(self.conn, "claude", session_id),
                          source="cli")

    def test_cli_pr_on_main_gets_state_and_response_refreshed(self):
        add_session(self.conn, branch="main")
        url = self.gh.add_pr(3, branch="handler-3", state="MERGED",
                             merged=NOW - timedelta(minutes=5))
        self.report("#3", url)
        self.refresh()
        row = link_row(self.conn, "#3")
        self.assertEqual(row["state"], "MERGED")
        self.assertEqual(row["closed_at"], gh_iso(NOW - timedelta(minutes=5)))
        self.assertEqual(row["branch"], "handler-3", "gh's head branch is authoritative")
        self.assertEqual(self.gh.count(["pr", "list"]), 0, "no detector on main")

        url4 = self.gh.add_pr(4, branch="handler-4")
        self.gh.review(url4, "the-user", NOW - timedelta(minutes=3))
        self.report("#4", url4)
        self.refresh(when=NOW + timedelta(hours=1))
        self.assertEqual(link_row(self.conn, "#4")["responded_at"],
                         gh_iso(NOW - timedelta(minutes=3)))

    def test_two_open_prs_on_one_session_are_both_refreshed(self):
        add_session(self.conn, branch="main")
        url3 = self.gh.add_pr(3, branch="a", state="MERGED", merged=NOW)
        url4 = self.gh.add_pr(4, branch="b", state="CLOSED", closed=NOW)
        self.report("#3", url3)
        self.report("#4", url4)
        self.refresh()
        self.assertEqual(link_row(self.conn, "#3")["state"], "MERGED")
        self.assertEqual(link_row(self.conn, "#4")["state"], "CLOSED")

    def test_merged_link_is_not_refetched(self):
        add_session(self.conn, branch="main")
        url = self.gh.add_pr(3, state="MERGED", merged=NOW)
        links.upsert_link(self.conn, "claude", "s1",
                          {"kind": "pr", "ref": "#3", "url": url, "state": "MERGED"},
                          iso(NOW - timedelta(days=1)), source="cli")
        self.refresh()
        self.assertEqual(self.gh.count(["pr", "view"]), 0)

    def test_copilot_review_does_not_count(self):
        add_session(self.conn, branch="main")
        url = self.gh.add_pr(3)
        self.gh.review(url, "copilot-pull-request-reviewer[bot]", NOW - timedelta(minutes=5),
                       user_type="Bot")
        self.report("#3", url)
        self.refresh()
        row = link_row(self.conn, "#3")
        self.assertEqual(row["state"], "OPEN")
        self.assertIsNone(row["responded_at"])
        session = dict(self.conn.execute("SELECT * FROM sessions").fetchone())
        self.assertTrue(links.pr_awaits_you(row, session, NOW))

    def test_urlless_link_is_looked_up_by_number_in_the_session_cwd(self):
        cwd = str(self.tmp_path)
        add_session(self.conn, branch="main", cwd=cwd)
        url = self.gh.add_pr(5, cwd=cwd)
        self.report("#5")
        self.refresh()
        row = link_row(self.conn, "#5")
        self.assertEqual((row["url"], row["state"]), (url, "OPEN"))
        view_calls = [c for c in self.gh.calls if c[0][:2] == ["pr", "view"]]
        self.assertEqual(view_calls[0][1], cwd)

    def test_urlless_link_without_a_cwd_is_never_awaits_you(self):
        add_session(self.conn, branch="main", cwd=str(self.tmp_path / "gone"))
        self.report("#5")
        self.refresh()
        row = link_row(self.conn, "#5")
        self.assertIsNone(row["url"])
        self.assertEqual(self.gh.count(["pr", "view"]), 0)
        session = dict(self.conn.execute("SELECT * FROM sessions").fetchone())
        self.assertFalse(links.pr_awaits_you(row, session, NOW))

    def test_throttle_is_honoured(self):
        add_session(self.conn, branch="main")
        url = self.gh.add_pr(3)
        self.report("#3", url, when=NOW - timedelta(hours=1))
        self.refresh()
        self.assertEqual(self.gh.count(["pr", "view"]), 1)
        self.refresh(when=NOW + timedelta(minutes=5))
        self.assertEqual(self.gh.count(["pr", "view"]), 1)
        self.refresh(when=NOW + timedelta(minutes=links.GH_CACHE_MINUTES + 1))
        self.assertEqual(self.gh.count(["pr", "view"]), 2)

    def test_one_pr_linked_to_two_sessions_is_fetched_once(self):
        add_session(self.conn, session_id="s1", branch="main")
        add_session(self.conn, session_id="s2", branch="main")
        url = self.gh.add_pr(3, state="MERGED", merged=NOW)
        self.report("#3", url, session_id="s1")
        self.report("#3", url, session_id="s2")
        self.refresh()
        self.assertEqual(self.gh.count(["pr", "view"]), 1)
        self.assertEqual(link_row(self.conn, "#3", "s1")["state"], "MERGED")
        self.assertEqual(link_row(self.conn, "#3", "s2")["state"], "MERGED")

    def test_manual_done_and_old_sessions_are_skipped(self):
        add_session(self.conn, session_id="done", branch="main")
        self.conn.execute(
            "INSERT INTO triage(tool, session_id, status, updated_at) VALUES ('claude','done','done','t')"
        )
        add_session(self.conn, session_id="old", branch="main", started_at=iso(NOW - timedelta(days=30)),
                    updated_at=iso(NOW - timedelta(days=20)))
        url = self.gh.add_pr(3)
        self.report("#3", url, session_id="done")
        links.upsert_link(self.conn, "claude", "old",
                          {"kind": "pr", "ref": "#3", "url": url, "state": "OPEN",
                           "responded_at": "2026-05-01T00:00:00Z"},
                          iso(NOW - timedelta(days=20)), source="gh")
        self.refresh()
        self.assertEqual(self.gh.count(["pr", "view"]), 0)

    def test_old_session_with_an_awaiting_pr_is_still_refreshed(self):
        add_session(self.conn, branch="main", started_at=iso(NOW - timedelta(days=30)),
                    updated_at=iso(NOW - timedelta(days=20)))
        url = self.gh.add_pr(3)
        self.report("#3", url, when=NOW - timedelta(days=20))
        self.refresh()
        self.assertEqual(self.gh.count(["pr", "view"]), 1)

    def test_cli_link_keeps_its_branch_after_the_session_moves_on(self):
        add_session(self.conn, branch="feature-x")
        url = self.gh.add_pr(1, branch="feature-x")
        cli.main(["link", "--kind", "pr", "--ref", "#1", "--url", url, "--state", "OPEN",
                  "--session", "claude:s1"])
        self.conn.execute("UPDATE links SET checked_at=?", (iso(NOW - timedelta(hours=1)),))
        self.conn.execute("UPDATE sessions SET branch='feature-y'")
        self.gh.prs[url].update(state="MERGED", mergedAt=gh_iso(NOW - timedelta(minutes=5)),
                                closedAt=gh_iso(NOW - timedelta(minutes=5)))
        row = link_row(self.conn)
        self.assertTrue(links.refresh_pr_link(self.conn, row, self.gh))
        row = link_row(self.conn)
        self.assertEqual((row["state"], row["branch"]), ("MERGED", "feature-x"))
        session = dict(self.conn.execute("SELECT * FROM sessions").fetchone())
        state = reconcile.compute_state(session, None, [row], now=NOW, alive=True)
        self.assertNotEqual(state[0], "done")
        self.assertIsNone(links.links_done_reason([row], "feature-y"))

    def test_gh_failure_leaves_the_link_alone(self):
        add_session(self.conn, branch="main")
        self.report("#3", "https://github.com/acme/widgets/pull/3")
        self.refresh()
        row = link_row(self.conn, "#3")
        self.assertEqual(row["state"], "OPEN")


class ParseJsonStreamTests(AgentBoardTestCase):
    def test_concatenated_pages_merge(self):
        self.assertEqual(links._parse_json_stream("[1, 2][3]\n[]"), [1, 2, 3])

    def test_single_object(self):
        self.assertEqual(links._parse_json_stream('{"a": 1}'), {"a": 1})

    def test_garbage_raises(self):
        with self.assertRaises(ValueError):
            links._parse_json_stream("nope")


class LastParentPromptTimesTests(AgentBoardTestCase):
    def test_batch_matches_the_per_session_lookup(self):
        rows = [
            ("claude", "s1", 50, "UserPromptSubmit", None),
            ("claude", "s1", 40, "UserPromptSubmit", None),
            ("claude", "s1", 30, "UserPromptSubmit", "agent-1"),  # subagent: ignored
            ("claude", "s1", 20, "Stop", None),
            ("codex", "s1", 10, "UserPromptSubmit", None),
            ("claude", "s2", 5, "Stop", None),
        ]
        for tool, session_id, minutes, event, agent_id in rows:
            self.conn.execute(
                "INSERT INTO events(ts, tool, session_id, event, agent_id) VALUES (?,?,?,?,?)",
                (iso(NOW - timedelta(minutes=minutes)), tool, session_id, event, agent_id),
            )
        batch = links.last_parent_prompt_times(self.conn)
        self.assertEqual(batch, {
            ("claude", "s1"): iso(NOW - timedelta(minutes=40)),
            ("codex", "s1"): iso(NOW - timedelta(minutes=10)),
        })
        for key in (("claude", "s1"), ("codex", "s1"), ("claude", "s2")):
            self.assertEqual(batch.get(key), links.last_parent_prompt_at(self.conn, *key))

    def test_injected_prompts_are_skipped_unless_any_kind(self):
        rows = [(50, "human"), (40, None), (30, "loop"), (20, "injected"), (10, "injected")]
        for minutes, kind in rows:
            self.conn.execute(
                "INSERT INTO events(ts, tool, session_id, event, prompt_kind) "
                "VALUES (?, 'claude', 's1', 'UserPromptSubmit', ?)",
                (iso(NOW - timedelta(minutes=minutes)), kind),
            )
        newest_reply = iso(NOW - timedelta(minutes=30))
        newest_any = iso(NOW - timedelta(minutes=10))
        self.assertEqual(links.last_parent_prompt_times(self.conn), {("claude", "s1"): newest_reply})
        self.assertEqual(links.last_parent_prompt_at(self.conn, "claude", "s1"), newest_reply)
        self.assertEqual(links.last_parent_prompt_times_any(self.conn),
                         {("claude", "s1"): newest_any})
        self.assertEqual(links.last_parent_prompt_at(self.conn, "claude", "s1", any_kind=True),
                         newest_any)

    def test_injected_prompt_does_not_release_a_needs_you_note(self):
        from agentboard import cli

        self.conn.execute(
            "INSERT INTO sessions(tool, session_id, first_prompt, last_event, last_event_at, "
            "updated_at, interactive) VALUES ('claude', 's1', 'go', 'Stop', ?, ?, 1)",
            (iso(NOW - timedelta(minutes=5)), iso(NOW - timedelta(minutes=5))),
        )
        self.conn.execute(
            "INSERT INTO notes(tool, session_id, ts, last_ts, kind, text, fingerprint, source) "
            "VALUES ('claude', 's1', ?, ?, 'needs_you', 'USER_ACTION: approve the plan', "
            "'fp', 'stop')",
            (iso(NOW - timedelta(minutes=5)), iso(NOW - timedelta(minutes=5))),
        )
        self.conn.execute(
            "INSERT INTO events(ts, tool, session_id, event, text, prompt_kind) "
            "VALUES (?, 'claude', 's1', 'UserPromptSubmit', '<task-notification>x', 'injected')",
            (iso(NOW - timedelta(minutes=1)),),
        )
        entry = cli.build_snapshot(self.conn, include_all=True, now=NOW)["sessions"][0]
        self.assertEqual((entry["state"], entry["waiting_reason"]),
                         ("needs_you", "USER_ACTION: approve the plan"))
        # A real reply releases it.
        self.conn.execute(
            "INSERT INTO events(ts, tool, session_id, event, text, prompt_kind) "
            "VALUES (?, 'claude', 's1', 'UserPromptSubmit', 'approved', 'human')",
            (iso(NOW - timedelta(seconds=30)),),
        )
        entry = cli.build_snapshot(self.conn, include_all=True, now=NOW)["sessions"][0]
        self.assertNotEqual(entry["state"], "needs_you")


if __name__ == "__main__":
    import unittest

    unittest.main()


class TurnHelperTests(AgentBoardTestCase):
    def _event(self, minutes, event, session_id="s1", tool="claude", agent_id=None,
               extra=None, kind=None):
        self.conn.execute(
            "INSERT INTO events(ts, tool, session_id, event, agent_id, extra, prompt_kind) "
            "VALUES (?,?,?,?,?,?,?)",
            (iso(NOW - timedelta(minutes=minutes)), tool, session_id, event, agent_id,
             json.dumps(extra) if extra is not None else None, kind),
        )

    def test_last_parent_stop_times(self):
        self._event(50, "UserPromptSubmit")
        self._event(40, "Stop", extra={"background_tasks": 3})
        self._event(30, "Stop", extra={"background_tasks": 1, "session_crons": 0})
        self._event(20, "Stop", agent_id="sub-1", extra={"background_tasks": 9})  # subagent
        self._event(10, "Stop", tool="codex", extra={"model": "gpt"})
        self._event(5, "UserPromptSubmit", session_id="s2")  # no Stop: absent
        self.assertEqual(links.last_parent_stop_times(self.conn), {
            ("claude", "s1"): (iso(NOW - timedelta(minutes=30)), 1),
            ("codex", "s1"): (iso(NOW - timedelta(minutes=10)), None),
        })

    def test_malformed_extra_reads_as_none(self):
        self.conn.execute(
            "INSERT INTO events(ts, tool, session_id, event, extra) "
            "VALUES (?, 'claude', 's1', 'Stop', 'not json')", (iso(NOW),))
        self.assertEqual(links.last_parent_stop_times(self.conn),
                         {("claude", "s1"): (iso(NOW), None)})

    def test_last_parent_prompt_kinds(self):
        self._event(50, "UserPromptSubmit", kind="human")
        self._event(40, "UserPromptSubmit", kind="injected")
        self._event(30, "UserPromptSubmit", agent_id="sub-1", kind="human")  # subagent
        self._event(20, "UserPromptSubmit", session_id="s2", kind="human")
        self._event(10, "UserPromptSubmit", session_id="s2")  # legacy row
        self._event(5, "Stop", session_id="s3")  # no prompt: absent
        self.assertEqual(links.last_parent_prompt_kinds(self.conn), {
            ("claude", "s1"): "injected",
            ("claude", "s2"): None,
        })

    def test_background_wait_before_newest_prompt(self):
        # s1: Stop(bg=1), SubagentStop, injected report-back.
        self._event(50, "UserPromptSubmit", kind="human")
        self._event(45, "Stop", extra={"background_tasks": 1})
        self._event(44, "SubagentStop", agent_id="bg-1")
        self._event(40, "UserPromptSubmit", kind="injected")
        self._event(39, "Stop", extra={"background_tasks": 0})
        # s2: a peer message after a clean Stop.
        self._event(50, "UserPromptSubmit", session_id="s2", kind="human")
        self._event(45, "Stop", session_id="s2", extra={"background_tasks": 0})
        self._event(40, "UserPromptSubmit", session_id="s2", kind="injected")
        # s3: a human prompt after a background Stop.
        self._event(50, "UserPromptSubmit", session_id="s3", kind="human")
        self._event(45, "Stop", session_id="s3", extra={"background_tasks": 2})
        self._event(40, "UserPromptSubmit", session_id="s3", kind="human")
        # s4: injected with no Stop before it.
        self._event(40, "UserPromptSubmit", session_id="s4", kind="injected")
        waits = links.background_wait_before_newest_prompt(self.conn)
        self.assertTrue(waits[("claude", "s1")])
        self.assertFalse(waits[("claude", "s2")])
        self.assertNotIn(("claude", "s3"), waits)
        self.assertFalse(waits[("claude", "s4")])
        # The same answer from a newest_parent_prompts map loaded once.
        newest = links.newest_parent_prompts(self.conn)
        self.assertEqual(links.background_wait_before_newest_prompt(self.conn, newest), waits)

    def test_newest_parent_prompts_feeds_the_derived_maps(self):
        self._event(50, "UserPromptSubmit", kind="human")
        self._event(40, "UserPromptSubmit", kind="injected")
        self._event(30, "UserPromptSubmit", agent_id="sub-1", kind="human")  # subagent
        self._event(20, "UserPromptSubmit", session_id="s2")  # legacy row
        self._event(5, "Stop", session_id="s3")  # no prompt: absent
        newest = links.newest_parent_prompts(self.conn)
        self.assertEqual(set(newest), {("claude", "s1"), ("claude", "s2")})
        prompt_id, ts, kind = newest[("claude", "s1")]
        self.assertEqual((ts, kind), (iso(NOW - timedelta(minutes=40)), "injected"))
        self.assertEqual(prompt_id, self.conn.execute(
            "SELECT id FROM events WHERE agent_id IS NULL AND session_id='s1' "
            "ORDER BY id DESC LIMIT 1").fetchone()[0])
        self.assertEqual(links.last_parent_prompt_times_any(self.conn, newest),
                         links.last_parent_prompt_times_any(self.conn))
        self.assertEqual(links.last_parent_prompt_kinds(self.conn, newest),
                         {("claude", "s1"): "injected", ("claude", "s2"): None})
