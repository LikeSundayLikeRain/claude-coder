"""Data models for storage.

Using dataclasses for simplicity and type safety.
"""

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, Optional

import aiosqlite


def _parse_datetime(value: Any) -> Any:
    """Parse datetime values from SQLite rows.

    With sqlite3 converters enabled, values may already be datetime instances.
    Without converters, values may be ISO strings.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    return value


@dataclass
class ChatSessionModel:
    """Unified session model — one row per (chat_id, message_thread_id).

    Private DM: chat_id=user_id, message_thread_id=0, topic_name=None.
    Group topic: chat_id=group_id, message_thread_id=topic_id, topic_name set.
    """

    chat_id: int
    message_thread_id: int
    user_id: int
    directory: str
    session_id: Optional[str] = None
    topic_name: Optional[str] = None
    model: Optional[str] = None
    betas: Optional[str] = None
    is_active: bool = True
    created_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "ChatSessionModel":
        """Create from database row."""
        data = dict(row)
        return cls.from_row_dict(data)

    @classmethod
    def from_row_dict(cls, data: dict) -> "ChatSessionModel":
        """Create from a plain dict (useful in tests)."""
        val = data.get("created_at")
        if val and isinstance(val, str):
            data["created_at"] = datetime.fromisoformat(val)
        data["is_active"] = bool(data.get("is_active", True))
        # Columns added in migration 13 — may be absent in old rows
        data.setdefault("model", None)
        data.setdefault("betas", None)
        return cls(**data)


@dataclass
class AuditLogModel:
    """Audit log data model."""

    user_id: int
    event_type: str
    timestamp: datetime
    id: Optional[int] = None
    event_data: Optional[Dict[str, Any]] = None
    success: bool = True
    ip_address: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        data = asdict(self)
        # Convert datetime to ISO format
        if data["timestamp"]:
            data["timestamp"] = data["timestamp"].isoformat()
        # Convert event_data to JSON string if present
        if data["event_data"]:
            data["event_data"] = json.dumps(data["event_data"])
        return data

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "AuditLogModel":
        """Create from database row."""
        data = dict(row)

        # Parse datetime fields
        data["timestamp"] = _parse_datetime(data.get("timestamp"))

        # Parse JSON fields
        if data.get("event_data"):
            try:
                data["event_data"] = json.loads(data["event_data"])
            except (json.JSONDecodeError, TypeError):
                data["event_data"] = {}

        return cls(**data)


@dataclass
class WebhookEventModel:
    """Webhook event data model."""

    event_id: str
    provider: str
    event_type: str
    id: Optional[int] = None
    delivery_id: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None
    processed: bool = False
    received_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        data = asdict(self)
        if data.get("received_at"):
            data["received_at"] = data["received_at"].isoformat()
        if data.get("payload"):
            data["payload"] = json.dumps(data["payload"])
        return data

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "WebhookEventModel":
        """Create from database row."""
        data = dict(row)
        data["received_at"] = _parse_datetime(data.get("received_at"))
        if data.get("payload"):
            try:
                data["payload"] = json.loads(data["payload"])
            except (json.JSONDecodeError, TypeError):
                data["payload"] = {}
        return cls(**data)


@dataclass
class ScheduledJobModel:
    """Scheduled job data model."""

    job_id: str
    job_name: str
    cron_expression: str
    prompt: str
    working_directory: str
    target_chat_ids: str = ""
    skill_name: Optional[str] = None
    created_by: int = 0
    is_active: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        data = asdict(self)
        for key in ["created_at", "updated_at"]:
            if data.get(key):
                data[key] = data[key].isoformat()
        return data

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "ScheduledJobModel":
        """Create from database row."""
        data = dict(row)
        for field in ["created_at", "updated_at"]:
            data[field] = _parse_datetime(data.get(field))
        return cls(**data)
