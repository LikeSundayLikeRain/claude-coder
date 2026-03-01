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
    ProjectThreadModel,
    UserModel,
)

logger = structlog.get_logger()


class UserRepository:
    """User data access."""

    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager

    async def get_user(self, user_id: int) -> Optional[UserModel]:
        """Get user by ID."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            )
            row = await cursor.fetchone()
            return UserModel.from_row(row) if row else None

    async def ensure_user(self, user_id: int, telegram_username: Optional[str] = None) -> None:
        """Create user row if it doesn't exist."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO users (user_id, telegram_username) VALUES (?, ?)",
                (user_id, telegram_username),
            )
            await conn.commit()

    async def update_session(self, user_id: int, session_id: str, directory: str) -> None:
        """Update session_id and directory for a user."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO users (user_id, session_id, directory)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    session_id = excluded.session_id,
                    directory = excluded.directory
                """,
                (user_id, session_id, directory),
            )
            await conn.commit()

    async def update_directory(self, user_id: int, directory: str) -> None:
        """Update directory and clear session_id (e.g. after /repo)."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                "UPDATE users SET directory = ?, session_id = NULL WHERE user_id = ?",
                (directory, user_id),
            )
            await conn.commit()

    async def clear_session(self, user_id: int) -> None:
        """Clear session_id (e.g. after /new)."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                "UPDATE users SET session_id = NULL WHERE user_id = ?",
                (user_id,),
            )
            await conn.commit()


class ProjectThreadRepository:
    """Project-thread mapping data access."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize repository."""
        self.db = db_manager

    async def get_by_chat_thread(
        self, chat_id: int, message_thread_id: int
    ) -> Optional[ProjectThreadModel]:
        """Find active mapping by chat+thread."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM project_threads
                WHERE chat_id = ? AND message_thread_id = ? AND is_active = 1
            """,
                (chat_id, message_thread_id),
            )
            row = await cursor.fetchone()
            return ProjectThreadModel.from_row(row) if row else None

    async def get_by_chat_project(
        self, chat_id: int, project_slug: str
    ) -> Optional[ProjectThreadModel]:
        """Find mapping by chat+project slug."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM project_threads
                WHERE chat_id = ? AND project_slug = ?
            """,
                (chat_id, project_slug),
            )
            row = await cursor.fetchone()
            return ProjectThreadModel.from_row(row) if row else None

    async def upsert_mapping(
        self,
        project_slug: str,
        chat_id: int,
        message_thread_id: int,
        topic_name: str,
        is_active: bool = True,
    ) -> ProjectThreadModel:
        """Create or update mapping by unique chat+project key."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO project_threads (
                    project_slug, chat_id, message_thread_id, topic_name, is_active
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, project_slug) DO UPDATE SET
                    message_thread_id = excluded.message_thread_id,
                    topic_name = excluded.topic_name,
                    is_active = excluded.is_active,
                    updated_at = CURRENT_TIMESTAMP
            """,
                (project_slug, chat_id, message_thread_id, topic_name, is_active),
            )
            await conn.commit()

        mapping = await self.get_by_chat_project(
            chat_id=chat_id, project_slug=project_slug
        )
        if not mapping:
            raise RuntimeError("Failed to upsert project thread mapping")
        return mapping

    async def deactivate_missing_projects(
        self, chat_id: int, active_project_slugs: List[str]
    ) -> int:
        """Deactivate mappings for projects no longer enabled/present."""
        async with self.db.get_connection() as conn:
            if active_project_slugs:
                placeholders = ",".join("?" for _ in active_project_slugs)
                query = f"""
                    UPDATE project_threads
                    SET is_active = 0, updated_at = CURRENT_TIMESTAMP
                    WHERE chat_id = ?
                      AND project_slug NOT IN ({placeholders})
                      AND is_active = 1
                """
                params = [chat_id] + active_project_slugs
                cursor = await conn.execute(query, params)
            else:
                cursor = await conn.execute(
                    """
                    UPDATE project_threads
                    SET is_active = 0, updated_at = CURRENT_TIMESTAMP
                    WHERE chat_id = ? AND is_active = 1
                """,
                    (chat_id,),
                )
            await conn.commit()
            return cursor.rowcount

    async def list_stale_active_mappings(
        self, chat_id: int, active_project_slugs: List[str]
    ) -> List[ProjectThreadModel]:
        """List active mappings that are no longer enabled/present."""
        async with self.db.get_connection() as conn:
            if active_project_slugs:
                placeholders = ",".join("?" for _ in active_project_slugs)
                query = f"""
                    SELECT * FROM project_threads
                    WHERE chat_id = ?
                      AND is_active = 1
                      AND project_slug NOT IN ({placeholders})
                    ORDER BY project_slug ASC
                """
                params = [chat_id] + active_project_slugs
                cursor = await conn.execute(query, params)
            else:
                cursor = await conn.execute(
                    """
                    SELECT * FROM project_threads
                    WHERE chat_id = ? AND is_active = 1
                    ORDER BY project_slug ASC
                """,
                    (chat_id,),
                )
            rows = await cursor.fetchall()
            return [ProjectThreadModel.from_row(row) for row in rows]

    async def set_active(self, chat_id: int, project_slug: str, is_active: bool) -> int:
        """Set active flag for a mapping by chat+project."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE project_threads
                SET is_active = ?, updated_at = CURRENT_TIMESTAMP
                WHERE chat_id = ? AND project_slug = ?
            """,
                (is_active, chat_id, project_slug),
            )
            await conn.commit()
            return cursor.rowcount

    async def list_by_chat(
        self, chat_id: int, active_only: bool = True
    ) -> List[ProjectThreadModel]:
        """List mappings for a chat."""
        async with self.db.get_connection() as conn:
            query = "SELECT * FROM project_threads WHERE chat_id = ?"
            params = [chat_id]
            if active_only:
                query += " AND is_active = 1"
            query += " ORDER BY project_slug ASC"
            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
            return [ProjectThreadModel.from_row(row) for row in rows]


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


