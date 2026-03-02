import asyncio
from unittest.mock import AsyncMock, MagicMock
import pytest

from src.claude.client_manager import ClientManager
from src.projects.lifecycle import TopicLifecycleManager


@pytest.mark.asyncio
async def test_on_exit_closes_topic_for_group():
    """on_exit should schedule close_on_idle for group topics."""
    lifecycle = TopicLifecycleManager()
    lifecycle.close_on_idle = AsyncMock()
    bot = AsyncMock()

    repo = AsyncMock()
    repo.get = AsyncMock(return_value=None)
    options_builder = MagicMock()

    cm = ClientManager(
        chat_session_repo=repo,
        options_builder=options_builder,
        bot=bot,
        lifecycle_manager=lifecycle,
    )

    on_exit = cm._make_on_exit(user_id=111, chat_id=-1001234, message_thread_id=42)
    on_exit(111)
    await asyncio.sleep(0.05)

    lifecycle.close_on_idle.assert_called_once_with(bot, chat_id=-1001234, message_thread_id=42)


@pytest.mark.asyncio
async def test_on_exit_skips_close_for_private_dm():
    """on_exit should NOT close topic for private DMs (chat_id == user_id, thread_id == 0)."""
    lifecycle = TopicLifecycleManager()
    lifecycle.close_on_idle = AsyncMock()
    bot = AsyncMock()

    repo = AsyncMock()
    options_builder = MagicMock()

    cm = ClientManager(
        chat_session_repo=repo,
        options_builder=options_builder,
        bot=bot,
        lifecycle_manager=lifecycle,
    )

    on_exit = cm._make_on_exit(user_id=111, chat_id=111, message_thread_id=0)
    on_exit(111)
    await asyncio.sleep(0.05)

    lifecycle.close_on_idle.assert_not_called()


@pytest.mark.asyncio
async def test_on_exit_works_without_lifecycle():
    """If no lifecycle_manager is provided, on_exit still removes the client."""
    repo = AsyncMock()
    options_builder = MagicMock()

    cm = ClientManager(chat_session_repo=repo, options_builder=options_builder)
    cm._clients[(111, -1001234, 42)] = MagicMock()

    on_exit = cm._make_on_exit(user_id=111, chat_id=-1001234, message_thread_id=42)
    on_exit(111)

    assert (111, -1001234, 42) not in cm._clients
