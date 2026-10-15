"""Schema and connection management for the AgentBoard sqlite database."""

import os
import sqlite3
from pathlib import Path

DB_FILENAME = "agentboard.db"

SCHEMA_VERSION = 4

_MIGRATIONS = {
    1: """
    CREATE TABLE events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        tool TEXT NOT NULL,
        session_id TEXT NOT NULL,
        event TEXT NOT NULL,
        agent_id TEXT,
        notification_type TEXT,
        tool_name TEXT,
        text TEXT,
        extra TEXT
    );
    CREATE INDEX idx_events_session_ts ON events(tool, session_id, ts);
    CREATE INDEX idx_events_session_parent_ts
        ON events(tool, session_id, ts) WHERE agent_id IS NULL;

    CREATE TABLE sessions(
        tool TEXT NOT NULL,
        session_id TEXT NOT NULL,
        cwd TEXT,
        branch TEXT,
        title TEXT,
        first_prompt TEXT,
        last_prompt TEXT,
        last_assistant TEXT,
        started_at TEXT,
        updated_at TEXT,
        last_event TEXT,
        last_event_at TEXT,
        waiting_reason TEXT,
        pid INTEGER,
        iterm_session_id TEXT,
        term_program TEXT,
        transcript_path TEXT,
        origin TEXT,
        interactive INTEGER,
        ended_at TEXT,
        PRIMARY KEY(tool, session_id)
    );

    CREATE TABLE triage(
        tool TEXT NOT NULL,
        session_id TEXT NOT NULL,
        status TEXT NOT NULL,
        until TEXT,
        note TEXT,
        updated_at TEXT,
        PRIMARY KEY(tool, session_id)
    );

    CREATE TABLE links(
        tool TEXT NOT NULL,
        session_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        ref TEXT NOT NULL,
        url TEXT,
        state TEXT,
        title TEXT,
        checked_at TEXT,
        PRIMARY KEY(tool, session_id, kind, ref)
    );
    """,
    2: """
    ALTER TABLE sessions ADD COLUMN status_text TEXT;
    ALTER TABLE sessions ADD COLUMN status_until TEXT;
    """,
    3: """
    ALTER TABLE sessions ADD COLUMN tty TEXT;
    """,
    4: """
    ALTER TABLE sessions ADD COLUMN end_reason TEXT;
    ALTER TABLE sessions ADD COLUMN todos_open INTEGER;
    """,
}


def home_dir() -> Path:
    """Resolve the AgentBoard data directory, honoring AGENTBOARD_HOME."""
    override = os.environ.get("AGENTBOARD_HOME")
    if override:
        return Path(override)
    return Path.home() / "Library" / "Application Support" / "AgentBoard"


def db_path() -> Path:
    return home_dir() / DB_FILENAME


def _migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version in sorted(_MIGRATIONS):
        if version <= current:
            continue
        conn.executescript(_MIGRATIONS[version])
        conn.execute(f"PRAGMA user_version = {version}")


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open (creating and migrating if needed) the AgentBoard database."""
    if path is None:
        path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    _migrate(conn)
    return conn
