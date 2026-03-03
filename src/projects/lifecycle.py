"""Topic lifecycle management — close/reopen/delete forum topics."""

from __future__ import annotations

import structlog
from telegram import Bot
from telegram.error import TelegramError

logger = structlog.get_logger()


class TopicLifecycleManager:
    """Manages Telegram forum topic visual state."""

    async def close_on_idle(
        self, bot: Bot, chat_id: int, message_thread_id: int
    ) -> None:
        """Send idle message and close the topic."""
        try:
            await bot.send_message(
                chat_id=chat_id,
                message_thread_id=message_thread_id,
                text="Session disconnected (idle). Send a message to reconnect.",
            )
        except TelegramError as e:
            logger.debug("idle_message_failed", error=str(e))

        try:
            await bot.close_forum_topic(
                chat_id=chat_id, message_thread_id=message_thread_id
            )
        except TelegramError as e:
            logger.debug("close_topic_failed", error=str(e))

    async def reopen(self, bot: Bot, chat_id: int, message_thread_id: int) -> None:
        """Reopen a closed topic. Silently ignores errors."""
        try:
            await bot.reopen_forum_topic(
                chat_id=chat_id, message_thread_id=message_thread_id
            )
        except TelegramError as e:
            logger.debug("reopen_topic_failed", error=str(e))

    async def delete_confirmed(
        self, bot: Bot, chat_id: int, message_thread_id: int
    ) -> None:
        """Permanently delete a topic. Falls back to close on failure."""
        try:
            await bot.delete_forum_topic(
                chat_id=chat_id, message_thread_id=message_thread_id
            )
        except TelegramError:
            try:
                await bot.close_forum_topic(
                    chat_id=chat_id, message_thread_id=message_thread_id
                )
            except TelegramError as e:
                logger.warning("delete_fallback_close_failed", error=str(e))

    async def rename_topic(
        self, bot: Bot, chat_id: int, message_thread_id: int, name: str
    ) -> None:
        """Rename a topic. Silently ignores errors (e.g. no permission)."""
        try:
            await bot.edit_forum_topic(
                chat_id=chat_id, message_thread_id=message_thread_id, name=name
            )
        except TelegramError as e:
            logger.debug("rename_topic_failed", error=str(e))
