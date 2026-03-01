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

    async def test_save_and_load_user_session(self, storage):
        """Saving a session and loading it back returns the same values."""
        await storage.users.ensure_user(12354)
        await storage.save_user_session(12354, "sess-abc", "/home/user/project")

        state = await storage.load_user_state(12354)
        assert state is not None
        assert state.session_id == "sess-abc"
        assert state.directory == "/home/user/project"

    async def test_load_user_state_returns_none_for_unknown_user(self, storage):
        """load_user_state returns None when user has no row."""
        state = await storage.load_user_state(99999)
        assert state is None

    async def test_save_user_directory_clears_session(self, storage):
        """save_user_directory updates directory and clears session_id."""
        await storage.users.ensure_user(12355)
        await storage.save_user_session(12355, "sess-xyz", "/old/path")
        await storage.save_user_directory(12355, "/new/path")

        state = await storage.load_user_state(12355)
        assert state is not None
        assert state.directory == "/new/path"
        assert state.session_id is None

    async def test_clear_user_session(self, storage):
        """clear_user_session removes session_id but keeps directory."""
        await storage.users.ensure_user(12356)
        await storage.save_user_session(12356, "sess-to-clear", "/some/dir")
        await storage.clear_user_session(12356)

        state = await storage.load_user_state(12356)
        assert state is not None
        assert state.session_id is None
        assert state.directory == "/some/dir"

    async def test_log_security_event(self, storage):
        """Test logging security events."""
        await storage.users.ensure_user(12352)
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
        await storage.users.ensure_user(12353)
        await storage.log_bot_event(
            user_id=12353,
            event_type="command_executed",
            event_data={"command": "/status"},
            success=True,
        )

        audit_logs = await storage.audit.get_user_audit_log(12353)
        assert len(audit_logs) == 1
        assert audit_logs[0].event_type == "command_executed"
