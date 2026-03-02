"""Tests for storage facade."""

import tempfile
from pathlib import Path

import pytest

from src.storage.facade import Storage


@pytest.fixture
async def storage():
    """Create test storage."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test.db"
        storage = Storage(f"sqlite:///{db_path}")
        await storage.initialize()
        yield storage
        await storage.close()


class TestStorageFacade:
    """Test storage facade functionality."""

    async def test_initialization(self, storage):
        """Test storage initialization."""
        assert await storage.health_check()

    async def test_save_and_load_session(self, storage):
        """Saving a session and loading it back returns the same values."""
        await storage.save_session(
            chat_id=12354,
            message_thread_id=0,
            user_id=12354,
            directory="/home/user/project",
            session_id="sess-abc",
        )

        state = await storage.load_session(12354, 0)
        assert state is not None
        assert state.session_id == "sess-abc"
        assert state.directory == "/home/user/project"

    async def test_load_session_returns_none_for_unknown(self, storage):
        """load_session returns None when no row exists."""
        state = await storage.load_session(99999, 0)
        assert state is None

    async def test_clear_session(self, storage):
        """clear_session removes the session row."""
        await storage.save_session(
            chat_id=12356,
            message_thread_id=0,
            user_id=12356,
            directory="/some/dir",
            session_id="sess-to-clear",
        )
        await storage.clear_session(12356, 0)

        state = await storage.load_session(12356, 0)
        assert state is None

    async def test_clear_session_does_not_affect_other_threads(self, storage):
        """Clearing one thread's session leaves others intact."""
        await storage.save_session(
            chat_id=12357,
            message_thread_id=0,
            user_id=12357,
            directory="/dir/a",
            session_id="sess-a",
        )
        await storage.save_session(
            chat_id=12357,
            message_thread_id=1,
            user_id=12357,
            directory="/dir/b",
            session_id="sess-b",
        )
        await storage.clear_session(12357, 0)

        state_a = await storage.load_session(12357, 0)
        state_b = await storage.load_session(12357, 1)
        assert state_a is None
        assert state_b is not None
        assert state_b.session_id == "sess-b"

    async def test_log_security_event(self, storage):
        """Test logging security events."""
        await storage.log_security_event(
            user_id=12352,
            event_type="authentication_failure",
            event_data={"reason": "invalid_token"},
            success=False,
            ip_address="192.168.1.1",
        )

        audit_logs = await storage.audit.get_user_audit_log(12352)
        assert len(audit_logs) == 1
        assert audit_logs[0].event_type == "authentication_failure"
        assert not audit_logs[0].success
        assert audit_logs[0].event_data["reason"] == "invalid_token"

    async def test_log_bot_event(self, storage):
        """Test logging bot events."""
        await storage.log_bot_event(
            user_id=12353,
            event_type="command_executed",
            event_data={"command": "/status"},
            success=True,
        )

        audit_logs = await storage.audit.get_user_audit_log(12353)
        assert len(audit_logs) == 1
        assert audit_logs[0].event_type == "command_executed"
