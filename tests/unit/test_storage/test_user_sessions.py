"""Tests for DM session persistence via ChatSessionRepository.

Previously tested UserSessionRepository (now removed).
Equivalent coverage is in tests/unit/test_chat_session_repository.py.
This file retains focused tests for the DM-session pattern
(chat_id = user_id, message_thread_id = 0).
"""

import tempfile
from pathlib import Path

import pytest

from src.storage.database import DatabaseManager
from src.storage.repositories import ChatSessionRepository


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
async def session_repo(db_manager):
    """Create chat session repository."""
    return ChatSessionRepository(db_manager)


class TestDMSessionRepository:
    """Tests for DM session round-trips via ChatSessionRepository."""

    async def test_save_and_get_session_round_trip(self, session_repo):
        """upsert + get returns the persisted session_id."""
        await session_repo.upsert(
            chat_id=1001,
            message_thread_id=0,
            user_id=1001,
            directory="/home/user/project",
            session_id="sess-abc",
        )

        result = await session_repo.get(1001, 0)

        assert result is not None
        assert result.user_id == 1001
        assert result.directory == "/home/user/project"
        assert result.session_id == "sess-abc"

    async def test_save_session_overwrites_existing(self, session_repo):
        """Saving a new session_id for same user overwrites the old one."""
        await session_repo.upsert(1002, 0, 1002, "/home/user/project", "sess-old")
        await session_repo.upsert(1002, 0, 1002, "/home/user/project", "sess-new")

        result = await session_repo.get(1002, 0)

        assert result is not None
        assert result.session_id == "sess-new"

    async def test_delete_session_removes_row(self, session_repo):
        """delete removes the row; subsequent get returns None."""
        await session_repo.upsert(1003, 0, 1003, "/home/user/project", "sess-xyz")
        await session_repo.delete(1003, 0)

        result = await session_repo.get(1003, 0)

        assert result is None

    async def test_delete_nonexistent_is_no_op(self, session_repo):
        """delete on a non-existent row does not raise."""
        await session_repo.delete(1004, 0)

    async def test_get_returns_none_for_nonexistent(self, session_repo):
        """get returns None when no row exists."""
        result = await session_repo.get(1005, 0)
        assert result is None

    async def test_list_by_user_returns_all_for_user(self, session_repo):
        """list_by_user returns all directories for a user."""
        await session_repo.upsert(1006, 0, 1006, "/project/alpha", "sess-alpha")
        # Simulate multi-chat scenario for same user
        await session_repo.upsert(2006, 0, 1006, "/project/beta", "sess-beta")

        sessions = await session_repo.list_by_user(1006)

        assert len(sessions) == 2
        directories = {s.directory for s in sessions}
        assert directories == {"/project/alpha", "/project/beta"}

    async def test_list_by_user_empty_for_new_user(self, session_repo):
        """list_by_user returns empty list when user has no sessions."""
        sessions = await session_repo.list_by_user(1007)
        assert sessions == []

    async def test_multiple_users_same_directory_do_not_collide(self, session_repo):
        """Different users can have sessions for the same directory without collision."""
        await session_repo.upsert(2001, 0, 2001, "/shared/project", "sess-user1")
        await session_repo.upsert(2002, 0, 2002, "/shared/project", "sess-user2")

        result1 = await session_repo.get_by_user_directory(2001, "/shared/project")
        result2 = await session_repo.get_by_user_directory(2002, "/shared/project")

        assert result1 is not None
        assert result1.session_id == "sess-user1"
        assert result2 is not None
        assert result2.session_id == "sess-user2"

    async def test_same_user_multiple_directories_isolated(self, session_repo):
        """A user's sessions for different directories are independent."""
        await session_repo.upsert(3001, 0, 3001, "/project/a", "sess-a")
        # Use a different chat_id to isolate rows (DM sessions use chat_id=user_id)
        await session_repo.upsert(3001, 1, 3001, "/project/b", "sess-b")

        await session_repo.delete(3001, 0)

        result_a = await session_repo.get(3001, 0)
        result_b = await session_repo.get(3001, 1)

        assert result_a is None
        assert result_b is not None
        assert result_b.session_id == "sess-b"
