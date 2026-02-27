"""Tests for database management."""

import tempfile
from pathlib import Path

import pytest

from src.storage.database import DatabaseManager


@pytest.fixture
async def db_manager():
    """Create test database manager."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test.db"
        manager = DatabaseManager(f"sqlite:///{db_path}")
        await manager.initialize()
        yield manager
        await manager.close()


class TestDatabaseManager:
    """Test database manager functionality."""

    async def test_initialization(self, db_manager):
        """Test database initialization."""
        # Database should be initialized
        assert await db_manager.health_check()

    async def test_connection_pool(self, db_manager):
        """Test connection pooling."""
        # Should be able to get multiple connections
        async with db_manager.get_connection() as conn1:
            async with db_manager.get_connection() as conn2:
                # Both connections should work
                await conn1.execute("SELECT 1")
                await conn2.execute("SELECT 1")

    async def test_schema_creation(self, db_manager):
        """Test that schema is created properly."""
        async with db_manager.get_connection() as conn:
            # Check that tables exist
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            tables = [row[0] for row in await cursor.fetchall()]

            # Tables that should exist after all migrations (including migration 7 drops)
            expected_tables = [
                "users",
                "audit_log",
                "project_threads",
                "schema_version",
                "scheduled_jobs",
                "webhook_events",
                "bot_sessions",
            ]

            for table in expected_tables:
                assert table in tables, f"Expected table '{table}' not found"

            # Tables that should have been dropped by migrations 7/8
            dropped_tables = [
                "messages",
                "tool_usage",
                "cost_tracking",
                "user_tokens",
                "sessions",
            ]

            for table in dropped_tables:
                assert table not in tables, f"Table '{table}' should have been dropped"

    async def test_foreign_keys_enabled(self, db_manager):
        """Test that foreign keys are enabled."""
        async with db_manager.get_connection() as conn:
            cursor = await conn.execute("PRAGMA foreign_keys")
            result = await cursor.fetchone()
            assert result[0] == 1  # Foreign keys enabled

    async def test_indexes_created(self, db_manager):
        """Test that indexes are created."""
        async with db_manager.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND name LIKE 'idx_%'"
            )
            indexes = [row[0] for row in await cursor.fetchall()]

            # Indexes that should still exist
            expected_indexes = [
                "idx_audit_log_user_id",
                "idx_audit_log_timestamp",
                "idx_project_threads_chat_active",
                "idx_project_threads_slug",
            ]

            for index in expected_indexes:
                assert index in indexes, f"Expected index '{index}' not found"

    async def test_migration_tracking(self, db_manager):
        """Test that migrations are tracked."""
        async with db_manager.get_connection() as conn:
            cursor = await conn.execute("SELECT MAX(version) FROM schema_version")
            version = await cursor.fetchone()
            assert version[0] == 9  # Should be at migration 9

    async def test_views_dropped(self, db_manager):
        """Test that analytics views were dropped by migration 7."""
        async with db_manager.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='view'"
            )
            views = [row[0] for row in await cursor.fetchall()]

            assert "daily_stats" not in views
            assert "user_stats" not in views
