"""Tests for BotSessionModel."""

import json
from datetime import UTC, datetime

from src.storage.models import BotSessionModel


class TestBotSessionModelFromRow:
    """Tests for BotSessionModel.from_row()."""

    def test_from_row_all_fields_populated(self):
        """from_row() correctly parses a row with all fields set."""
        last_active = datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC)
        row = {
            "user_id": 123456,
            "session_id": "abc-def-123",
            "directory": "/home/user/project",
            "model": "claude-opus-4-5",
            "betas": json.dumps(["interleaved-thinking-2025-05-14"]),
            "last_active": last_active,
        }

        model = BotSessionModel.from_row(row)

        assert model.user_id == 123456
        assert model.session_id == "abc-def-123"
        assert model.directory == "/home/user/project"
        assert model.model == "claude-opus-4-5"
        assert model.betas == ["interleaved-thinking-2025-05-14"]
        assert model.last_active == last_active

    def test_from_row_nullable_fields_none(self):
        """from_row() handles None for model and betas."""
        last_active = datetime(2024, 6, 1, 0, 0, 0, tzinfo=UTC)
        row = {
            "user_id": 789,
            "session_id": "xyz-999",
            "directory": "/tmp/work",
            "model": None,
            "betas": None,
            "last_active": last_active,
        }

        model = BotSessionModel.from_row(row)

        assert model.model is None
        assert model.betas is None
        assert model.last_active == last_active

    def test_from_row_last_active_as_string(self):
        """from_row() parses last_active from ISO string."""
        row = {
            "user_id": 42,
            "session_id": "str-test",
            "directory": "/foo",
            "model": None,
            "betas": None,
            "last_active": "2024-03-20T12:00:00+00:00",
        }

        model = BotSessionModel.from_row(row)

        assert isinstance(model.last_active, datetime)
        assert model.last_active.year == 2024
        assert model.last_active.month == 3
        assert model.last_active.day == 20

    def test_from_row_betas_multiple_values(self):
        """from_row() parses betas list with multiple entries."""
        row = {
            "user_id": 1,
            "session_id": "multi-beta",
            "directory": "/dir",
            "model": "claude-sonnet-4-5",
            "betas": json.dumps(["beta-a", "beta-b", "beta-c"]),
            "last_active": datetime(2024, 1, 1, tzinfo=UTC),
        }

        model = BotSessionModel.from_row(row)

        assert model.betas == ["beta-a", "beta-b", "beta-c"]

    def test_roundtrip_empty_betas(self):
        """Empty betas list survives a to_dict/from_row round-trip."""
        original = BotSessionModel(
            user_id=1,
            session_id="s",
            directory="/d",
            model=None,
            betas=[],
            last_active=datetime(2024, 1, 1, tzinfo=UTC),
        )
        recovered = BotSessionModel.from_row(original.to_dict())
        assert recovered.betas == []


class TestBotSessionModelToDict:
    """Tests for BotSessionModel.to_dict()."""

    def test_to_dict_all_fields(self):
        """to_dict() serializes all fields correctly."""
        last_active = datetime(2024, 5, 10, 8, 0, 0, tzinfo=UTC)
        model = BotSessionModel(
            user_id=100,
            session_id="sess-abc",
            directory="/projects/myapp",
            model="claude-haiku-4-5",
            betas=["interleaved-thinking-2025-05-14"],
            last_active=last_active,
        )

        result = model.to_dict()

        assert result["user_id"] == 100
        assert result["session_id"] == "sess-abc"
        assert result["directory"] == "/projects/myapp"
        assert result["model"] == "claude-haiku-4-5"
        expected_betas = json.dumps(["interleaved-thinking-2025-05-14"])
        assert result["betas"] == expected_betas
        assert result["last_active"] == last_active.isoformat()

    def test_to_dict_betas_empty_list(self):
        """to_dict() serializes empty betas list as JSON string, not None."""
        model = BotSessionModel(
            user_id=1,
            session_id="s",
            directory="/d",
            model=None,
            betas=[],
            last_active=datetime(2024, 1, 1, tzinfo=UTC),
        )
        result = model.to_dict()
        assert result["betas"] == "[]"

    def test_to_dict_betas_none(self):
        """to_dict() keeps betas as None when not set."""
        model = BotSessionModel(
            user_id=200,
            session_id="no-betas",
            directory="/some/dir",
            model=None,
            betas=None,
            last_active=datetime(2024, 1, 1, tzinfo=UTC),
        )

        result = model.to_dict()

        assert result["betas"] is None
        assert result["model"] is None

    def test_to_dict_last_active_is_iso_string(self):
        """to_dict() converts last_active datetime to ISO string."""
        last_active = datetime(2024, 11, 22, 15, 45, 30, tzinfo=UTC)
        model = BotSessionModel(
            user_id=300,
            session_id="iso-test",
            directory="/dir",
            model=None,
            betas=None,
            last_active=last_active,
        )

        result = model.to_dict()

        assert isinstance(result["last_active"], str)
        assert result["last_active"] == "2024-11-22T15:45:30+00:00"
