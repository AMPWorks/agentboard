"""Schema and connection management for the AgentBoard sqlite database."""

import os
import sqlite3
from pathlib import Path

DB_FILENAME = "agentboard.db"

SCHEMA_VERSION = 9

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
    5: """
    CREATE TABLE notes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tool TEXT NOT NULL,
        session_id TEXT NOT NULL,
        ts TEXT NOT NULL,
        last_ts TEXT NOT NULL,
        kind TEXT NOT NULL,
        text TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        count INTEGER NOT NULL DEFAULT 1,
        source TEXT NOT NULL,
        event_id INTEGER,
        last_event_id INTEGER,
        dismissed_at TEXT
    );
    CREATE UNIQUE INDEX idx_notes_fp
        ON notes(tool, session_id, fingerprint) WHERE dismissed_at IS NULL;
    CREATE INDEX idx_notes_open ON notes(dismissed_at, last_ts);
    CREATE INDEX idx_notes_session ON notes(tool, session_id);

    CREATE TABLE notes_meta(key TEXT PRIMARY KEY, value INTEGER);
    INSERT INTO notes_meta(key, value)
        VALUES ('hook_from', (SELECT COALESCE(MAX(id), 0) FROM events)),
               ('rebuilt_through', 0);
    """,
    # Session lifecycle: a PR link records its head branch, close time,
    # who reported it, its creation time, the first human response and
    # whether the active gh login authored it; a session records where its
    # title came from and when (at which prompt count, on which branch) it
    # was last retitled.
    6: """
    ALTER TABLE links ADD COLUMN branch TEXT;
    ALTER TABLE links ADD COLUMN closed_at TEXT;
    ALTER TABLE links ADD COLUMN source TEXT;
    ALTER TABLE links ADD COLUMN created_at TEXT;
    ALTER TABLE links ADD COLUMN responded_at TEXT;
    ALTER TABLE links ADD COLUMN self_authored INTEGER;
    ALTER TABLE sessions ADD COLUMN title_source TEXT;
    ALTER TABLE sessions ADD COLUMN retitled_at TEXT;
    ALTER TABLE sessions ADD COLUMN retitle_prompt_count INTEGER;
    ALTER TABLE sessions ADD COLUMN retitle_branch TEXT;
    """,
    # State accuracy and backfill load: an event records what kind of prompt
    # it was (human, loop, cron, injected; NULL is legacy and reads as human);
    # a session records the background tasks and crons its last parent Stop
    # listed, and how far the incremental todo scan has read its transcript.
    # Recorded injected prompts are marked by the default prefixes here, so
    # the app (which never runs `notes --rebuild`) gets them right too. ltrim
    # with the second argument strips what Python's str.lstrip() strips
    # (ASCII whitespace), which is what the hook uses,
    # and GLOB is case-sensitive like str.startswith (LIKE is not).
    7: """
    ALTER TABLE events ADD COLUMN prompt_kind TEXT;
    ALTER TABLE sessions ADD COLUMN background_tasks INTEGER;
    ALTER TABLE sessions ADD COLUMN background_types TEXT;
    ALTER TABLE sessions ADD COLUMN session_crons INTEGER;
    ALTER TABLE sessions ADD COLUMN cron_prompts TEXT;
    ALTER TABLE sessions ADD COLUMN todos_scan_offset INTEGER;
    ALTER TABLE sessions ADD COLUMN todos_scan_state TEXT;
    UPDATE events SET prompt_kind = 'injected'
     WHERE event = 'UserPromptSubmit' AND agent_id IS NULL
       AND (ltrim(text, ' ' || char(9, 10, 11, 12, 13)) GLOB '<task-notification>*'
            OR ltrim(text, ' ' || char(9, 10, 11, 12, 13)) GLOB '<agent-message*'
            OR ltrim(text, ' ' || char(9, 10, 11, 12, 13)) GLOB '<cross-session-message*');
    """,
    # Usage visibility: a session records the model it last ran and its
    # context size in tokens, with when that size was measured (the
    # transcript line's or the hook event's time), so the app can say how
    # old the number is.
    8: """
    ALTER TABLE sessions ADD COLUMN model TEXT;
    ALTER TABLE sessions ADD COLUMN context_tokens INTEGER;
    ALTER TABLE sessions ADD COLUMN context_at TEXT;
    """,
    # tmux hosting: a session started inside tmux records its server socket
    # path and pane id (from $TMUX/$TMUX_PANE), so `focus` can find the pane
    # and its attached clients, or attach a new window to it.
    9: """
    ALTER TABLE sessions ADD COLUMN tmux_socket TEXT;
    ALTER TABLE sessions ADD COLUMN tmux_pane TEXT;
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
