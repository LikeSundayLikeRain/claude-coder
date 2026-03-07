"""Data access layer using repository pattern.

Features:
- Clean data access API
- Query optimization
- Error handling
"""

import json
from typing import List, Optional

import structlog

from .database import DatabaseManager
from .models import (
    AuditLogModel,
    ChatSessionModel,
)

logger = structlog.get_logger()


class ChatSessionRepository:
    """Unified session data access — one row per (chat_id, message_thread_id)."""

    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager

    async def get(
        self, chat_id: int, message_thread_id: int
    ) -> Optional[ChatSessionModel]:
        """Get active session by PK."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM chat_sessions
                WHERE chat_id = ? AND message_thread_id = ? AND is_active = 1
                """,
                (chat_id, message_thread_id),
            )
            row = await cursor.fetchone()
            return ChatSessionModel.from_row(row) if row else None

    async def upsert(
        self,
        chat_id: int,
        message_thread_id: int,
        user_id: int,
        directory: str,
        session_id: Optional[str] = None,
        topic_name: Optional[str] = None,
    ) -> None:
        """Insert or update a session row. Uses ON CONFLICT to update."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO chat_sessions
                    (chat_id, message_thread_id, user_id, directory, session_id, topic_name, is_active)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(chat_id, message_thread_id) DO UPDATE SET
                    session_id = COALESCE(excluded.session_id, chat_sessions.session_id),
                    directory  = excluded.directory,
                    topic_name = COALESCE(excluded.topic_name, chat_sessions.topic_name),
                    is_active  = 1
                """,
                (
                    chat_id,
                    message_thread_id,
                    user_id,
                    directory,
                    session_id,
                    topic_name,
                ),
            )
            await conn.commit()

    async def set_model(
        self,
        chat_id: int,
        message_thread_id: int,
        model: Optional[str],
        betas: Optional[str],
    ) -> None:
        """Persist model/betas preference for a session."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                UPDATE chat_sessions
                SET model = ?, betas = ?
                WHERE chat_id = ? AND message_thread_id = ? AND is_active = 1
                """,
                (model, betas, chat_id, message_thread_id),
            )
            await conn.commit()

    async def deactivate(self, chat_id: int, message_thread_id: int) -> int:
        """Soft-delete (is_active=0). Returns rowcount."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE chat_sessions
                SET is_active = 0
                WHERE chat_id = ? AND message_thread_id = ?
                """,
                (chat_id, message_thread_id),
            )
            await conn.commit()
            return cursor.rowcount

    async def delete(self, chat_id: int, message_thread_id: int) -> None:
        """Hard-delete a row (used by /new)."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                DELETE FROM chat_sessions
                WHERE chat_id = ? AND message_thread_id = ?
                """,
                (chat_id, message_thread_id),
            )
            await conn.commit()

    async def find_by_session_id(
        self, chat_id: int, session_id: str
    ) -> Optional[ChatSessionModel]:
        """Find an active topic bound to a given Claude session_id."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM chat_sessions
                WHERE chat_id = ? AND session_id = ? AND is_active = 1
                LIMIT 1
                """,
                (chat_id, session_id),
            )
            row = await cursor.fetchone()
            return ChatSessionModel.from_row(row) if row else None

    async def list_active_by_chat(self, chat_id: int) -> List[ChatSessionModel]:
        """All active sessions in a chat (for /status dashboard). ORDER BY directory ASC."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM chat_sessions
                WHERE chat_id = ? AND is_active = 1
                ORDER BY directory ASC
                """,
                (chat_id,),
            )
            rows = await cursor.fetchall()
            return [ChatSessionModel.from_row(row) for row in rows]

    async def list_by_user(self, user_id: int) -> List[ChatSessionModel]:
        """All active sessions for a user across all chats (reverse lookup). ORDER BY created_at DESC."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM chat_sessions
                WHERE user_id = ? AND is_active = 1
                ORDER BY created_at DESC
                """,
                (user_id,),
            )
            rows = await cursor.fetchall()
            return [ChatSessionModel.from_row(row) for row in rows]

    async def get_by_user_directory(
        self, user_id: int, directory: str
    ) -> Optional[ChatSessionModel]:
        """Private DM lookup: WHERE user_id=? AND directory=? AND message_thread_id=0 AND is_active=1."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM chat_sessions
                WHERE user_id = ? AND directory = ? AND message_thread_id = 0 AND is_active = 1
                """,
                (user_id, directory),
            )
            row = await cursor.fetchone()
            return ChatSessionModel.from_row(row) if row else None

    async def count_active_by_chat_directory(self, chat_id: int, directory: str) -> int:
        """Count active sessions for a directory in a chat (for auto-suffix naming)."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT COUNT(*) FROM chat_sessions
                WHERE chat_id = ? AND directory = ? AND is_active = 1
                """,
                (chat_id, directory),
            )
            row = await cursor.fetchone()
            return row[0] if row else 0


class AuditLogRepository:
    """Audit log data access."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize repository."""
        self.db = db_manager

    async def log_event(self, audit_log: AuditLogModel) -> int:
        """Log audit event and return ID."""
        async with self.db.get_connection() as conn:
            event_data_json = (
                json.dumps(audit_log.event_data) if audit_log.event_data else None
            )

            cursor = await conn.execute(
                """
                INSERT INTO audit_log
                (user_id, event_type, event_data, success, timestamp, ip_address)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_log.user_id,
                    audit_log.event_type,
                    event_data_json,
                    audit_log.success,
                    audit_log.timestamp,
                    audit_log.ip_address,
                ),
            )
            await conn.commit()
            return cursor.lastrowid

    async def get_user_audit_log(
        self, user_id: int, limit: int = 100
    ) -> List[AuditLogModel]:
        """Get audit log for user."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM audit_log
                WHERE user_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (user_id, limit),
            )
            rows = await cursor.fetchall()
            return [AuditLogModel.from_row(row) for row in rows]

    async def get_recent_audit_log(self, hours: int = 24) -> List[AuditLogModel]:
        """Get recent audit log entries."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM audit_log
                WHERE timestamp > datetime('now', '-' || ? || ' hours')
                ORDER BY timestamp DESC
                """,
                (hours,),
            )
            rows = await cursor.fetchall()
            return [AuditLogModel.from_row(row) for row in rows]
