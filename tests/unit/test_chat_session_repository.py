"""Tests for ChatSessionRepository."""

import pytest

from src.storage.database import DatabaseManager
from src.storage.repositories import ChatSessionRepository


@pytest.fixture
async def repo(tmp_path):
    db_path = str(tmp_path / "test.db")
    db = DatabaseManager(f"sqlite:///{db_path}")
    await db.initialize()
    repo = ChatSessionRepository(db)
    yield repo
    await db.close()


@pytest.mark.asyncio
async def test_upsert_and_get(repo):
    await repo.upsert(
        chat_id=-1001,
        message_thread_id=42,
        user_id=100,
        directory="/proj",
        session_id="s1",
        topic_name="proj",
    )
    row = await repo.get(chat_id=-1001, message_thread_id=42)
    assert row is not None
    assert row.user_id == 100
    assert row.session_id == "s1"
    assert row.directory == "/proj"
    assert row.topic_name == "proj"
    assert row.is_active is True


@pytest.mark.asyncio
async def test_upsert_updates_session_id(repo):
    await repo.upsert(
        chat_id=100,
        message_thread_id=0,
        user_id=100,
        directory="/proj",
        session_id="s1",
    )
    await repo.upsert(
        chat_id=100,
        message_thread_id=0,
        user_id=100,
        directory="/proj",
        session_id="s2",
    )
    row = await repo.get(chat_id=100, message_thread_id=0)
    assert row is not None
    assert row.session_id == "s2"


@pytest.mark.asyncio
async def test_upsert_preserves_session_id_when_null(repo):
    """COALESCE keeps existing session_id when new value is None."""
    await repo.upsert(
        chat_id=100,
        message_thread_id=0,
        user_id=100,
        directory="/proj",
        session_id="s1",
    )
    await repo.upsert(
        chat_id=100,
        message_thread_id=0,
        user_id=100,
        directory="/proj",
        session_id=None,
    )
    row = await repo.get(chat_id=100, message_thread_id=0)
    assert row is not None
    assert row.session_id == "s1"


@pytest.mark.asyncio
async def test_deactivate(repo):
    await repo.upsert(
        chat_id=-1001,
        message_thread_id=42,
        user_id=100,
        directory="/proj",
        topic_name="proj",
    )
    count = await repo.deactivate(chat_id=-1001, message_thread_id=42)
    assert count == 1
    row = await repo.get(chat_id=-1001, message_thread_id=42)
    assert row is None  # get() only returns active rows


@pytest.mark.asyncio
async def test_delete(repo):
    await repo.upsert(chat_id=100, message_thread_id=0, user_id=100, directory="/proj")
    await repo.delete(chat_id=100, message_thread_id=0)
    row = await repo.get(chat_id=100, message_thread_id=0)
    assert row is None


@pytest.mark.asyncio
async def test_list_active_by_chat(repo):
    await repo.upsert(
        chat_id=-1001, message_thread_id=42, user_id=100, directory="/a", topic_name="a"
    )
    await repo.upsert(
        chat_id=-1001, message_thread_id=43, user_id=100, directory="/b", topic_name="b"
    )
    await repo.upsert(
        chat_id=-9999,
        message_thread_id=1,
        user_id=200,
        directory="/other",
        topic_name="other",
    )
    rows = await repo.list_active_by_chat(chat_id=-1001)
    assert len(rows) == 2
    assert rows[0].directory == "/a"
    assert rows[1].directory == "/b"


@pytest.mark.asyncio
async def test_list_by_user(repo):
    await repo.upsert(chat_id=100, message_thread_id=0, user_id=100, directory="/dm")
    await repo.upsert(
        chat_id=-1001,
        message_thread_id=42,
        user_id=100,
        directory="/proj",
        topic_name="proj",
    )
    await repo.upsert(
        chat_id=-1001,
        message_thread_id=43,
        user_id=200,
        directory="/other",
        topic_name="other",
    )
    rows = await repo.list_by_user(user_id=100)
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_get_by_user_directory(repo):
    """Private DM lookup by user+directory."""
    await repo.upsert(
        chat_id=100,
        message_thread_id=0,
        user_id=100,
        directory="/proj",
        session_id="s1",
    )
    row = await repo.get_by_user_directory(user_id=100, directory="/proj")
    assert row is not None
    assert row.session_id == "s1"
    assert row.message_thread_id == 0


@pytest.mark.asyncio
async def test_get_by_user_directory_ignores_group_topics(repo):
    """get_by_user_directory only matches message_thread_id=0 (private DM)."""
    await repo.upsert(
        chat_id=-1001,
        message_thread_id=42,
        user_id=100,
        directory="/proj",
        session_id="group-s",
        topic_name="proj",
    )
    row = await repo.get_by_user_directory(user_id=100, directory="/proj")
    assert row is None


@pytest.mark.asyncio
async def test_count_active_by_chat_directory(repo):
    await repo.upsert(
        chat_id=-1001,
        message_thread_id=42,
        user_id=100,
        directory="/proj",
        topic_name="proj",
    )
    await repo.upsert(
        chat_id=-1001,
        message_thread_id=43,
        user_id=100,
        directory="/proj",
        topic_name="proj (2)",
    )
    count = await repo.count_active_by_chat_directory(chat_id=-1001, directory="/proj")
    assert count == 2


@pytest.mark.asyncio
async def test_count_excludes_deactivated(repo):
    await repo.upsert(
        chat_id=-1001,
        message_thread_id=42,
        user_id=100,
        directory="/proj",
        topic_name="proj",
    )
    await repo.deactivate(chat_id=-1001, message_thread_id=42)
    count = await repo.count_active_by_chat_directory(chat_id=-1001, directory="/proj")
    assert count == 0
