"""Unified storage interface."""

from datetime import UTC, datetime
from typing import Any, Dict, Optional

import structlog

from .database import DatabaseManager
from .models import AuditLogModel, UserModel
from .repositories import AuditLogRepository, ProjectThreadRepository, UserRepository

logger = structlog.get_logger()


class Storage:
    """Main storage interface."""

    def __init__(self, database_url: str):
        self.db_manager = DatabaseManager(database_url)
        self.users = UserRepository(self.db_manager)
        self.project_threads = ProjectThreadRepository(self.db_manager)
        self.audit = AuditLogRepository(self.db_manager)

    async def initialize(self):
        await self.db_manager.initialize()

    async def close(self):
        await self.db_manager.close()

    async def health_check(self) -> bool:
        return await self.db_manager.health_check()

    # --- User session state (single source of truth) ---

    async def save_user_session(self, user_id: int, session_id: str, directory: str) -> None:
        """Persist session_id + directory together."""
        await self.users.update_session(user_id, session_id, directory)

    async def load_user_state(self, user_id: int) -> Optional[UserModel]:
        """Load user's persisted state (session_id + directory)."""
        return await self.users.get_user(user_id)

    async def save_user_directory(self, user_id: int, directory: str) -> None:
        """Update directory and clear session (used by /repo)."""
        await self.users.update_directory(user_id, directory)

    async def clear_user_session(self, user_id: int) -> None:
        """Clear session_id only (used by /new)."""
        await self.users.clear_session(user_id)

    # --- Audit ---

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
