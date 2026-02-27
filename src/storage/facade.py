"""Unified storage interface.

Provides simple API for the rest of the application.
"""

from datetime import UTC, datetime
from typing import Any, Dict, Optional

import structlog

from .database import DatabaseManager
from .models import (
    AuditLogModel,
    UserModel,
)
from .repositories import (
    AuditLogRepository,
    BotSessionRepository,
    ProjectThreadRepository,
    UserRepository,
)

logger = structlog.get_logger()


class Storage:
    """Main storage interface."""

    def __init__(self, database_url: str):
        """Initialize storage with database URL."""
        self.db_manager = DatabaseManager(database_url)
        self.users = UserRepository(self.db_manager)
        self.project_threads = ProjectThreadRepository(self.db_manager)
        self.audit = AuditLogRepository(self.db_manager)
        self.bot_sessions = BotSessionRepository(self.db_manager)

    async def initialize(self):
        """Initialize storage system."""
        logger.info("Initializing storage system")
        await self.db_manager.initialize()
        logger.info("Storage system initialized")

    async def close(self):
        """Close storage connections."""
        logger.info("Closing storage system")
        await self.db_manager.close()

    async def health_check(self) -> bool:
        """Check storage system health."""
        return await self.db_manager.health_check()

    # High-level operations

    async def get_or_create_user(
        self, user_id: int, username: Optional[str] = None
    ) -> UserModel:
        """Get or create user."""
        user = await self.users.get_user(user_id)

        if not user:
            logger.info("Creating new user", user_id=user_id, username=username)
            user = UserModel(
                user_id=user_id,
                telegram_username=username,
                first_seen=datetime.now(UTC),
                last_active=datetime.now(UTC),
                is_allowed=False,  # Default to not allowed
            )
            await self.users.create_user(user)

        return user

    async def log_security_event(
        self,
        user_id: int,
        event_type: str,
        event_data: Dict[str, Any],
        success: bool = True,
        ip_address: Optional[str] = None,
    ):
        """Log security-related event."""
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
    ):
        """Log bot-related event."""
        audit_event = AuditLogModel(
            id=None,
            user_id=user_id,
            event_type=event_type,
            event_data=event_data,
            success=success,
            timestamp=datetime.now(UTC),
        )
        await self.audit.log_event(audit_event)

    # Convenience methods

    async def is_user_allowed(self, user_id: int) -> bool:
        """Check if user is allowed."""
        user = await self.users.get_user(user_id)
        return user.is_allowed if user else False

    async def save_user_directory(self, user_id: int, directory: str) -> None:
        """Persist user's current working directory."""
        async with self.db_manager.get_connection() as conn:
            await conn.execute(
                "UPDATE users SET current_directory = ? WHERE user_id = ?",
                (directory, user_id),
            )
            await conn.commit()

    async def load_user_directory(self, user_id: int) -> Optional[str]:
        """Load user's persisted working directory."""
        async with self.db_manager.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT current_directory FROM users WHERE user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
            return row[0] if row and row[0] else None
