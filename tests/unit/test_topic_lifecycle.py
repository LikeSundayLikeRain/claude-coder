from unittest.mock import AsyncMock, patch
import pytest
from telegram.error import TelegramError

from src.projects.lifecycle import TopicLifecycleManager


@pytest.fixture
def bot():
    mock = AsyncMock()
    mock.send_message = AsyncMock()
    mock.close_forum_topic = AsyncMock()
    mock.reopen_forum_topic = AsyncMock()
    mock.delete_forum_topic = AsyncMock()
    return mock


@pytest.fixture
def lifecycle():
    return TopicLifecycleManager()


@pytest.mark.asyncio
async def test_close_on_idle(lifecycle, bot):
    await lifecycle.close_on_idle(bot, chat_id=-1001234, message_thread_id=42)
    bot.send_message.assert_called_once()
    assert "idle" in bot.send_message.call_args.kwargs.get("text", "").lower() or \
           "idle" in str(bot.send_message.call_args)
    bot.close_forum_topic.assert_called_once_with(chat_id=-1001234, message_thread_id=42)


@pytest.mark.asyncio
async def test_close_on_idle_ignores_telegram_error(lifecycle, bot):
    bot.close_forum_topic.side_effect = TelegramError("topic already closed")
    # Should not raise
    await lifecycle.close_on_idle(bot, chat_id=-1001234, message_thread_id=42)


@pytest.mark.asyncio
async def test_reopen(lifecycle, bot):
    await lifecycle.reopen(bot, chat_id=-1001234, message_thread_id=42)
    bot.reopen_forum_topic.assert_called_once_with(chat_id=-1001234, message_thread_id=42)


@pytest.mark.asyncio
async def test_reopen_ignores_error(lifecycle, bot):
    bot.reopen_forum_topic.side_effect = TelegramError("not closed")
    await lifecycle.reopen(bot, chat_id=-1001234, message_thread_id=42)


@pytest.mark.asyncio
async def test_delete_confirmed(lifecycle, bot):
    await lifecycle.delete_confirmed(bot, chat_id=-1001234, message_thread_id=42)
    bot.delete_forum_topic.assert_called_once_with(chat_id=-1001234, message_thread_id=42)


@pytest.mark.asyncio
async def test_delete_falls_back_to_close(lifecycle, bot):
    bot.delete_forum_topic.side_effect = TelegramError("cannot delete")
    await lifecycle.delete_confirmed(bot, chat_id=-1001234, message_thread_id=42)
    bot.close_forum_topic.assert_called_once_with(chat_id=-1001234, message_thread_id=42)


@pytest.mark.asyncio
async def test_rename_topic_calls_edit():
    lifecycle = TopicLifecycleManager()
    bot = AsyncMock()
    await lifecycle.rename_topic(bot, chat_id=-1001234, message_thread_id=42, name="Fix auth bug")
    bot.edit_forum_topic.assert_called_once_with(
        chat_id=-1001234, message_thread_id=42, name="Fix auth bug"
    )


@pytest.mark.asyncio
async def test_rename_topic_swallows_error():
    lifecycle = TopicLifecycleManager()
    bot = AsyncMock()
    bot.edit_forum_topic.side_effect = TelegramError("forbidden")
    # Should not raise
    await lifecycle.rename_topic(bot, chat_id=-1001234, message_thread_id=42, name="test")
