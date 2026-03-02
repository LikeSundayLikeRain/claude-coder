"""Tests for repository implementations."""

import tempfile
from pathlib import Path

import pytest

from src.storage.database import DatabaseManager
from src.storage.models import ChatSessionModel
from src.storage.repositories import (
    AuditLogRepository,
    ChatSessionRepository,
)


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
async def chat_session_repo(db_manager):
    """Create chat session repository."""
    return ChatSessionRepository(db_manager)


@pytest.fixture
async def audit_repo(db_manager):
    """Create audit log repository."""
    return AuditLogRepository(db_manager)


class TestChatSessionRepository:
    """Test chat session repository."""

    async def test_upsert_and_get(self, chat_session_repo):
        """upsert creates a row and get retrieves it."""
        await chat_session_repo.upsert(
            chat_id=100,
            message_thread_id=0,
            user_id=42,
            directory="/home/user/proj",
            session_id="sess-abc",
        )

        result = await chat_session_repo.get(100, 0)
        assert result is not None
        assert isinstance(result, ChatSessionModel)
        assert result.chat_id == 100
        assert result.message_thread_id == 0
        assert result.user_id == 42
        assert result.directory == "/home/user/proj"
        assert result.session_id == "sess-abc"

    async def test_upsert_overwrites_session_id(self, chat_session_repo):
        """Upserting with new session_id updates the existing row."""
        await chat_session_repo.upsert(100, 0, 42, "/proj", "sess-old")
        await chat_session_repo.upsert(100, 0, 42, "/proj", "sess-new")

        result = await chat_session_repo.get(100, 0)
        assert result is not None
        assert result.session_id == "sess-new"

    async def test_get_returns_none_for_missing(self, chat_session_repo):
        """get returns None when no row exists."""
        result = await chat_session_repo.get(99999, 0)
        assert result is None

    async def test_deactivate(self, chat_session_repo):
        """deactivate sets is_active=0; subsequent get returns None."""
        await chat_session_repo.upsert(200, 10, 5, "/proj/alpha", topic_name="Alpha")

        changed = await chat_session_repo.deactivate(200, 10)
        assert changed == 1

        # get only returns active rows
        result = await chat_session_repo.get(200, 10)
        assert result is None

    async def test_delete(self, chat_session_repo):
        """delete hard-removes the row."""
        await chat_session_repo.upsert(300, 0, 7, "/proj/beta")
        assert await chat_session_repo.get(300, 0) is not None

        await chat_session_repo.delete(300, 0)
        assert await chat_session_repo.get(300, 0) is None

    async def test_list_active_by_chat(self, chat_session_repo):
        """list_active_by_chat returns only active rows for a chat."""
        await chat_session_repo.upsert(-1001, 10, 1, "/proj/a", topic_name="A")
        await chat_session_repo.upsert(-1001, 20, 2, "/proj/b", topic_name="B")
        await chat_session_repo.deactivate(-1001, 20)

        active = await chat_session_repo.list_active_by_chat(-1001)
        assert len(active) == 1
        assert active[0].directory == "/proj/a"

    async def test_get_by_user_directory(self, chat_session_repo):
        """get_by_user_directory finds DM session (message_thread_id=0)."""
        await chat_session_repo.upsert(
            chat_id=42, message_thread_id=0, user_id=42, directory="/home/user/proj"
        )

        result = await chat_session_repo.get_by_user_directory(42, "/home/user/proj")
        assert result is not None
        assert result.directory == "/home/user/proj"

    async def test_count_active_by_chat_directory(self, chat_session_repo):
        """count_active_by_chat_directory counts active sessions per directory."""
        await chat_session_repo.upsert(-1001, 10, 1, "/proj/a", topic_name="A1")
        await chat_session_repo.upsert(-1001, 20, 2, "/proj/a", topic_name="A2")
        await chat_session_repo.upsert(-1001, 30, 3, "/proj/b", topic_name="B")

        count_a = await chat_session_repo.count_active_by_chat_directory(-1001, "/proj/a")
        assert count_a == 2

        count_b = await chat_session_repo.count_active_by_chat_directory(-1001, "/proj/b")
        assert count_b == 1

    async def test_list_by_user(self, chat_session_repo):
        """list_by_user returns all active sessions for a user."""
        await chat_session_repo.upsert(100, 0, 99, "/proj/x")
        await chat_session_repo.upsert(200, 0, 99, "/proj/y")
        await chat_session_repo.upsert(300, 0, 88, "/proj/z")  # different user

        sessions = await chat_session_repo.list_by_user(99)
        assert len(sessions) == 2
        dirs = {s.directory for s in sessions}
        assert dirs == {"/proj/x", "/proj/y"}
