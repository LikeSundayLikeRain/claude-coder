"""Tests for migration 12 — chat_sessions consolidation.

Strategy:
  1. Open a raw SQLite connection (no DatabaseManager) and run migrations 1-11
     so the DB is at the pre-12 state.
  2. Seed test data into users / user_sessions / project_threads / audit_log.
  3. Run the migration-12 SQL directly via executescript().
  4. Assert post-migration invariants.
"""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from src.storage.database import DatabaseManager


# ---------------------------------------------------------------------------
# Helper: extract the migration-12 SQL from DatabaseManager
# ---------------------------------------------------------------------------

def _get_migration_sql(version: int) -> str:
    """Return the raw SQL string for a given migration version."""
    dm = DatabaseManager("sqlite:///dummy.db")
    for v, sql in dm._get_migrations():
        if v == version:
            return sql
    raise ValueError(f"Migration {version} not found")


# ---------------------------------------------------------------------------
# Fixture: DB at migration-11 state with seed data
# ---------------------------------------------------------------------------

@pytest.fixture
def db_at_11():
    """
    Create a temporary SQLite DB, run migrations 1-11 only, seed data,
    and yield the (path, connection) so tests can then apply migration 12.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test_m12.db"

        dm = DatabaseManager(f"sqlite:///{db_path}")
        migrations = dm._get_migrations()

        con = sqlite3.connect(str(db_path), detect_types=sqlite3.PARSE_DECLTYPES)
        con.row_factory = sqlite3.Row

        # schema_version table
        con.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)"
        )
        con.commit()

        # Run migrations 1-11
        for v, sql in migrations:
            if v > 11:
                break
            con.executescript(sql)
            con.execute(
                "INSERT INTO schema_version (version) VALUES (?)", (v,)
            )
            con.commit()

        # ----------------------------------------------------------------
        # Seed data
        # ----------------------------------------------------------------

        # users (migration-11 schema: user_id, telegram_username)
        con.executemany(
            "INSERT INTO users (user_id, telegram_username) VALUES (?, ?)",
            [
                (100, "alice"),
                (200, "bob"),
                (300, "carol"),  # carol has no project_thread
            ],
        )

        # user_sessions
        con.executemany(
            "INSERT INTO user_sessions (user_id, directory, session_id) VALUES (?, ?, ?)",
            [
                (100, "/projects/alpha", "sess-alpha-100"),
                (200, "/projects/beta",  "sess-beta-200"),
                (300, "/projects/gamma", "sess-gamma-300"),  # DM only, no thread
            ],
        )

        # project_threads  (chat_id, message_thread_id, directory, topic_name, is_active)
        con.executemany(
            """INSERT INTO project_threads
               (chat_id, message_thread_id, directory, topic_name, is_active)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (-1001, 10, "/projects/alpha", "Alpha",  True),
                (-1001, 20, "/projects/beta",  "Beta",   True),
            ],
        )

        # audit_log
        con.execute(
            """INSERT INTO audit_log (user_id, event_type, event_data, success)
               VALUES (100, 'login', '{}', 1)"""
        )

        con.commit()
        yield con
        con.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMigration12:
    """Verify migration 12 post-conditions."""

    def _apply_migration_12(self, con: sqlite3.Connection) -> None:
        sql = _get_migration_sql(12)
        con.executescript(sql)
        con.commit()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def test_chat_sessions_table_exists(self, db_at_11: sqlite3.Connection) -> None:
        """chat_sessions table must exist after migration."""
        self._apply_migration_12(db_at_11)
        cur = db_at_11.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='chat_sessions'"
        )
        assert cur.fetchone() is not None, "chat_sessions table not created"

    def test_chat_sessions_columns(self, db_at_11: sqlite3.Connection) -> None:
        """chat_sessions must have all required columns."""
        self._apply_migration_12(db_at_11)
        cur = db_at_11.execute("PRAGMA table_info(chat_sessions)")
        cols = {row["name"] for row in cur.fetchall()}
        expected = {
            "chat_id", "message_thread_id", "user_id", "directory",
            "session_id", "topic_name", "is_active", "created_at",
        }
        assert expected <= cols, f"Missing columns: {expected - cols}"

    def test_chat_sessions_primary_key(self, db_at_11: sqlite3.Connection) -> None:
        """PK must be (chat_id, message_thread_id)."""
        self._apply_migration_12(db_at_11)
        cur = db_at_11.execute("PRAGMA table_info(chat_sessions)")
        pk_cols = {row["name"] for row in cur.fetchall() if row["pk"] > 0}
        assert pk_cols == {"chat_id", "message_thread_id"}

    # ------------------------------------------------------------------
    # project_threads migration
    # ------------------------------------------------------------------

    def test_project_threads_migrated(self, db_at_11: sqlite3.Connection) -> None:
        """Rows from project_threads must appear in chat_sessions."""
        self._apply_migration_12(db_at_11)
        cur = db_at_11.execute(
            "SELECT * FROM chat_sessions WHERE chat_id = -1001 ORDER BY message_thread_id"
        )
        rows = cur.fetchall()
        assert len(rows) == 2, f"Expected 2 project-thread rows, got {len(rows)}"

    def test_project_thread_alpha_has_session_id(self, db_at_11: sqlite3.Connection) -> None:
        """Alpha thread must carry session_id from user_sessions join."""
        self._apply_migration_12(db_at_11)
        cur = db_at_11.execute(
            "SELECT session_id, topic_name, user_id FROM chat_sessions "
            "WHERE chat_id = -1001 AND message_thread_id = 10"
        )
        row = cur.fetchone()
        assert row is not None
        assert row["session_id"] == "sess-alpha-100"
        assert row["topic_name"] == "Alpha"
        assert row["user_id"] == 100

    def test_project_thread_beta_has_session_id(self, db_at_11: sqlite3.Connection) -> None:
        """Beta thread must carry session_id from user_sessions join."""
        self._apply_migration_12(db_at_11)
        cur = db_at_11.execute(
            "SELECT session_id, topic_name, user_id FROM chat_sessions "
            "WHERE chat_id = -1001 AND message_thread_id = 20"
        )
        row = cur.fetchone()
        assert row is not None
        assert row["session_id"] == "sess-beta-200"
        assert row["topic_name"] == "Beta"
        assert row["user_id"] == 200

    # ------------------------------------------------------------------
    # DM session migration
    # ------------------------------------------------------------------

    def test_dm_session_migrated(self, db_at_11: sqlite3.Connection) -> None:
        """user_sessions NOT in project_threads must land as DM rows (thread=0)."""
        self._apply_migration_12(db_at_11)
        cur = db_at_11.execute(
            "SELECT * FROM chat_sessions WHERE message_thread_id = 0"
        )
        rows = cur.fetchall()
        assert len(rows) == 1, f"Expected 1 DM row, got {len(rows)}"
        row = rows[0]
        assert row["chat_id"] == 300        # chat_id = user_id
        assert row["user_id"] == 300
        assert row["directory"] == "/projects/gamma"
        assert row["session_id"] == "sess-gamma-300"
        assert row["topic_name"] is None

    def test_project_thread_directories_not_duplicated_as_dm(
        self, db_at_11: sqlite3.Connection
    ) -> None:
        """Directories already in project_threads must NOT be re-inserted as DM rows."""
        self._apply_migration_12(db_at_11)
        cur = db_at_11.execute(
            "SELECT COUNT(*) FROM chat_sessions WHERE directory IN (?, ?)",
            ("/projects/alpha", "/projects/beta"),
        )
        count = cur.fetchone()[0]
        # Only the 2 project-thread rows; no extra DM rows for alpha/beta
        assert count == 2

    # ------------------------------------------------------------------
    # Old tables dropped
    # ------------------------------------------------------------------

    def test_users_table_dropped(self, db_at_11: sqlite3.Connection) -> None:
        self._apply_migration_12(db_at_11)
        cur = db_at_11.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
        )
        assert cur.fetchone() is None, "users table should have been dropped"

    def test_user_sessions_table_dropped(self, db_at_11: sqlite3.Connection) -> None:
        self._apply_migration_12(db_at_11)
        cur = db_at_11.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='user_sessions'"
        )
        assert cur.fetchone() is None, "user_sessions table should have been dropped"

    def test_project_threads_table_dropped(self, db_at_11: sqlite3.Connection) -> None:
        self._apply_migration_12(db_at_11)
        cur = db_at_11.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='project_threads'"
        )
        assert cur.fetchone() is None, "project_threads table should have been dropped"

    # ------------------------------------------------------------------
    # audit_log — no FK to users
    # ------------------------------------------------------------------

    def test_audit_log_no_fk_to_users(self, db_at_11: sqlite3.Connection) -> None:
        """audit_log must not have a REFERENCES users FK after migration."""
        self._apply_migration_12(db_at_11)
        cur = db_at_11.execute("PRAGMA foreign_key_list(audit_log)")
        fks = cur.fetchall()
        tables_referenced = {row["table"] for row in fks}
        assert "users" not in tables_referenced, (
            "audit_log still references users table"
        )

    def test_audit_log_data_preserved(self, db_at_11: sqlite3.Connection) -> None:
        """Existing audit_log rows must survive the table rebuild."""
        self._apply_migration_12(db_at_11)
        cur = db_at_11.execute(
            "SELECT COUNT(*) FROM audit_log WHERE user_id = 100 AND event_type = 'login'"
        )
        assert cur.fetchone()[0] == 1

    # ------------------------------------------------------------------
    # Indexes
    # ------------------------------------------------------------------

    def test_chat_sessions_user_id_index(self, db_at_11: sqlite3.Connection) -> None:
        self._apply_migration_12(db_at_11)
        cur = db_at_11.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_chat_sessions_user_id'"
        )
        assert cur.fetchone() is not None

    def test_chat_sessions_directory_index(self, db_at_11: sqlite3.Connection) -> None:
        self._apply_migration_12(db_at_11)
        cur = db_at_11.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_chat_sessions_directory'"
        )
        assert cur.fetchone() is not None

    # ------------------------------------------------------------------
    # End-to-end: DatabaseManager.initialize() runs all migrations incl. 12
    # ------------------------------------------------------------------

    def test_database_manager_initialize_reaches_v12(self) -> None:
        """DatabaseManager.initialize() must bring schema to version 12."""
        import asyncio
        import tempfile as _tmp

        async def _run() -> int:
            with _tmp.TemporaryDirectory() as d:
                dm = DatabaseManager(f"sqlite:///{Path(d) / 'full.db'}")
                await dm.initialize()
                async with dm.get_connection() as conn:
                    cur = await conn.execute(
                        "SELECT MAX(version) FROM schema_version"
                    )
                    row = await cur.fetchone()
                    version = row[0]
                await dm.close()
                return version

        assert asyncio.run(_run()) == 12
