import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentboard import db
from tests.helpers import AgentBoardTestCase


class DbTests(AgentBoardTestCase):
    def test_home_dir_honors_env(self):
        self.assertEqual(db.home_dir(), self.agentboard_home)

    def test_connect_creates_schema(self):
        tables = {
            row["name"]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertTrue({"events", "sessions", "triage", "links", "notes", "notes_meta"} <= tables)

    def test_schema_version_set(self):
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, db.SCHEMA_VERSION)

    def test_connect_is_idempotent(self):
        conn2 = db.connect()
        try:
            version = conn2.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(version, db.SCHEMA_VERSION)
        finally:
            conn2.close()

    def test_status_columns_present(self):
        cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(sessions)")}
        self.assertIn("status_text", cols)
        self.assertIn("status_until", cols)

    def test_notes_table_and_indexes_present(self):
        cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(notes)")}
        self.assertTrue({
            "id", "tool", "session_id", "ts", "last_ts", "kind", "text",
            "fingerprint", "count", "source", "event_id", "last_event_id",
            "dismissed_at",
        } <= cols)
        indexes = {
            row["name"]: row["sql"]
            for row in self.conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='notes'"
            )
        }
        self.assertIn("idx_notes_fp", indexes)
        self.assertIn("UNIQUE", indexes["idx_notes_fp"])
        self.assertIn("WHERE dismissed_at IS NULL", indexes["idx_notes_fp"])
        self.assertIn("idx_notes_open", indexes)

    def test_notes_unique_index_only_covers_open_notes(self):
        insert = (
            "INSERT INTO notes(tool, session_id, ts, last_ts, kind, text, fingerprint, source, dismissed_at)"
            " VALUES ('claude', 's1', 't', 't', 'update', 'x', 'fp', 'cli', ?)"
        )
        self.conn.execute(insert, ("2026-01-01T00:00:00Z",))
        self.conn.execute(insert, ("2026-01-02T00:00:00Z",))
        self.conn.execute(insert, (None,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(insert, (None,))

    def test_notes_meta_seeded_on_fresh_db(self):
        meta = dict(self.conn.execute("SELECT key, value FROM notes_meta").fetchall())
        self.assertEqual(meta, {"hook_from": 0, "rebuilt_through": 0})

    def test_migration_5_records_hook_from_boundary_over_existing_events(self):
        path = self.tmp_path / "v4.db"
        old = sqlite3.connect(str(path), isolation_level=None)
        for version in (1, 2, 3, 4):
            old.executescript(db._MIGRATIONS[version])
        old.execute("PRAGMA user_version = 4")
        for _ in range(3):
            old.execute(
                "INSERT INTO events(ts, tool, session_id, event) VALUES ('t', 'claude', 's', 'Stop')"
            )
        old.close()
        conn = db.connect(path)
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
            meta = dict(conn.execute("SELECT key, value FROM notes_meta").fetchall())
            self.assertEqual(meta, {"hook_from": 3, "rebuilt_through": 0})
        finally:
            conn.close()

    def test_migration_6_adds_lifecycle_columns_to_a_v5_database(self):
        path = self.tmp_path / "v5.db"
        old = sqlite3.connect(str(path), isolation_level=None)
        for version in (1, 2, 3, 4, 5):
            old.executescript(db._MIGRATIONS[version])
        old.execute("PRAGMA user_version = 5")
        old.execute(
            "INSERT INTO sessions(tool, session_id, title, branch) "
            "VALUES ('claude', 's1', 'old title', 'feature-x')"
        )
        old.execute(
            "INSERT INTO links(tool, session_id, kind, ref, url, state, checked_at) "
            "VALUES ('claude', 's1', 'pr', '#1', 'https://x/1', 'MERGED', 't')"
        )
        old.close()
        conn = db.connect(path)
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 6)
            link = conn.execute("SELECT * FROM links").fetchone()
            self.assertEqual((link["ref"], link["url"], link["state"]),
                             ("#1", "https://x/1", "MERGED"))
            for col in ("branch", "closed_at", "source", "created_at", "responded_at",
                        "self_authored"):
                self.assertIsNone(link[col], col)
            session = conn.execute("SELECT * FROM sessions").fetchone()
            self.assertEqual((session["title"], session["branch"]), ("old title", "feature-x"))
            for col in ("title_source", "retitled_at", "retitle_prompt_count", "retitle_branch"):
                self.assertIsNone(session[col], col)
        finally:
            conn.close()

    def test_db_file_created_under_agentboard_home(self):
        self.assertTrue((self.agentboard_home / "agentboard.db").exists())


if __name__ == "__main__":
    import unittest

    unittest.main()
