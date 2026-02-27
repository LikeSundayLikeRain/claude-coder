"""Data access layer using repository pattern.

Features:
- Clean data access API
- Query optimization
- Error handling
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Dict, List, Optional

import aiosqlite
import structlog

from .database import DatabaseManager
from .models import (
    AuditLogModel,
    BotSessionModel,
    ProjectThreadModel,
    UserModel,
)

logger = structlog.get_logger()


class UserRepository:
    """User data access."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize repository."""
        self.db = db_manager

    async def get_user(self, user_id: int) -> Optional[UserModel]:
        """Get user by ID."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            )
            row = await cursor.fetchone()
            return UserModel.from_row(row) if row else None

    async def create_user(self, user: UserModel) -> UserModel:
        """Create new user."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO users
                (user_id, telegram_username, first_seen,
                 last_active, is_allowed)
                VALUES (?, ?, ?, ?, ?)
            """,
                (
                    user.user_id,
                    user.telegram_username,
                    user.first_seen or datetime.now(UTC),
                    user.last_active or datetime.now(UTC),
                    user.is_allowed,
                ),
            )
            await conn.commit()

            logger.info(
                "Created user", user_id=user.user_id, username=user.telegram_username
            )
            return user

    async def update_user(self, user: UserModel):
        """Update user data."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                UPDATE users
                SET telegram_username = ?, last_active = ?,
                    total_cost = ?, message_count = ?, session_count = ?
                WHERE user_id = ?
            """,
                (
                    user.telegram_username,
                    user.last_active or datetime.now(UTC),
                    user.total_cost,
                    user.message_count,
                    user.session_count,
                    user.user_id,
                ),
            )
            await conn.commit()

    async def get_allowed_users(self) -> List[int]:
        """Get list of allowed user IDs."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT user_id FROM users WHERE is_allowed = TRUE"
            )
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

    async def set_user_allowed(self, user_id: int, allowed: bool):
        """Set user allowed status."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                "UPDATE users SET is_allowed = ? WHERE user_id = ?", (allowed, user_id)
            )
            await conn.commit()

            logger.info("Updated user permissions", user_id=user_id, allowed=allowed)

    async def get_all_users(self) -> List[UserModel]:
        """Get all users."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute("SELECT * FROM users ORDER BY first_seen DESC")
            rows = await cursor.fetchall()
            return [UserModel.from_row(row) for row in rows]


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
                WHERE chat_id = ? AND message_thread_id = ? AND is_active = TRUE
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
                    SET is_active = FALSE, updated_at = CURRENT_TIMESTAMP
                    WHERE chat_id = ?
                      AND project_slug NOT IN ({placeholders})
                      AND is_active = TRUE
                """
                params = [chat_id] + active_project_slugs
                cursor = await conn.execute(query, params)
            else:
                cursor = await conn.execute(
                    """
                    UPDATE project_threads
                    SET is_active = FALSE, updated_at = CURRENT_TIMESTAMP
                    WHERE chat_id = ? AND is_active = TRUE
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
                      AND is_active = TRUE
                      AND project_slug NOT IN ({placeholders})
                    ORDER BY project_slug ASC
                """
                params = [chat_id] + active_project_slugs
                cursor = await conn.execute(query, params)
            else:
                cursor = await conn.execute(
                    """
                    SELECT * FROM project_threads
                    WHERE chat_id = ? AND is_active = TRUE
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
                query += " AND is_active = TRUE"
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


class BotSessionRepository:
    """Bot session state data access for restart recovery."""

    def __init__(self, db: DatabaseManager) -> None:
        """Initialize repository."""
        self.db = db

    async def upsert(
        self,
        user_id: int,
        session_id: str,
        directory: str,
        model: Optional[str] = None,
        betas: Optional[List[str]] = None,
    ) -> None:
        """Insert or update bot session for a user."""
        betas_json = json.dumps(betas) if betas is not None else None
        last_active = datetime.now(UTC)
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                INSERT OR REPLACE INTO bot_sessions
                    (user_id, session_id, directory, model, betas, last_active)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, session_id, directory, model, betas_json, last_active),
            )
            await conn.commit()

    async def get_by_user(self, user_id: int) -> Optional[BotSessionModel]:
        """Get bot session by user ID."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM bot_sessions WHERE user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
            return BotSessionModel.from_row(row) if row else None

    async def delete(self, user_id: int) -> None:
        """Delete bot session for a user."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                "DELETE FROM bot_sessions WHERE user_id = ?",
                (user_id,),
            )
            await conn.commit()

    async def cleanup_expired(self, max_age_hours: int = 24) -> int:
        """Delete entries older than max_age_hours; returns deleted count."""
        cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "DELETE FROM bot_sessions WHERE last_active < ?",
                (cutoff,),
            )
            await conn.commit()
            return cursor.rowcount
