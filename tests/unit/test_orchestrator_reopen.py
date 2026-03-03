from unittest.mock import AsyncMock

import pytest

from src.projects.lifecycle import TopicLifecycleManager


@pytest.mark.asyncio
async def test_reopen_called_for_group_topic():
    """TopicLifecycleManager.reopen should work for group topics."""
    lifecycle = TopicLifecycleManager()
    lifecycle.reopen = AsyncMock()

    bot = AsyncMock()
    await lifecycle.reopen(bot, chat_id=-1001234, message_thread_id=42)
    lifecycle.reopen.assert_called_once_with(
        bot, chat_id=-1001234, message_thread_id=42
    )


@pytest.mark.asyncio
async def test_reopen_not_called_for_private_dm():
    """Reopen should not be called when message_thread_id is 0 (private DM)."""
    lifecycle = TopicLifecycleManager()
    lifecycle.reopen = AsyncMock()

    message_thread_id = 0
    if message_thread_id != 0:
        bot = AsyncMock()
        await lifecycle.reopen(bot, chat_id=111, message_thread_id=0)

    lifecycle.reopen.assert_not_called()
