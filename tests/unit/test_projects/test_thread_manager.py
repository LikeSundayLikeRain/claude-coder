from unittest.mock import AsyncMock, MagicMock

import pytest

from src.projects.thread_manager import ProjectThreadManager
from src.storage.models import ChatSessionModel


@pytest.fixture
def mock_repo():
    repo = AsyncMock()
    return repo


@pytest.fixture
def manager(mock_repo):
    return ProjectThreadManager(repository=mock_repo)


class TestGenerateTopicName:
    def test_basename_no_collision(self):
        name = ProjectThreadManager.generate_topic_name("/home/user/my-project", [])
        assert name == "my-project"

    def test_basename_collision_uses_suffix(self):
        name = ProjectThreadManager.generate_topic_name("/home/user/work/app", ["app"])
        assert name == "app (2)"

    def test_no_collision_with_different_names(self):
        name = ProjectThreadManager.generate_topic_name(
            "/home/user/frontend", ["backend", "api"]
        )
        assert name == "frontend"


class TestResolveDirectory:
    @pytest.mark.asyncio
    async def test_found(self, manager, mock_repo):
        mock_repo.get.return_value = ChatSessionModel(
            chat_id=1,
            message_thread_id=2,
            user_id=0,
            directory="/home/user/proj",
            topic_name="proj",
        )
        result = await manager.resolve_directory(1, 2)
        assert result == "/home/user/proj"

    @pytest.mark.asyncio
    async def test_not_found(self, manager, mock_repo):
        mock_repo.get.return_value = None
        result = await manager.resolve_directory(1, 99)
        assert result is None


class TestCreateTopic:
    @pytest.mark.asyncio
    async def test_creates_topic_and_stores_binding(self, manager, mock_repo):
        mock_bot = AsyncMock()
        mock_bot.create_forum_topic.return_value = MagicMock(message_thread_id=42)
        mock_bot.send_message = AsyncMock()
        mock_repo.get.return_value = ChatSessionModel(
            chat_id=1,
            message_thread_id=42,
            user_id=10,
            directory="/home/user/proj",
            topic_name="proj",
        )
        result = await manager.create_topic(mock_bot, 1, 10, "/home/user/proj", "proj")
        assert result.message_thread_id == 42
        mock_bot.create_forum_topic.assert_called_once()
        mock_repo.upsert.assert_called_once()

    @pytest.mark.asyncio
    async def test_raises_if_repo_returns_none(self, manager, mock_repo):
        mock_bot = AsyncMock()
        mock_bot.create_forum_topic.return_value = MagicMock(message_thread_id=10)
        mock_bot.send_message = AsyncMock()
        mock_repo.get.return_value = None
        with pytest.raises(RuntimeError, match="Failed to create topic mapping"):
            await manager.create_topic(mock_bot, 1, 10, "/home/user/proj", "proj")


class TestRemoveTopic:
    @pytest.mark.asyncio
    async def test_closes_and_deactivates(self, manager, mock_repo):
        mock_bot = AsyncMock()
        await manager.remove_topic(mock_bot, 1, 42)
        mock_bot.close_forum_topic.assert_called_once_with(
            chat_id=1, message_thread_id=42
        )
        mock_repo.deactivate.assert_called_once_with(1, 42)
