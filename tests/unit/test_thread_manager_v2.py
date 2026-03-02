"""Tests for ProjectThreadManager with ChatSessionRepository."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.projects.thread_manager import ProjectThreadManager
from src.storage.models import ChatSessionModel
from src.storage.repositories import ChatSessionRepository


def make_session(
    chat_id: int = 100,
    message_thread_id: int = 42,
    user_id: int = 1,
    directory: str = "/projects/myapp",
    topic_name: str = "myapp",
) -> ChatSessionModel:
    return ChatSessionModel(
        chat_id=chat_id,
        message_thread_id=message_thread_id,
        user_id=user_id,
        directory=directory,
        topic_name=topic_name,
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# generate_topic_name
# ---------------------------------------------------------------------------


def test_generate_topic_name_no_collision() -> None:
    name = ProjectThreadManager.generate_topic_name("/projects/myapp", [])
    assert name == "myapp"


def test_generate_topic_name_collision_gives_suffix_2() -> None:
    name = ProjectThreadManager.generate_topic_name("/projects/myapp", ["myapp"])
    assert name == "myapp (2)"


def test_generate_topic_name_double_collision_gives_suffix_3() -> None:
    name = ProjectThreadManager.generate_topic_name(
        "/projects/myapp", ["myapp", "myapp (2)"]
    )
    assert name == "myapp (3)"


def test_generate_topic_name_override_name_used_as_is() -> None:
    name = ProjectThreadManager.generate_topic_name(
        "/projects/myapp", ["myapp"], override_name="Custom Name"
    )
    assert name == "Custom Name"


def test_generate_topic_name_override_ignores_existing_names() -> None:
    # override_name is returned even when it matches an existing name
    name = ProjectThreadManager.generate_topic_name(
        "/projects/myapp", ["Custom Name"], override_name="Custom Name"
    )
    assert name == "Custom Name"


# ---------------------------------------------------------------------------
# create_topic
# ---------------------------------------------------------------------------


async def test_create_topic_calls_create_forum_topic_and_upsert() -> None:
    session = make_session()

    # Mock topic object returned by Telegram
    mock_topic = MagicMock()
    mock_topic.message_thread_id = 42

    bot = MagicMock()
    bot.create_forum_topic = AsyncMock(return_value=mock_topic)
    bot.send_message = AsyncMock()

    repo = AsyncMock(spec=ChatSessionRepository)
    repo.upsert = AsyncMock()
    repo.get = AsyncMock(return_value=session)

    manager = ProjectThreadManager(repository=repo)
    result = await manager.create_topic(
        bot=bot,
        chat_id=100,
        user_id=1,
        directory="/projects/myapp",
        topic_name="myapp",
    )

    bot.create_forum_topic.assert_awaited_once_with(chat_id=100, name="myapp")
    repo.upsert.assert_awaited_once_with(
        chat_id=100,
        message_thread_id=42,
        user_id=1,
        directory="/projects/myapp",
        topic_name="myapp",
        session_id=None,
    )
    repo.get.assert_awaited_once_with(100, 42)
    assert result is session


async def test_create_topic_raises_if_repo_get_returns_none() -> None:
    mock_topic = MagicMock()
    mock_topic.message_thread_id = 42

    bot = MagicMock()
    bot.create_forum_topic = AsyncMock(return_value=mock_topic)
    bot.send_message = AsyncMock()

    repo = AsyncMock(spec=ChatSessionRepository)
    repo.upsert = AsyncMock()
    repo.get = AsyncMock(return_value=None)

    manager = ProjectThreadManager(repository=repo)
    with pytest.raises(RuntimeError, match="Failed to create topic mapping"):
        await manager.create_topic(
            bot=bot,
            chat_id=100,
            user_id=1,
            directory="/projects/myapp",
            topic_name="myapp",
        )


# ---------------------------------------------------------------------------
# remove_topic
# ---------------------------------------------------------------------------


async def test_remove_topic_calls_close_and_deactivate() -> None:
    bot = MagicMock()
    bot.close_forum_topic = AsyncMock()

    repo = AsyncMock(spec=ChatSessionRepository)
    repo.deactivate = AsyncMock()

    manager = ProjectThreadManager(repository=repo)
    await manager.remove_topic(bot=bot, chat_id=100, message_thread_id=42)

    bot.close_forum_topic.assert_awaited_once_with(chat_id=100, message_thread_id=42)
    repo.deactivate.assert_awaited_once_with(100, 42)


async def test_remove_topic_swallows_telegram_error() -> None:
    from telegram.error import TelegramError

    bot = MagicMock()
    bot.close_forum_topic = AsyncMock(side_effect=TelegramError("already closed"))

    repo = AsyncMock(spec=ChatSessionRepository)
    repo.deactivate = AsyncMock()

    manager = ProjectThreadManager(repository=repo)
    # Should not raise
    await manager.remove_topic(bot=bot, chat_id=100, message_thread_id=42)

    repo.deactivate.assert_awaited_once_with(100, 42)


# ---------------------------------------------------------------------------
# resolve_directory
# ---------------------------------------------------------------------------


async def test_resolve_directory_returns_directory_from_repo() -> None:
    session = make_session(directory="/projects/myapp")

    repo = AsyncMock(spec=ChatSessionRepository)
    repo.get = AsyncMock(return_value=session)

    manager = ProjectThreadManager(repository=repo)
    result = await manager.resolve_directory(chat_id=100, message_thread_id=42)

    repo.get.assert_awaited_once_with(100, 42)
    assert result == "/projects/myapp"


async def test_resolve_directory_returns_none_when_not_found() -> None:
    repo = AsyncMock(spec=ChatSessionRepository)
    repo.get = AsyncMock(return_value=None)

    manager = ProjectThreadManager(repository=repo)
    result = await manager.resolve_directory(chat_id=100, message_thread_id=99)

    assert result is None
