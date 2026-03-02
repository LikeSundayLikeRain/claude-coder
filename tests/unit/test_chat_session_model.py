"""Tests for ChatSessionModel."""

from datetime import datetime, timezone

import pytest

from src.storage.models import ChatSessionModel


class TestChatSessionModelCreation:
    """Test basic ChatSessionModel creation."""

    def test_basic_creation_all_fields(self) -> None:
        """Test creating a model with all fields specified."""
        created_at = datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        model = ChatSessionModel(
            chat_id=12345,
            message_thread_id=99,
            user_id=67890,
            directory="/projects/myapp",
            session_id="abc-123",
            topic_name="myapp",
            is_active=True,
            created_at=created_at,
        )
        assert model.chat_id == 12345
        assert model.message_thread_id == 99
        assert model.user_id == 67890
        assert model.directory == "/projects/myapp"
        assert model.session_id == "abc-123"
        assert model.topic_name == "myapp"
        assert model.is_active is True
        assert model.created_at == created_at

    def test_private_dm_defaults(self) -> None:
        """Test private DM: message_thread_id=0, topic_name=None."""
        model = ChatSessionModel(
            chat_id=111,
            message_thread_id=0,
            user_id=111,
            directory="/home/user/project",
        )
        assert model.message_thread_id == 0
        assert model.topic_name is None
        assert model.session_id is None
        assert model.is_active is True
        assert model.created_at is None

    def test_group_topic(self) -> None:
        """Test group topic: chat_id=group_id, message_thread_id=topic_id."""
        model = ChatSessionModel(
            chat_id=-100987654321,
            message_thread_id=42,
            user_id=999,
            directory="/projects/backend",
            topic_name="backend",
        )
        assert model.chat_id == -100987654321
        assert model.message_thread_id == 42
        assert model.topic_name == "backend"


class TestChatSessionModelFromRowDict:
    """Test ChatSessionModel.from_row_dict classmethod."""

    def test_from_row_dict_basic(self) -> None:
        """Test creation from plain dict."""
        data = {
            "chat_id": 500,
            "message_thread_id": 0,
            "user_id": 500,
            "directory": "/work",
            "session_id": "sess-xyz",
            "topic_name": None,
            "is_active": True,
            "created_at": None,
        }
        model = ChatSessionModel.from_row_dict(data)
        assert model.chat_id == 500
        assert model.session_id == "sess-xyz"
        assert model.is_active is True

    def test_from_row_dict_is_active_int_coercion_truthy(self) -> None:
        """Test is_active coercion from int 1 (SQLite stores bools as 0/1)."""
        data = {
            "chat_id": 1,
            "message_thread_id": 0,
            "user_id": 1,
            "directory": "/tmp",
            "is_active": 1,
            "created_at": None,
        }
        model = ChatSessionModel.from_row_dict(data)
        assert model.is_active is True

    def test_from_row_dict_is_active_int_coercion_falsy(self) -> None:
        """Test is_active coercion from int 0 (SQLite stores bools as 0/1)."""
        data = {
            "chat_id": 2,
            "message_thread_id": 0,
            "user_id": 2,
            "directory": "/tmp",
            "is_active": 0,
            "created_at": None,
        }
        model = ChatSessionModel.from_row_dict(data)
        assert model.is_active is False

    def test_from_row_dict_created_at_string_parsing(self) -> None:
        """Test that created_at ISO string is parsed to datetime."""
        data = {
            "chat_id": 3,
            "message_thread_id": 5,
            "user_id": 3,
            "directory": "/srv",
            "is_active": True,
            "created_at": "2024-06-01T12:00:00+00:00",
        }
        model = ChatSessionModel.from_row_dict(data)
        assert isinstance(model.created_at, datetime)
        assert model.created_at.year == 2024
        assert model.created_at.month == 6
        assert model.created_at.day == 1

    def test_from_row_dict_created_at_none(self) -> None:
        """Test that None created_at stays None."""
        data = {
            "chat_id": 4,
            "message_thread_id": 0,
            "user_id": 4,
            "directory": "/opt",
            "is_active": True,
            "created_at": None,
        }
        model = ChatSessionModel.from_row_dict(data)
        assert model.created_at is None

    def test_from_row_dict_created_at_already_datetime(self) -> None:
        """Test that an already-parsed datetime is passed through unchanged."""
        dt = datetime(2025, 3, 1, 8, 0, 0, tzinfo=timezone.utc)
        data = {
            "chat_id": 5,
            "message_thread_id": 0,
            "user_id": 5,
            "directory": "/var",
            "is_active": True,
            "created_at": dt,
        }
        model = ChatSessionModel.from_row_dict(data)
        assert model.created_at == dt

    def test_from_row_dict_is_active_defaults_true_when_missing(self) -> None:
        """Test is_active defaults to True when key is absent."""
        data = {
            "chat_id": 6,
            "message_thread_id": 0,
            "user_id": 6,
            "directory": "/data",
            "created_at": None,
        }
        model = ChatSessionModel.from_row_dict(data)
        assert model.is_active is True
