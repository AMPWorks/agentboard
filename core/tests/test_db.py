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
        self.assertTrue({"events", "sessions", "triage", "links"} <= tables)

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

    def test_db_file_created_under_agentboard_home(self):
        self.assertTrue((self.agentboard_home / "agentboard.db").exists())


if __name__ == "__main__":
    import unittest

    unittest.main()
