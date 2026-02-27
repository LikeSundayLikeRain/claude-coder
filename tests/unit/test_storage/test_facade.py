"""Tests for storage facade."""

import tempfile
from datetime import datetime  # noqa: F401
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
        # Should be able to perform health check
        assert await storage.health_check()

    async def test_get_or_create_user(self, storage):
        """Test getting or creating user."""
        # Create new user
        user = await storage.get_or_create_user(12345, "testuser")
        assert user.user_id == 12345
        assert user.telegram_username == "testuser"
        assert not user.is_allowed  # Default to not allowed

        # Get existing user
        user2 = await storage.get_or_create_user(12345, "testuser")
        assert user2.user_id == 12345
        assert user2.telegram_username == "testuser"

    async def test_is_user_allowed(self, storage):
        """Test checking user permissions."""
        # Create allowed user
        await storage.get_or_create_user(12348, "alloweduser")
        await storage.users.set_user_allowed(12348, True)

        # Check permission
        assert await storage.is_user_allowed(12348)

        # Create disallowed user
        await storage.get_or_create_user(12349, "disalloweduser")
        assert not await storage.is_user_allowed(12349)

    async def test_log_security_event(self, storage):
        """Test logging security events."""
        # Setup user
        await storage.get_or_create_user(12352, "securityuser")

        # Log security event
        await storage.log_security_event(
            user_id=12352,
            event_type="authentication_failure",
            event_data={"reason": "invalid_token"},
            success=False,
            ip_address="192.168.1.1",
        )

        # Verify event was logged
        audit_logs = await storage.audit.get_user_audit_log(12352)
        assert len(audit_logs) == 1
        assert audit_logs[0].event_type == "authentication_failure"
        assert not audit_logs[0].success
        assert audit_logs[0].event_data["reason"] == "invalid_token"

    async def test_log_bot_event(self, storage):
        """Test logging bot events."""
        await storage.get_or_create_user(12353, "botuser")

        await storage.log_bot_event(
            user_id=12353,
            event_type="command_executed",
            event_data={"command": "/status"},
            success=True,
        )

        audit_logs = await storage.audit.get_user_audit_log(12353)
        assert len(audit_logs) == 1
        assert audit_logs[0].event_type == "command_executed"

    async def test_save_and_load_user_directory(self, storage):
        """Test persisting and loading user directory."""
        await storage.get_or_create_user(12354, "diruser")

        # Save directory
        await storage.save_user_directory(12354, "/home/user/project")

        # Load directory
        directory = await storage.load_user_directory(12354)
        assert directory == "/home/user/project"

    async def test_load_user_directory_returns_none_when_unset(self, storage):
        """Test loading directory when not set."""
        await storage.get_or_create_user(12355, "nodiruser")

        directory = await storage.load_user_directory(12355)
        assert directory is None
