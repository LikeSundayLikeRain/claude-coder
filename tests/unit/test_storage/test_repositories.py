"""Tests for repository implementations."""

import tempfile
from pathlib import Path

import pytest

from src.storage.database import DatabaseManager
from src.storage.models import (
    ProjectThreadModel,
    UserModel,
)
from src.storage.repositories import (
    AuditLogRepository,
    ProjectThreadRepository,
    UserRepository,
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
async def user_repo(db_manager):
    """Create user repository."""
    return UserRepository(db_manager)


@pytest.fixture
async def audit_repo(db_manager):
    """Create audit log repository."""
    return AuditLogRepository(db_manager)


@pytest.fixture
async def project_thread_repo(db_manager):
    """Create project thread repository."""
    return ProjectThreadRepository(db_manager)


class TestUserRepository:
    """Test user repository."""

    async def test_create_and_get_user(self, user_repo):
        """Test creating and retrieving user via ensure_user + get_user."""
        await user_repo.ensure_user(12345, "testuser")

        retrieved_user = await user_repo.get_user(12345)
        assert retrieved_user is not None
        assert retrieved_user.user_id == 12345
        assert retrieved_user.telegram_username == "testuser"

    async def test_update_user(self, user_repo):
        """Test updating session and directory for a user."""
        await user_repo.ensure_user(12346, "testuser2")

        await user_repo.update_session(12346, "sess-xyz", "/home/user/project")

        updated_user = await user_repo.get_user(12346)
        assert updated_user.session_id == "sess-xyz"
        assert updated_user.directory == "/home/user/project"

    async def test_get_allowed_users(self, user_repo):
        """Test that ensure_user creates rows that can be retrieved."""
        await user_repo.ensure_user(12347, "allowed")
        await user_repo.ensure_user(12348, "disallowed")

        user1 = await user_repo.get_user(12347)
        user2 = await user_repo.get_user(12348)

        assert user1 is not None
        assert user1.user_id == 12347
        assert user2 is not None
        assert user2.user_id == 12348


class TestProjectThreadRepository:
    """Test project thread repository."""

    async def test_upsert_and_lookup(self, project_thread_repo):
        """Upsert creates mapping and lookup resolves it."""
        mapping = await project_thread_repo.upsert_mapping(
            project_slug="app1",
            chat_id=-1001234567890,
            message_thread_id=321,
            topic_name="App One",
        )

        assert isinstance(mapping, ProjectThreadModel)
        assert mapping.project_slug == "app1"
        assert mapping.message_thread_id == 321

        lookup = await project_thread_repo.get_by_chat_thread(-1001234567890, 321)
        assert lookup is not None
        assert lookup.project_slug == "app1"

    async def test_deactivate_missing_projects(self, project_thread_repo):
        """Mappings not in active set are deactivated."""
        await project_thread_repo.upsert_mapping(
            project_slug="app1",
            chat_id=-1001234567890,
            message_thread_id=111,
            topic_name="App 1",
        )
        await project_thread_repo.upsert_mapping(
            project_slug="app2",
            chat_id=-1001234567890,
            message_thread_id=222,
            topic_name="App 2",
        )

        changed = await project_thread_repo.deactivate_missing_projects(
            chat_id=-1001234567890,
            active_project_slugs=["app1"],
        )

        assert changed == 1
        inactive_mapping = await project_thread_repo.get_by_chat_project(
            -1001234567890, "app2"
        )
        assert inactive_mapping is not None
        assert inactive_mapping.is_active is False

    async def test_list_stale_active_mappings(self, project_thread_repo):
        """Returns only active mappings not in enabled project set."""
        await project_thread_repo.upsert_mapping(
            project_slug="app1",
            chat_id=-1001234567890,
            message_thread_id=111,
            topic_name="App 1",
            is_active=True,
        )
        await project_thread_repo.upsert_mapping(
            project_slug="app2",
            chat_id=-1001234567890,
            message_thread_id=222,
            topic_name="App 2",
            is_active=True,
        )
        await project_thread_repo.upsert_mapping(
            project_slug="app3",
            chat_id=-1001234567890,
            message_thread_id=333,
            topic_name="App 3",
            is_active=False,
        )

        stale = await project_thread_repo.list_stale_active_mappings(
            chat_id=-1001234567890,
            active_project_slugs=["app1"],
        )

        assert len(stale) == 1
        assert stale[0].project_slug == "app2"

    async def test_set_active_updates_flag(self, project_thread_repo):
        """set_active toggles mapping active flag."""
        await project_thread_repo.upsert_mapping(
            project_slug="app1",
            chat_id=-1001234567890,
            message_thread_id=111,
            topic_name="App 1",
            is_active=True,
        )

        changed = await project_thread_repo.set_active(
            chat_id=-1001234567890,
            project_slug="app1",
            is_active=False,
        )

        assert changed == 1
        mapping = await project_thread_repo.get_by_chat_project(-1001234567890, "app1")
        assert mapping is not None
        assert mapping.is_active is False
