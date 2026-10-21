import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentboard import codexmap
from tests.helpers import AgentBoardTestCase


def insert_codex_session(conn, session_id, **overrides):
    row = {"tool": "codex", "session_id": session_id}
    row.update(overrides)
    cols = ",".join(row.keys())
    placeholders = ",".join("?" for _ in row)
    conn.execute(f"INSERT INTO sessions({cols}) VALUES ({placeholders})", list(row.values()))


def make_ps_run(processes=(), envs=None):
    """processes: [(pid, tty, comm), ...] for the listing call.
    envs: {pid: raw ps-eww-style command string} for the per-pid env call.
    """
    envs = envs or {}

    def ps_run(argv, **kwargs):
        if argv[:2] == ["ps", "-axo"]:
            lines = "\n".join(f"{pid} {tty} {comm}" for pid, tty, comm in processes)
            return mock.Mock(stdout=lines)
        if argv[:2] == ["ps", "eww"]:
            pid = int(argv[-1])
            return mock.Mock(stdout=envs.get(pid, ""))
        raise AssertionError(f"unexpected ps invocation: {argv}")

    return ps_run


def make_lsof_run(cwds):
    """cwds: {pid: cwd_string_or_None}"""

    def lsof_run(argv, **kwargs):
        pid = int(argv[argv.index("-p") + 1])
        cwd = cwds.get(pid)
        if cwd is None:
            return mock.Mock(stdout="")
        return mock.Mock(stdout=f"p{pid}\nn{cwd}\n")

    return lsof_run


def make_stat_fn(times):
    """times: {'/dev/ttysNNN': epoch_float_or_(atime, mtime)}"""

    def stat_fn(path):
        if path not in times:
            raise OSError(f"no such tty: {path}")
        value = times[path]
        atime, mtime = value if isinstance(value, tuple) else (value, value)
        return mock.Mock(st_atime=atime, st_mtime=mtime)

    return stat_fn


NOW_ISO = "2026-06-15T12:00:00.000000Z"
NOW_EPOCH = 1781524800.0  # the epoch instant NOW_ISO parses to (reconcile.parse_ts)


class FindCandidatesTests(AgentBoardTestCase):
    def test_matches_on_normalized_cwd(self):
        ps_run = make_ps_run(processes=[(100, "ttys001", "codex")])
        lsof_run = make_lsof_run({100: "/Users/dev/proj/"})
        candidates = codexmap.find_candidates(
            "/Users/dev/proj", ps_run=ps_run, lsof_run=lsof_run
        )
        self.assertEqual(candidates, [{"pid": 100, "tty": "ttys001"}])

    def test_daemon_process_excluded_even_with_matching_basename(self):
        ps_run = make_ps_run(processes=[
            (1, "??", "/Users/x/.codex/packages/app-server-daemon/releases/1.2.3/bin/codex"),
            (200, "ttys007", "codex"),
        ])
        lsof_run = make_lsof_run({1: "/Users/dev/proj", 200: "/Users/dev/proj"})
        candidates = codexmap.find_candidates(
            "/Users/dev/proj", ps_run=ps_run, lsof_run=lsof_run
        )
        self.assertEqual(candidates, [{"pid": 200, "tty": "ttys007"}])

    def test_no_tty_excluded(self):
        ps_run = make_ps_run(processes=[(300, "??", "codex")])
        lsof_run = make_lsof_run({300: "/Users/dev/proj"})
        candidates = codexmap.find_candidates(
            "/Users/dev/proj", ps_run=ps_run, lsof_run=lsof_run
        )
        self.assertEqual(candidates, [])

    def test_non_matching_cwd_excluded(self):
        ps_run = make_ps_run(processes=[(400, "ttys003", "codex")])
        lsof_run = make_lsof_run({400: "/somewhere/else"})
        candidates = codexmap.find_candidates(
            "/Users/dev/proj", ps_run=ps_run, lsof_run=lsof_run
        )
        self.assertEqual(candidates, [])


class ReadTuiEnvTests(AgentBoardTestCase):
    def test_extracts_only_the_three_known_keys(self):
        raw = (
            "codex SECRET_TOKEN=dontcapture ITERM_SESSION_ID=w0t1p0:ABCD "
            "TERM_PROGRAM=iTerm.app HOME=/Users/x PATH=/usr/bin OTHER=xyz"
        )
        ps_run = make_ps_run(envs={60123: raw})
        env = codexmap.read_tui_env(60123, ps_run=ps_run)
        self.assertEqual(env, {"ITERM_SESSION_ID": "w0t1p0:ABCD", "TERM_PROGRAM": "iTerm.app"})
        self.assertNotIn("SECRET_TOKEN", env)
        self.assertNotIn("HOME", env)

    def test_missing_env_returns_empty(self):
        ps_run = make_ps_run(envs={})
        self.assertEqual(codexmap.read_tui_env(1, ps_run=ps_run), {})


class MapCodexSessionTests(AgentBoardTestCase):
    def test_single_candidate_maps_and_stores_env(self):
        insert_codex_session(self.conn, "sess1")
        ps_run = make_ps_run(
            processes=[(60123, "ttys006", "codex")],
            envs={60123: "codex ITERM_SESSION_ID=w0t1p0:ABCD-1 TERM_PROGRAM=iTerm.app SECRET=x"},
        )
        lsof_run = make_lsof_run({60123: "/Users/dev"})

        mapped = codexmap.map_codex_session(
            self.conn, "sess1", "/Users/dev",
            ps_run=ps_run, lsof_run=lsof_run,
        )

        self.assertTrue(mapped)
        row = self.conn.execute(
            "SELECT pid, tty, iterm_session_id, term_program FROM sessions "
            "WHERE tool='codex' AND session_id='sess1'"
        ).fetchone()
        self.assertEqual(row["pid"], 60123)
        self.assertEqual(row["tty"], "ttys006")
        self.assertEqual(row["iterm_session_id"], "w0t1p0:ABCD-1")
        self.assertEqual(row["term_program"], "iTerm.app")

    def test_successful_map_logs_at_debug_and_misses_at_info(self):
        # Spawned on every Codex session start/prompt: a routine mapping must
        # not add an info line; an unmapped session still does.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENTBOARD_LOG_LEVEL", None)
            insert_codex_session(self.conn, "sess1")
            codexmap.map_codex_session(
                self.conn, "sess1", "/Users/dev",
                ps_run=make_ps_run(processes=[(60123, "ttys006", "codex")],
                                   envs={60123: "codex TERM_PROGRAM=iTerm.app"}),
                lsof_run=make_lsof_run({60123: "/Users/dev"}),
            )
            log = self.agentboard_home / "agentboard.log"
            self.assertFalse(log.exists() and "outcome=mapped" in log.read_text())
            codexmap.map_codex_session(
                self.conn, "sess1", "/Users/dev",
                ps_run=make_ps_run(processes=[]), lsof_run=make_lsof_run({}),
            )
            self.assertIn("INFO codexmap.map session=sess1", log.read_text())
            self.assertIn("outcome=no_candidates", log.read_text())

    def test_no_candidates_does_not_map(self):
        insert_codex_session(self.conn, "sess1")
        ps_run = make_ps_run(processes=[])
        lsof_run = make_lsof_run({})

        mapped = codexmap.map_codex_session(
            self.conn, "sess1", "/Users/dev", ps_run=ps_run, lsof_run=lsof_run
        )

        self.assertFalse(mapped)
        row = self.conn.execute(
            "SELECT pid FROM sessions WHERE tool='codex' AND session_id='sess1'"
        ).fetchone()
        self.assertIsNone(row["pid"])

    def test_multiple_candidates_tty_recency_tiebreak_picks_most_recent(self):
        insert_codex_session(self.conn, "sess1")
        ps_run = make_ps_run(
            processes=[(100, "ttys001", "codex"), (200, "ttys002", "codex")],
            envs={200: "codex TERM_PROGRAM=iTerm.app"},
        )
        lsof_run = make_lsof_run({100: "/Users/dev", 200: "/Users/dev"})
        stat_fn = make_stat_fn({
            "/dev/ttys001": NOW_EPOCH - 20,  # older, but still within the 30s window
            "/dev/ttys002": NOW_EPOCH - 5,   # most recently touched -- should win
        })

        mapped = codexmap.map_codex_session(
            self.conn, "sess1", "/Users/dev", at=NOW_ISO,
            ps_run=ps_run, lsof_run=lsof_run, stat_fn=stat_fn,
        )

        self.assertTrue(mapped)
        row = self.conn.execute(
            "SELECT pid, tty FROM sessions WHERE tool='codex' AND session_id='sess1'"
        ).fetchone()
        self.assertEqual(row["pid"], 200)
        self.assertEqual(row["tty"], "ttys002")

    def test_candidate_older_than_30s_is_excluded(self):
        insert_codex_session(self.conn, "sess1")
        ps_run = make_ps_run(processes=[(100, "ttys001", "codex"), (200, "ttys002", "codex")])
        lsof_run = make_lsof_run({100: "/Users/dev", 200: "/Users/dev"})
        stat_fn = make_stat_fn({
            "/dev/ttys001": NOW_EPOCH - 45,  # outside the 30s window
            "/dev/ttys002": NOW_EPOCH - 10,  # within the window -- should win
        })

        mapped = codexmap.map_codex_session(
            self.conn, "sess1", "/Users/dev", at=NOW_ISO,
            ps_run=ps_run, lsof_run=lsof_run, stat_fn=stat_fn,
        )

        self.assertTrue(mapped)
        row = self.conn.execute(
            "SELECT pid FROM sessions WHERE tool='codex' AND session_id='sess1'"
        ).fetchone()
        self.assertEqual(row["pid"], 200)

    def test_ambiguous_tty_recency_does_not_map(self):
        insert_codex_session(self.conn, "sess1")
        ps_run = make_ps_run(processes=[(100, "ttys001", "codex"), (200, "ttys002", "codex")])
        lsof_run = make_lsof_run({100: "/Users/dev", 200: "/Users/dev"})
        # Both missing a tty activity signal entirely -- no basis to pick one.
        stat_fn = make_stat_fn({})

        mapped = codexmap.map_codex_session(
            self.conn, "sess1", "/Users/dev", at=NOW_ISO,
            ps_run=ps_run, lsof_run=lsof_run, stat_fn=stat_fn,
        )

        self.assertFalse(mapped)
        row = self.conn.execute(
            "SELECT pid FROM sessions WHERE tool='codex' AND session_id='sess1'"
        ).fetchone()
        self.assertIsNone(row["pid"])

    def test_ambiguous_tied_recency_does_not_map(self):
        insert_codex_session(self.conn, "sess1")
        ps_run = make_ps_run(processes=[(100, "ttys001", "codex"), (200, "ttys002", "codex")])
        lsof_run = make_lsof_run({100: "/Users/dev", 200: "/Users/dev"})
        stat_fn = make_stat_fn({
            "/dev/ttys001": NOW_EPOCH - 5,
            "/dev/ttys002": NOW_EPOCH - 5,
        })

        mapped = codexmap.map_codex_session(
            self.conn, "sess1", "/Users/dev", at=NOW_ISO,
            ps_run=ps_run, lsof_run=lsof_run, stat_fn=stat_fn,
        )

        self.assertFalse(mapped)

    def test_already_mapped_elsewhere_with_live_pid_is_deprioritized(self):
        insert_codex_session(self.conn, "sess1")
        insert_codex_session(self.conn, "other-session", pid=100)
        ps_run = make_ps_run(
            processes=[(100, "ttys001", "codex"), (200, "ttys002", "codex")],
            envs={200: "codex TERM_PROGRAM=iTerm.app"},
        )
        lsof_run = make_lsof_run({100: "/Users/dev", 200: "/Users/dev"})

        mapped = codexmap.map_codex_session(
            self.conn, "sess1", "/Users/dev", at=NOW_ISO,
            ps_run=ps_run, lsof_run=lsof_run,
            is_pid_alive_fn=lambda pid: pid == 100,
        )

        self.assertTrue(mapped)
        row = self.conn.execute(
            "SELECT pid FROM sessions WHERE tool='codex' AND session_id='sess1'"
        ).fetchone()
        self.assertEqual(row["pid"], 200)
        # The other session's claim on pid 100 is untouched -- it was only
        # deprioritized as a candidate for *this* session, not stolen from.
        other = self.conn.execute(
            "SELECT pid FROM sessions WHERE tool='codex' AND session_id='other-session'"
        ).fetchone()
        self.assertEqual(other["pid"], 100)

    def test_remap_steals_pid_from_old_session(self):
        insert_codex_session(
            self.conn, "old-session",
            pid=60123, tty="ttys006", iterm_session_id="w0t1p0:OLD", term_program="iTerm.app",
        )
        insert_codex_session(self.conn, "new-session")
        # codex resume: the exact same TUI process now serves a new thread.
        ps_run = make_ps_run(
            processes=[(60123, "ttys006", "codex")],
            envs={60123: "codex ITERM_SESSION_ID=w0t1p0:OLD TERM_PROGRAM=iTerm.app"},
        )
        lsof_run = make_lsof_run({60123: "/Users/dev"})

        mapped = codexmap.map_codex_session(
            self.conn, "new-session", "/Users/dev",
            ps_run=ps_run, lsof_run=lsof_run,
        )

        self.assertTrue(mapped)
        new_row = self.conn.execute(
            "SELECT pid, tty FROM sessions WHERE tool='codex' AND session_id='new-session'"
        ).fetchone()
        self.assertEqual(new_row["pid"], 60123)
        old_row = self.conn.execute(
            "SELECT pid, tty, iterm_session_id, term_program FROM sessions "
            "WHERE tool='codex' AND session_id='old-session'"
        ).fetchone()
        self.assertIsNone(old_row["pid"])
        self.assertIsNone(old_row["tty"])
        self.assertIsNone(old_row["iterm_session_id"])
        self.assertIsNone(old_row["term_program"])
