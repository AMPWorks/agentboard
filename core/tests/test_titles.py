import io
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentboard import cli, hook, links, titles
from tests.helpers import AgentBoardTestCase

NOW = datetime.now(timezone.utc).replace(microsecond=0)
COMMAND = ["retitler", "--fast"]


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class FakeRunner:
    def __init__(self, output="New work title\n", error=None):
        self.output = output
        self.error = error
        self.calls = []

    def __call__(self, command, context, timeout=None, cwd=None):
        self.calls.append((command, context, timeout))
        if self.error is not None:
            raise self.error
        return self.output


class RetitleTests(AgentBoardTestCase):
    def setUp(self):
        super().setUp()
        self.config = {"retitle_command": COMMAND}

    def session(self, session_id="s1", prompts=5, minutes_ago=5, **overrides):
        row = {
            "tool": "claude", "session_id": session_id, "cwd": "/tmp/proj", "branch": "feature-x",
            "title": "PR #4 author investigation", "title_source": "ai",
            "started_at": iso(NOW - timedelta(days=1)),
            "updated_at": iso(NOW - timedelta(minutes=minutes_ago)),
            "last_event": "Stop", "origin": "hook", "interactive": 1,
        }
        row.update(overrides)
        cols = ",".join(row)
        self.conn.execute(f"INSERT INTO sessions({cols}) VALUES ({','.join('?' for _ in row)})",
                          list(row.values()))
        for i in range(prompts):
            self.prompt(session_id, f"prompt {i}", minutes_ago=minutes_ago + 60 - i)

    def prompt(self, session_id, text, minutes_ago=1, agent_id=None):
        self.conn.execute(
            "INSERT INTO events(ts, tool, session_id, event, agent_id, text) "
            "VALUES (?, 'claude', ?, 'UserPromptSubmit', ?, ?)",
            (iso(NOW - timedelta(minutes=minutes_ago)), session_id, agent_id, text),
        )

    def row(self, session_id="s1"):
        return dict(self.conn.execute(
            "SELECT * FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone())

    def retitle(self, runner=None, config=None, now=NOW):
        runner = runner or FakeRunner()
        count = titles.retitle_sessions(self.conn, self.config if config is None else config,
                                        runner=runner, now=now)
        return count, runner

    def log_lines(self):
        path = self.agentboard_home / "agentboard.log"
        return path.read_text().splitlines() if path.exists() else []

    def test_five_new_prompts_retitle_and_update_counters(self):
        self.session(prompts=5)
        count, runner = self.retitle()
        self.assertEqual(count, 1)
        row = self.row()
        self.assertEqual((row["title"], row["title_source"]), ("New work title", "generated"))
        self.assertEqual(row["retitle_prompt_count"], 5)
        self.assertEqual(row["retitle_branch"], "feature-x")
        self.assertIsNotNone(row["retitled_at"])
        command, context, timeout = runner.calls[0]
        self.assertEqual(command, COMMAND)
        self.assertEqual(timeout, titles.RETITLE_TIMEOUT_SECS)
        self.assertIn("Current title: PR #4 author investigation", context)
        self.assertIn("Branch: feature-x", context)
        self.assertIn("User: prompt 4", context)

    def test_two_new_prompts_do_not(self):
        self.session(prompts=2)
        count, runner = self.retitle()
        self.assertEqual((count, runner.calls), (0, []))

    def test_subagent_prompts_do_not_count(self):
        self.session(prompts=3)
        for i in range(3):
            self.prompt("s1", "sub", agent_id="a1")
        self.assertEqual(self.retitle()[0], 0)

    def test_injected_prompts_do_not_count_or_reach_the_context(self):
        self.session(prompts=3)
        for i in range(3):
            self.prompt("s1", f"<task-notification>task {i} done</task-notification>")
        self.conn.execute("UPDATE events SET prompt_kind='injected' WHERE text LIKE '<task-%'")
        self.assertEqual(titles.parent_prompt_count(self.conn, "claude", "s1"), 3)
        self.assertEqual(titles.parent_prompt_counts(self.conn), {("claude", "s1"): 3})
        self.assertEqual(self.retitle()[0], 0)
        self.prompt("s1", "a real prompt")
        self.prompt("s1", "another real prompt")
        count, runner = self.retitle()
        self.assertEqual(count, 1)
        self.assertNotIn("task-notification", runner.calls[0][1])

    def test_counts_are_relative_to_the_last_retitle(self):
        self.session(prompts=7, retitle_prompt_count=5, retitle_branch="feature-x",
                     retitled_at=iso(NOW - timedelta(days=1)))
        self.assertEqual(self.retitle()[0], 0)
        for i in range(3):
            self.prompt("s1", f"more {i}")
        self.assertEqual(self.retitle()[0], 1)
        self.assertEqual(self.row()["retitle_prompt_count"], 10)

    def test_branch_change_triggers_a_retitle(self):
        self.session(prompts=6, retitle_prompt_count=5, retitle_branch="feature-old",
                     retitled_at=iso(NOW - timedelta(days=1)))
        self.assertEqual(self.retitle()[0], 1)
        self.assertEqual(self.row()["retitle_branch"], "feature-x")

    def test_custom_title_is_never_replaced(self):
        self.session(prompts=20, title="My name", title_source="custom")
        count, runner = self.retitle()
        self.assertEqual((count, runner.calls), (0, []))
        self.assertEqual(self.row()["title"], "My name")

    def test_failing_command_is_logged_and_leaves_the_title(self):
        self.session(prompts=5)
        error = subprocess.CalledProcessError(2, COMMAND, "", "not logged in")
        count, _ = self.retitle(runner=FakeRunner(error=error))
        self.assertEqual(count, 0)
        row = self.row()
        self.assertEqual((row["title"], row["title_source"]), ("PR #4 author investigation", "ai"))
        self.assertIsNone(row["retitle_prompt_count"])
        line = next(l for l in self.log_lines() if "titles.retitle" in l)
        self.assertIn("WARN", line)
        self.assertIn("outcome=nonzero", line)
        self.assertIn("not logged in", line)

    def test_slow_command_is_logged_and_retried_only_after_the_interval(self):
        self.session(prompts=5)
        error = subprocess.TimeoutExpired(COMMAND, titles.RETITLE_TIMEOUT_SECS)
        self.assertEqual(self.retitle(runner=FakeRunner(error=error))[0], 0)
        self.assertEqual(self.row()["title"], "PR #4 author investigation")
        self.assertTrue(any("outcome=timeout" in l for l in self.log_lines()))
        count, runner = self.retitle(now=NOW + timedelta(minutes=10))
        self.assertEqual((count, runner.calls), (0, []), "no retry within the interval")
        later = NOW + timedelta(minutes=titles.MIN_RETITLE_INTERVAL_MINUTES + 1)
        self.assertEqual(self.retitle(now=later)[0], 1)

    def test_empty_output_leaves_the_title(self):
        self.session(prompts=5)
        self.assertEqual(self.retitle(runner=FakeRunner(output="\n  \n"))[0], 0)
        self.assertEqual(self.row()["title_source"], "ai")

    def test_null_retitle_command_disables_it(self):
        self.session(prompts=5)
        count, runner = self.retitle(config={"retitle_command": None})
        self.assertEqual((count, runner.calls), (0, []))

    def test_per_run_cap_picks_the_most_recently_active(self):
        for i, minutes in enumerate((50, 5, 30, 10)):
            self.session(f"s{i}", prompts=5, minutes_ago=minutes)
        config = dict(self.config, retitle_max_per_run=2)
        count, runner = self.retitle(config=config)
        self.assertEqual(count, 2)
        retitled = {r["session_id"] for r in self.conn.execute(
            "SELECT session_id FROM sessions WHERE title_source='generated'")}
        self.assertEqual(retitled, {"s1", "s3"})

    def test_done_ended_old_and_noninteractive_sessions_are_skipped(self):
        self.session("manual", prompts=5)
        self.conn.execute("INSERT INTO triage(tool, session_id, status, updated_at) "
                          "VALUES ('claude','manual','done','t')")
        self.session("ended", prompts=5, ended_at=iso(NOW - timedelta(minutes=1)))
        self.session("old", prompts=5, minutes_ago=60 * 24 * 10)
        self.session("headless", prompts=5, interactive=0)
        self.session("merged", prompts=5)
        links.upsert_link(self.conn, "claude", "merged",
                          {"kind": "pr", "ref": "#1", "state": "MERGED", "branch": "feature-x"},
                          iso(NOW - timedelta(minutes=2)))
        count, runner = self.retitle()
        self.assertEqual((count, runner.calls), (0, []))

    def test_min_prompts_is_configurable(self):
        self.session(prompts=2)
        self.assertEqual(self.retitle(config=dict(self.config, retitle_min_prompts=2))[0], 1)

    def test_context_is_capped(self):
        self.session(prompts=0)
        for i in range(12):
            self.prompt("s1", "x" * 2000, minutes_ago=20 - i)
        context = titles.build_context(self.conn, self.row())
        self.assertLessEqual(len(context), titles.CONTEXT_MAX_CHARS)
        self.assertIn("Current title:", context)

    def test_clean_title(self):
        self.assertEqual(titles.clean_title('\n**Title: "Fix the hook."**\nmore'), "Fix the hook")
        self.assertEqual(len(titles.clean_title("y" * 200)), titles.TITLE_MAX_CHARS)
        self.assertIsNone(titles.clean_title(""))

    def test_refresh_links_runs_the_retitle(self):
        self.session(prompts=5)
        (self.agentboard_home / "config.json").write_text(json.dumps(self.config))
        runner = FakeRunner("From refresh")
        with mock.patch("agentboard.links.gh_available", return_value=False):
            links.refresh_links(self.conn, home=self.agentboard_home, now=NOW,
                                retitle_runner=runner)
        self.assertEqual(self.row()["title"], "From refresh")

    def test_snapshot_never_retitles(self):
        self.session(prompts=5)
        (self.agentboard_home / "config.json").write_text(json.dumps(self.config))
        with mock.patch("agentboard.titles.retitle_sessions", side_effect=AssertionError), \
                mock.patch("agentboard.titles.run_command", side_effect=AssertionError):
            cli.build_snapshot(self.conn, include_all=True)
        self.assertEqual(self.row()["title_source"], "ai")


class DefaultCommandTests(AgentBoardTestCase):
    def test_default_is_claude_print_mode_with_haiku_when_on_path(self):
        with mock.patch("agentboard.titles.shutil.which", return_value="/x/claude"):
            command = titles.retitle_command({})
        self.assertEqual(command[:4], ["/x/claude", "-p", "--model", "haiku"])
        self.assertEqual(command[-1], titles.RETITLE_INSTRUCTION)

    def test_default_runs_with_no_tools(self):
        with mock.patch("agentboard.titles.shutil.which", return_value="/x/claude"):
            command = titles.retitle_command({})
        index = command.index("--tools")
        self.assertEqual(command[index + 1], "")
        # `--tools` is variadic: the next token must be a flag, or it
        # would swallow the instruction as a tool name.
        self.assertTrue(command[index + 2].startswith("--"))
        self.assertIn("--strict-mcp-config", command)

    def test_default_falls_back_to_known_install_locations(self):
        fake = self.fake_home / ".local" / "bin" / "claude"
        fake.parent.mkdir(parents=True)
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        with mock.patch("agentboard.titles.shutil.which", return_value=None):
            self.assertEqual(titles.retitle_command({})[0], str(fake))

    def test_no_claude_means_no_retitling(self):
        with mock.patch("agentboard.titles.shutil.which", return_value=None), \
                mock.patch("agentboard.titles.CLAUDE_CLI_CANDIDATES", ()):
            self.assertIsNone(titles.retitle_command({}))

    def test_string_command_is_split(self):
        self.assertEqual(titles.retitle_command({"retitle_command": "llm -m small"}),
                         ["llm", "-m", "small"])
        self.assertIsNone(titles.retitle_command({"retitle_command": 5}))

    def test_run_command_sets_the_hook_skip_env(self):
        out = titles.run_command(
            [sys.executable, "-c",
             "import os,sys; sys.stdout.write(os.environ.get('AGENTBOARD_HOOK_SKIP','') + sys.stdin.read())"],
            "ctx", timeout=10)
        self.assertEqual(out, "1ctx")
        with self.assertRaises(subprocess.CalledProcessError):
            titles.run_command([sys.executable, "-c", "raise SystemExit(3)"], "", timeout=10)

    def test_hook_records_nothing_while_skip_env_is_set(self):
        payload = json.dumps({"session_id": "retitle-run", "cwd": "/tmp",
                              "hook_event_name": "SessionStart"})
        with mock.patch.dict(os.environ, {titles.HOOK_SKIP_ENV: "1"}):
            hook.run("claude", io.StringIO(payload))
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM sessions WHERE session_id='retitle-run'").fetchone())
        self.assertEqual(hook.HOOK_SKIP_ENV, titles.HOOK_SKIP_ENV)


class TitleCommandTests(AgentBoardTestCase):
    def setUp(self):
        super().setUp()
        self.conn.execute(
            "INSERT INTO sessions(tool, session_id, branch, title, title_source, origin, updated_at) "
            "VALUES ('claude','s1','feature-x','Old','ai','hook',?)", (iso(NOW),),
        )

    def row(self):
        return dict(self.conn.execute("SELECT * FROM sessions WHERE session_id='s1'").fetchone())

    def test_title_command_sets_a_cli_title_and_resets_the_baseline(self):
        for i in range(3):
            self.conn.execute("INSERT INTO events(ts, tool, session_id, event) "
                              "VALUES (?, 'claude', 's1', 'UserPromptSubmit')", (iso(NOW),))
        rc = cli.main(["title", "--text", "  Handler:  PR #9  ", "--session", "claude:s1"])
        self.assertEqual(rc, 0)
        row = self.row()
        self.assertEqual((row["title"], row["title_source"]), ("Handler: PR #9", "cli"))
        self.assertEqual((row["retitle_prompt_count"], row["retitle_branch"]), (3, "feature-x"))

    def test_title_command_keeps_a_custom_title(self):
        self.conn.execute("UPDATE sessions SET title='Mine', title_source='custom'")
        self.assertEqual(cli.main(["title", "--text", "Other", "--session", "claude:s1"]), 0)
        self.assertEqual(self.row()["title"], "Mine")

    def test_generated_retitle_replaces_a_cli_title_once_due(self):
        cli.main(["title", "--text", "Named by skill", "--session", "claude:s1"])
        config = {"retitle_command": COMMAND}
        runner = FakeRunner("Generated")
        later = NOW + timedelta(hours=2)
        self.assertEqual(titles.retitle_sessions(self.conn, config, runner=runner, now=later), 0)
        for i in range(5):
            self.conn.execute("INSERT INTO events(ts, tool, session_id, event, text) "
                              "VALUES (?, 'claude', 's1', 'UserPromptSubmit', 'p')", (iso(NOW),))
        self.assertEqual(titles.retitle_sessions(self.conn, config, runner=runner, now=later), 1)
        self.assertEqual((self.row()["title"], self.row()["title_source"]), ("Generated", "generated"))

    def test_empty_text_is_an_error(self):
        self.assertEqual(cli.main(["title", "--text", "  ", "--session", "claude:s1"]), 1)


if __name__ == "__main__":
    import unittest

    unittest.main()
