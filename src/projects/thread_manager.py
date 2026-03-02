"""Dynamic Telegram forum topic management for project threads."""

from pathlib import Path
from typing import List, Optional

import structlog
from telegram import Bot
from telegram.error import TelegramError

from ..storage.models import ChatSessionModel
from ..storage.repositories import ChatSessionRepository

logger = structlog.get_logger()


class ProjectThreadManager:
    """Dynamic topic creation and routing for project threads."""

    def __init__(self, repository: ChatSessionRepository) -> None:
        self.repository = repository

    async def create_topic(
        self,
        bot: Bot,
        chat_id: int,
        user_id: int,
        directory: str,
        topic_name: str,
        session_id: Optional[str] = None,
    ) -> ChatSessionModel:
        """Create a forum topic and store the session binding."""
        topic = await bot.create_forum_topic(chat_id=chat_id, name=topic_name)
        await self.repository.upsert(
            chat_id=chat_id,
            message_thread_id=topic.message_thread_id,
            user_id=user_id,
            directory=directory,
            topic_name=topic_name,
            session_id=session_id,
        )
        try:
            await bot.send_message(
                chat_id=chat_id,
                message_thread_id=topic.message_thread_id,
                text=(
                    f"\U0001f9f5 <b>{topic_name}</b>\n\n"
                    "Ready. Send messages here to work on this project."
                ),
                parse_mode="HTML",
            )
        except TelegramError as e:
            logger.warning("bootstrap_message_failed", error=str(e))

        result = await self.repository.get(chat_id, topic.message_thread_id)
        if not result:
            raise RuntimeError("Failed to create topic mapping")
        return result

    async def remove_topic(
        self, bot: Bot, chat_id: int, message_thread_id: int
    ) -> None:
        """Close a forum topic and deactivate its binding."""
        try:
            await bot.close_forum_topic(
                chat_id=chat_id, message_thread_id=message_thread_id
            )
        except TelegramError:
            pass
        await self.repository.deactivate(chat_id, message_thread_id)

    async def resolve_directory(
        self, chat_id: int, message_thread_id: int
    ) -> Optional[str]:
        """Resolve the directory bound to a chat+thread, or None."""
        mapping = await self.repository.get(chat_id, message_thread_id)
        return mapping.directory if mapping else None

    async def list_topics(self, chat_id: int) -> List[ChatSessionModel]:
        """List all active topic bindings for a chat."""
        return await self.repository.list_active_by_chat(chat_id)

    @staticmethod
    def generate_topic_name(
        directory: str,
        existing_names: List[str],
        override_name: Optional[str] = None,
    ) -> str:
        """Generate a topic name from directory with auto-suffix on collision.

        Auto-suffix: myapp, myapp (2), myapp (3), ...
        If override_name is provided, use it as-is.
        """
        if override_name:
            return override_name
        basename = Path(directory).name
        if basename not in existing_names:
            return basename
        n = 2
        while f"{basename} ({n})" in existing_names:
            n += 1
        return f"{basename} ({n})"
