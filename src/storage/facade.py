"""Unified storage interface."""

from datetime import UTC, datetime
from typing import Any, Dict, Optional

import structlog

from .database import DatabaseManager
from .models import AuditLogModel, ChatSessionModel
from .repositories import AuditLogRepository, ChatSessionRepository

logger = structlog.get_logger()


class Storage:
    """Main storage interface."""

    def __init__(self, database_url: str):
        self.db_manager = DatabaseManager(database_url)
        self.chat_sessions = ChatSessionRepository(self.db_manager)
        self.audit = AuditLogRepository(self.db_manager)

    async def initialize(self) -> None:
        await self.db_manager.initialize()

    async def close(self) -> None:
        await self.db_manager.close()

    async def health_check(self) -> bool:
        return await self.db_manager.health_check()

    # --- Session convenience methods ---

    async def save_session(
        self,
        chat_id: int,
        message_thread_id: int,
        user_id: int,
        directory: str,
        session_id: Optional[str] = None,
        topic_name: Optional[str] = None,
    ) -> None:
        """Persist or update a session."""
        await self.chat_sessions.upsert(
            chat_id=chat_id,
            message_thread_id=message_thread_id,
            user_id=user_id,
            directory=directory,
            session_id=session_id,
            topic_name=topic_name,
        )

    async def load_session(
        self,
        chat_id: int,
        message_thread_id: int,
    ) -> Optional[ChatSessionModel]:
        """Load active session by PK."""
        return await self.chat_sessions.get(chat_id, message_thread_id)

    async def clear_session(self, chat_id: int, message_thread_id: int) -> None:
        """Remove session row (used by /new)."""
        await self.chat_sessions.delete(chat_id, message_thread_id)

    # --- Audit (unchanged) ---

    async def log_security_event(
        self,
        user_id: int,
        event_type: str,
        event_data: Dict[str, Any],
        success: bool = True,
        ip_address: Optional[str] = None,
    ) -> None:
        audit_event = AuditLogModel(
            id=None,
            user_id=user_id,
            event_type=event_type,
            event_data=event_data,
            success=success,
            timestamp=datetime.now(UTC),
            ip_address=ip_address,
        )
        await self.audit.log_event(audit_event)

    async def log_bot_event(
        self,
        user_id: int,
        event_type: str,
        event_data: Dict[str, Any],
        success: bool = True,
    ) -> None:
        audit_event = AuditLogModel(
            id=None,
            user_id=user_id,
            event_type=event_type,
            event_data=event_data,
            success=success,
            timestamp=datetime.now(UTC),
        )
        await self.audit.log_event(audit_event)
