"""Tests for BotSessionRepository."""

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.storage.database import DatabaseManager
from src.storage.models import BotSessionModel
from src.storage.repositories import BotSessionRepository


@pytest.fixture
async def db_manager():
    """Create test database manager."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test.db"
        manager = DatabaseManager(f"sqlite:///{db_path}")
        await manager.initialize()
        yield manager
        await manager.close()


@pytest.fixture
async def repo(db_manager):
    """Create BotSessionRepository."""
    return BotSessionRepository(db_manager)


class TestBotSessionRepository:
    """Tests for BotSessionRepository."""

    async def test_upsert_and_get_by_user(self, repo):
        """upsert() inserts a row; get_by_user() retrieves it."""
        await repo.upsert(
            user_id=1001,
            session_id="sess-abc",
            directory="/home/user/project",
            model="claude-sonnet-4-6",
            betas=["interleaved-thinking-2025-05-14"],
        )

        result = await repo.get_by_user(1001)

        assert result is not None
        assert isinstance(result, BotSessionModel)
        assert result.user_id == 1001
        assert result.session_id == "sess-abc"
        assert result.directory == "/home/user/project"
        assert result.model == "claude-sonnet-4-6"
        assert result.betas == ["interleaved-thinking-2025-05-14"]
        assert isinstance(result.last_active, datetime)

    async def test_upsert_overwrites_on_conflict(self, repo):
        """Second upsert for same user_id replaces previous values."""
        await repo.upsert(
            user_id=1002,
            session_id="sess-old",
            directory="/home/user/old",
            model="claude-haiku-4-5",
            betas=None,
        )

        await repo.upsert(
            user_id=1002,
            session_id="sess-new",
            directory="/home/user/new",
            model="claude-opus-4-6",
            betas=["beta-flag"],
        )

        result = await repo.get_by_user(1002)

        assert result is not None
        assert result.session_id == "sess-new"
        assert result.directory == "/home/user/new"
        assert result.model == "claude-opus-4-6"
        assert result.betas == ["beta-flag"]

    async def test_get_by_user_returns_none_when_not_found(self, repo):
        """get_by_user() returns None for unknown user_id."""
        result = await repo.get_by_user(9999)
        assert result is None

    async def test_delete_removes_entry(self, repo):
        """delete() removes the row for that user_id."""
        await repo.upsert(
            user_id=1003,
            session_id="sess-to-delete",
            directory="/tmp/project",
        )

        # Verify it exists
        assert await repo.get_by_user(1003) is not None

        await repo.delete(1003)

        # Now it should be gone
        assert await repo.get_by_user(1003) is None

    async def test_cleanup_expired_deletes_old_entries_and_returns_count(self, repo):
        """cleanup_expired() removes entries older than threshold and returns count."""
        # Insert a current entry
        await repo.upsert(
            user_id=2001,
            session_id="sess-fresh",
            directory="/tmp/fresh",
        )

        # Insert an old entry by manually backdating last_active
        old_time = datetime.now(UTC) - timedelta(hours=25)
        async with repo.db.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO bot_sessions (user_id, session_id, directory, last_active)
                VALUES (?, ?, ?, ?)
                """,
                (2002, "sess-stale", "/tmp/stale", old_time),
            )
            await conn.commit()

        count = await repo.cleanup_expired(max_age_hours=24)

        assert count == 1
        assert await repo.get_by_user(2001) is not None
        assert await repo.get_by_user(2002) is None
