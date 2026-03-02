"""Tests for the updated Storage facade (chat_sessions-based)."""

import pytest

from src.storage.facade import Storage
from src.storage.models import ChatSessionModel


@pytest.fixture
async def storage(tmp_path):
    """Provide an initialised in-memory-like Storage backed by a temp file."""
    db_url = f"sqlite:///{tmp_path}/test.db"
    s = Storage(db_url)
    await s.initialize()
    yield s
    await s.close()


# ---------------------------------------------------------------------------
# Attribute presence / absence
# ---------------------------------------------------------------------------


def test_chat_sessions_attr_exists(storage):
    """Storage must expose chat_sessions repository."""
    assert hasattr(storage, "chat_sessions")


def test_removed_attrs_absent(storage):
    """Old repos (users, user_sessions, project_threads) must not exist."""
    assert not hasattr(storage, "users")
    assert not hasattr(storage, "user_sessions")
    assert not hasattr(storage, "project_threads")


# ---------------------------------------------------------------------------
# save_session / load_session round-trip
# ---------------------------------------------------------------------------


async def test_save_and_load_session(storage):
    """save_session followed by load_session returns the stored values."""
    await storage.save_session(
        chat_id=100,
        message_thread_id=0,
        user_id=42,
        directory="/projects/alpha",
        session_id="sess-abc",
        topic_name=None,
    )
    result = await storage.load_session(chat_id=100, message_thread_id=0)

    assert result is not None
    assert isinstance(result, ChatSessionModel)
    assert result.chat_id == 100
    assert result.message_thread_id == 0
    assert result.user_id == 42
    assert result.directory == "/projects/alpha"
    assert result.session_id == "sess-abc"
    assert result.is_active is True


async def test_load_session_missing_returns_none(storage):
    """load_session for a non-existent PK returns None."""
    result = await storage.load_session(chat_id=999, message_thread_id=999)
    assert result is None


async def test_save_session_upserts(storage):
    """Calling save_session twice updates the existing row."""
    await storage.save_session(
        chat_id=200,
        message_thread_id=1,
        user_id=7,
        directory="/projects/beta",
        session_id="sess-old",
    )
    await storage.save_session(
        chat_id=200,
        message_thread_id=1,
        user_id=7,
        directory="/projects/beta",
        session_id="sess-new",
    )
    result = await storage.load_session(chat_id=200, message_thread_id=1)
    assert result is not None
    assert result.session_id == "sess-new"


# ---------------------------------------------------------------------------
# clear_session
# ---------------------------------------------------------------------------


async def test_clear_session_removes_row(storage):
    """clear_session hard-deletes the row; subsequent load returns None."""
    await storage.save_session(
        chat_id=300,
        message_thread_id=2,
        user_id=5,
        directory="/projects/gamma",
    )
    # Confirm it exists first
    assert await storage.load_session(300, 2) is not None

    await storage.clear_session(chat_id=300, message_thread_id=2)

    assert await storage.load_session(300, 2) is None


async def test_clear_session_nonexistent_is_noop(storage):
    """clear_session on a missing row raises no error."""
    await storage.clear_session(chat_id=404, message_thread_id=0)  # should not raise


# ---------------------------------------------------------------------------
# log_bot_event
# ---------------------------------------------------------------------------


async def test_log_bot_event_does_not_raise(storage):
    """log_bot_event stores an audit entry without error."""
    await storage.log_bot_event(
        user_id=1,
        event_type="test_event",
        event_data={"key": "value"},
        success=True,
    )
    # Verify via audit repo
    entries = await storage.audit.get_user_audit_log(user_id=1, limit=10)
    assert len(entries) == 1
    assert entries[0].event_type == "test_event"
    assert entries[0].success  # SQLite returns 1 for TRUE
    assert entries[0].event_data == {"key": "value"}
