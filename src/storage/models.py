"""Data models for storage.

Using dataclasses for simplicity and type safety.
"""

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Dict, List, Optional

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
class UserModel:
    """User data model."""

    user_id: int
    telegram_username: Optional[str] = None
    first_seen: Optional[datetime] = None
    last_active: Optional[datetime] = None
    is_allowed: bool = False
    total_cost: float = 0.0
    message_count: int = 0
    session_count: int = 0
    current_directory: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        data = asdict(self)
        # Convert datetime to ISO format
        for key in ["first_seen", "last_active"]:
            if data[key]:
                data[key] = data[key].isoformat()
        return data

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "UserModel":
        """Create from database row."""
        data = dict(row)

        # Parse datetime fields
        for field in ["first_seen", "last_active"]:
            data[field] = _parse_datetime(data.get(field))

        return cls(**data)


@dataclass
class ProjectThreadModel:
    """Project-thread mapping data model."""

    project_slug: str
    chat_id: int
    message_thread_id: int
    topic_name: str
    is_active: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    id: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        data = asdict(self)
        for key in ["created_at", "updated_at"]:
            if data[key]:
                data[key] = data[key].isoformat()
        return data

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "ProjectThreadModel":
        """Create from database row."""
        data = dict(row)

        for field in ["created_at", "updated_at"]:
            val = data.get(field)
            if val and isinstance(val, str):
                data[field] = datetime.fromisoformat(val)
        data["is_active"] = bool(data.get("is_active", True))

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


@dataclass
class BotSessionModel:
    """Persisted bot session state for restart recovery."""

    user_id: int
    session_id: str
    directory: str
    model: Optional[str]
    betas: Optional[List[str]]
    last_active: datetime

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage."""
        data = asdict(self)
        betas = data["betas"]
        data["betas"] = json.dumps(betas) if betas is not None else None
        data["last_active"] = data["last_active"].isoformat()
        return data

    @classmethod
    def from_row(cls, row: Any) -> "BotSessionModel":
        """Create from database row."""
        data = dict(row)

        # Parse betas from JSON string
        if data.get("betas") is not None:
            try:
                data["betas"] = json.loads(data["betas"])
            except (json.JSONDecodeError, TypeError):
                data["betas"] = None

        # Parse last_active from ISO string if needed
        data["last_active"] = _parse_datetime(data["last_active"])

        return cls(**data)
