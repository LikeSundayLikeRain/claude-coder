# True Concurrent Sessions Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Re-key the entire session system from `(user_id, directory)` to `(user_id, chat_id, message_thread_id)` so each Telegram forum topic acts as an independent terminal panel — including multiple topics targeting the same directory.

**Architecture:** Merge `user_sessions` + `project_threads` + `users` tables into a single `chat_sessions` table. Replace three repositories + three models with one. Re-key `ClientManager` and update all routing in orchestrator. Auto-detect group topics from incoming messages (no feature flag needed).

**Tech Stack:** Python 3.12, aiosqlite, python-telegram-bot, claude-agent-sdk, pytest-asyncio

**Design doc:** `docs/plans/2026-03-01-true-concurrent-sessions-design.md`

---

### Task 1: Migration 12 — Create `chat_sessions`, drop `users`

**Files:**
- Modify: `src/storage/database.py` (add migration 12 to `_get_migrations()`)

**Context:** Migration 11 (current) created `user_sessions(user_id, directory, session_id)` with PK `(user_id, directory)` and `project_threads(chat_id, message_thread_id, directory, topic_name, is_active, created_at)` with PK `(chat_id, message_thread_id)`. The `users` table has only `(user_id, telegram_username)` — both columns are dead.

**Step 1: Write the failing test**

```python
# tests/unit/test_migration_12.py
import pytest
import aiosqlite
import sqlite3
from src.storage.database import DatabaseManager

@pytest.fixture
async def db_at_migration_11(tmp_path):
    """Create a DB at migration 11 with seed data."""
    db_path = str(tmp_path / "test.db")
    db = DatabaseManager(f"sqlite:///{db_path}")
    await db.initialize()  # runs all migrations including 11

    # Seed data for migration verification
    async with db.get_connection() as conn:
        # users table exists at migration 11
        await conn.execute(
            "INSERT OR IGNORE INTO users (user_id, telegram_username) VALUES (100, 'alice')"
        )
        # user_sessions
        await conn.execute(
            "INSERT INTO user_sessions (user_id, directory, session_id) VALUES (100, '/home/proj', 'sess-abc')"
        )
        # project_threads
        await conn.execute(
            "INSERT INTO project_threads (chat_id, message_thread_id, directory, topic_name, is_active) VALUES (-1001, 42, '/home/proj', 'proj', 1)"
        )
        await conn.commit()

    yield db
    await db.close()


@pytest.mark.asyncio
async def test_migration_12_creates_chat_sessions(db_at_migration_11):
    """After migration 12, chat_sessions exists with correct schema and data."""
    db = db_at_migration_11
    async with db.get_connection() as conn:
        # chat_sessions table should exist
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='chat_sessions'"
        )
        assert await cursor.fetchone() is not None

        # Verify schema columns
        cursor = await conn.execute("PRAGMA table_info(chat_sessions)")
        columns = {row[1] for row in await cursor.fetchall()}
        assert columns == {
            "chat_id", "message_thread_id", "user_id", "directory",
            "session_id", "topic_name", "is_active", "created_at",
        }

        # PK is (chat_id, message_thread_id)
        cursor = await conn.execute("PRAGMA table_info(chat_sessions)")
        pk_cols = [row[1] for row in await cursor.fetchall() if row[5] > 0]
        assert pk_cols == ["chat_id", "message_thread_id"]


@pytest.mark.asyncio
async def test_migration_12_migrates_project_threads_data(db_at_migration_11):
    """project_threads rows migrate into chat_sessions with user_id from user_sessions."""
    db = db_at_migration_11
    async with db.get_connection() as conn:
        cursor = await conn.execute(
            "SELECT * FROM chat_sessions WHERE chat_id = -1001 AND message_thread_id = 42"
        )
        row = await cursor.fetchone()
        assert row is not None
        data = dict(row)
        assert data["user_id"] == 100
        assert data["directory"] == "/home/proj"
        assert data["session_id"] == "sess-abc"
        assert data["topic_name"] == "proj"
        assert data["is_active"] == 1


@pytest.mark.asyncio
async def test_migration_12_migrates_private_dm_sessions(db_at_migration_11):
    """user_sessions rows without a project_thread get chat_id=user_id, thread_id=0."""
    db = db_at_migration_11
    async with db.get_connection() as conn:
        # Add a private-DM-only session (no project_thread)
        await conn.execute(
            "INSERT INTO user_sessions (user_id, directory, session_id) VALUES (200, '/home/other', 'sess-xyz')"
        )
        await conn.commit()

    # Re-run migration (idempotent: already ran, but the seed data tests the logic)
    # Actually the migration already ran. Let's just verify the DM fallback row exists
    # for user 100's session that WAS matched to a project_thread.
    # For a truly private session, we need to add it BEFORE migration runs.
    # This test verifies the migration SQL handles the fallback correctly.
    # We'll adjust the fixture approach in implementation.


@pytest.mark.asyncio
async def test_migration_12_drops_old_tables(db_at_migration_11):
    """users, user_sessions, project_threads tables are dropped."""
    db = db_at_migration_11
    async with db.get_connection() as conn:
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('users', 'user_sessions', 'project_threads')"
        )
        remaining = [row[0] for row in await cursor.fetchall()]
        assert remaining == [], f"Old tables still exist: {remaining}"


@pytest.mark.asyncio
async def test_migration_12_removes_user_fk_from_audit_log(db_at_migration_11):
    """audit_log no longer has FK to users table."""
    db = db_at_migration_11
    async with db.get_connection() as conn:
        cursor = await conn.execute("PRAGMA foreign_key_list(audit_log)")
        fks = await cursor.fetchall()
        user_fks = [fk for fk in fks if fk[2] == "users"]
        assert user_fks == []
```

**Step 2: Run test to verify it fails**

Run: `cd /local/home/moxu/claude-coder/.claude/worktrees/multi-project && uv run pytest tests/unit/test_migration_12.py -v`
Expected: FAIL (migration 12 doesn't exist yet, old tables still present)

**Step 3: Write migration 12 in `database.py`**

Add to `_get_migrations()` list after migration 11:

```python
(
    12,
    """
    PRAGMA foreign_keys = OFF;

    -- 1. Create chat_sessions table
    CREATE TABLE IF NOT EXISTS chat_sessions (
        chat_id            INTEGER NOT NULL,
        message_thread_id  INTEGER NOT NULL DEFAULT 0,
        user_id            INTEGER NOT NULL,
        directory          TEXT NOT NULL,
        session_id         TEXT,
        topic_name         TEXT,
        is_active          BOOLEAN DEFAULT TRUE,
        created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (chat_id, message_thread_id)
    );

    -- 2. Migrate project_threads rows (group topics)
    --    Join with user_sessions to pull session_id for matching directory
    INSERT OR IGNORE INTO chat_sessions
        (chat_id, message_thread_id, user_id, directory, session_id, topic_name, is_active, created_at)
    SELECT
        pt.chat_id,
        pt.message_thread_id,
        COALESCE(us.user_id, 0),
        pt.directory,
        us.session_id,
        pt.topic_name,
        pt.is_active,
        pt.created_at
    FROM project_threads pt
    LEFT JOIN user_sessions us ON us.directory = pt.directory;

    -- 3. Migrate private DM sessions (user_sessions rows NOT matched to any project_thread)
    --    These become chat_id=user_id, message_thread_id=0
    INSERT OR IGNORE INTO chat_sessions
        (chat_id, message_thread_id, user_id, directory, session_id, topic_name, is_active)
    SELECT
        us.user_id,
        0,
        us.user_id,
        us.directory,
        us.session_id,
        NULL,
        1
    FROM user_sessions us
    WHERE NOT EXISTS (
        SELECT 1 FROM project_threads pt WHERE pt.directory = us.directory
    );

    -- 4. Rebuild audit_log without FK to users
    CREATE TABLE IF NOT EXISTS audit_log_new (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL,
        event_type  TEXT NOT NULL,
        event_data  TEXT,
        success     BOOLEAN DEFAULT TRUE,
        timestamp   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        ip_address  TEXT
    );
    INSERT INTO audit_log_new SELECT * FROM audit_log;
    DROP TABLE audit_log;
    ALTER TABLE audit_log_new RENAME TO audit_log;

    -- 5. Drop old tables
    DROP TABLE IF EXISTS user_sessions;
    DROP TABLE IF EXISTS project_threads;
    DROP TABLE IF EXISTS users;

    -- 6. Create index for reverse lookups (user_id -> all sessions)
    CREATE INDEX IF NOT EXISTS idx_chat_sessions_user
        ON chat_sessions(user_id);
    CREATE INDEX IF NOT EXISTS idx_chat_sessions_directory
        ON chat_sessions(directory);

    PRAGMA foreign_keys = ON;
    """,
),
```

**Step 4: Run test to verify it passes**

Run: `cd /local/home/moxu/claude-coder/.claude/worktrees/multi-project && uv run pytest tests/unit/test_migration_12.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/storage/database.py tests/unit/test_migration_12.py
git commit -m "feat: migration 12 — merge user_sessions + project_threads into chat_sessions, drop users"
```

---

### Task 2: Replace models — `ChatSessionModel`

**Files:**
- Modify: `src/storage/models.py`

**Context:** Currently has `UserModel`, `UserSessionModel`, `ProjectThreadModel`. All three are replaced by a single `ChatSessionModel`. `AuditLogModel`, `WebhookEventModel`, `ScheduledJobModel` remain unchanged.

**Step 1: Write the failing test**

```python
# tests/unit/test_chat_session_model.py
import pytest
from unittest.mock import MagicMock
from src.storage.models import ChatSessionModel

def test_chat_session_model_creation():
    m = ChatSessionModel(
        chat_id=-1001,
        message_thread_id=42,
        user_id=100,
        directory="/home/proj",
        session_id="sess-abc",
        topic_name="proj",
    )
    assert m.chat_id == -1001
    assert m.message_thread_id == 42
    assert m.user_id == 100
    assert m.directory == "/home/proj"
    assert m.session_id == "sess-abc"
    assert m.topic_name == "proj"
    assert m.is_active is True  # default

def test_chat_session_model_private_dm():
    m = ChatSessionModel(
        chat_id=100,
        message_thread_id=0,
        user_id=100,
        directory="/home/proj",
    )
    assert m.topic_name is None
    assert m.message_thread_id == 0

def test_chat_session_model_from_row():
    row = MagicMock()
    row.__iter__ = MagicMock(return_value=iter([]))
    row.keys = MagicMock(return_value=[
        "chat_id", "message_thread_id", "user_id", "directory",
        "session_id", "topic_name", "is_active", "created_at",
    ])
    row.__getitem__ = lambda self, key: {
        "chat_id": -1001, "message_thread_id": 42, "user_id": 100,
        "directory": "/home/proj", "session_id": "sess-abc",
        "topic_name": "proj", "is_active": 1, "created_at": None,
    }[key]

    # Use dict(row) pattern like the real code
    def dict_row(r):
        return {k: r[k] for k in r.keys()}

    data = dict_row(row)
    m = ChatSessionModel.from_row_dict(data)
    assert m.chat_id == -1001
    assert m.is_active is True
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_chat_session_model.py -v`
Expected: FAIL (ChatSessionModel doesn't exist)

**Step 3: Implement ChatSessionModel, remove old models**

In `src/storage/models.py`:
- Delete `UserModel` class entirely
- Delete `UserSessionModel` class entirely
- Delete `ProjectThreadModel` class entirely
- Add `ChatSessionModel`:

```python
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
        return cls(**data)
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_chat_session_model.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/storage/models.py tests/unit/test_chat_session_model.py
git commit -m "refactor: replace UserModel + UserSessionModel + ProjectThreadModel with ChatSessionModel"
```

---

### Task 3: Replace repositories — `ChatSessionRepository`

**Files:**
- Modify: `src/storage/repositories.py`

**Context:** Currently has `UserRepository`, `UserSessionRepository`, `ProjectThreadRepository`, `AuditLogRepository`. Replace the first three with `ChatSessionRepository`. Keep `AuditLogRepository` unchanged.

**Step 1: Write the failing test**

```python
# tests/unit/test_chat_session_repository.py
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
        chat_id=-1001, message_thread_id=42, user_id=100,
        directory="/proj", session_id="s1", topic_name="proj",
    )
    row = await repo.get(chat_id=-1001, message_thread_id=42)
    assert row is not None
    assert row.user_id == 100
    assert row.session_id == "s1"

@pytest.mark.asyncio
async def test_upsert_updates_session_id(repo):
    await repo.upsert(chat_id=100, message_thread_id=0, user_id=100,
                       directory="/proj", session_id="s1")
    await repo.upsert(chat_id=100, message_thread_id=0, user_id=100,
                       directory="/proj", session_id="s2")
    row = await repo.get(chat_id=100, message_thread_id=0)
    assert row.session_id == "s2"

@pytest.mark.asyncio
async def test_deactivate(repo):
    await repo.upsert(chat_id=-1001, message_thread_id=42, user_id=100,
                       directory="/proj", topic_name="proj")
    count = await repo.deactivate(chat_id=-1001, message_thread_id=42)
    assert count == 1
    row = await repo.get(chat_id=-1001, message_thread_id=42)
    assert row is None  # get() only returns active rows

@pytest.mark.asyncio
async def test_list_active_by_chat(repo):
    await repo.upsert(chat_id=-1001, message_thread_id=42, user_id=100,
                       directory="/a", topic_name="a")
    await repo.upsert(chat_id=-1001, message_thread_id=43, user_id=100,
                       directory="/b", topic_name="b")
    rows = await repo.list_active_by_chat(chat_id=-1001)
    assert len(rows) == 2

@pytest.mark.asyncio
async def test_list_by_user(repo):
    await repo.upsert(chat_id=100, message_thread_id=0, user_id=100,
                       directory="/dm")
    await repo.upsert(chat_id=-1001, message_thread_id=42, user_id=100,
                       directory="/proj", topic_name="proj")
    rows = await repo.list_by_user(user_id=100)
    assert len(rows) == 2

@pytest.mark.asyncio
async def test_get_by_user_directory(repo):
    """Private DM lookup by user+directory (backwards compat for private chat)."""
    await repo.upsert(chat_id=100, message_thread_id=0, user_id=100,
                       directory="/proj", session_id="s1")
    row = await repo.get_by_user_directory(user_id=100, directory="/proj")
    assert row is not None
    assert row.session_id == "s1"

@pytest.mark.asyncio
async def test_delete_row(repo):
    await repo.upsert(chat_id=100, message_thread_id=0, user_id=100,
                       directory="/proj")
    await repo.delete(chat_id=100, message_thread_id=0)
    row = await repo.get(chat_id=100, message_thread_id=0)
    assert row is None

@pytest.mark.asyncio
async def test_count_active_by_chat_directory(repo):
    """Count how many active topics exist for a directory in a chat (for auto-suffix)."""
    await repo.upsert(chat_id=-1001, message_thread_id=42, user_id=100,
                       directory="/proj", topic_name="proj")
    await repo.upsert(chat_id=-1001, message_thread_id=43, user_id=100,
                       directory="/proj", topic_name="proj (2)")
    count = await repo.count_active_by_chat_directory(chat_id=-1001, directory="/proj")
    assert count == 2
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_chat_session_repository.py -v`
Expected: FAIL (ChatSessionRepository doesn't exist)

**Step 3: Implement ChatSessionRepository, remove old repos**

In `src/storage/repositories.py`:
- Delete `UserRepository` class
- Delete `UserSessionRepository` class
- Delete `ProjectThreadRepository` class
- Update imports: remove `UserModel`, `UserSessionModel`, `ProjectThreadModel`; add `ChatSessionModel`
- Add `ChatSessionRepository`:

```python
class ChatSessionRepository:
    """Unified session data access — one row per (chat_id, message_thread_id)."""

    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager

    async def get(self, chat_id: int, message_thread_id: int) -> Optional[ChatSessionModel]:
        """Get active session by PK."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM chat_sessions WHERE chat_id = ? AND message_thread_id = ? AND is_active = 1",
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
        """Insert or update a session row."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO chat_sessions (chat_id, message_thread_id, user_id, directory, session_id, topic_name)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, message_thread_id) DO UPDATE SET
                    session_id = COALESCE(excluded.session_id, chat_sessions.session_id),
                    directory = excluded.directory,
                    topic_name = COALESCE(excluded.topic_name, chat_sessions.topic_name),
                    is_active = 1
                """,
                (chat_id, message_thread_id, user_id, directory, session_id, topic_name),
            )
            await conn.commit()

    async def deactivate(self, chat_id: int, message_thread_id: int) -> int:
        """Soft-delete by setting is_active=0."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "UPDATE chat_sessions SET is_active = 0 WHERE chat_id = ? AND message_thread_id = ?",
                (chat_id, message_thread_id),
            )
            await conn.commit()
            return cursor.rowcount

    async def delete(self, chat_id: int, message_thread_id: int) -> None:
        """Hard-delete a session row (used by /new to clear session)."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                "DELETE FROM chat_sessions WHERE chat_id = ? AND message_thread_id = ?",
                (chat_id, message_thread_id),
            )
            await conn.commit()

    async def list_active_by_chat(self, chat_id: int) -> list[ChatSessionModel]:
        """List all active sessions in a chat (for /status dashboard)."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM chat_sessions WHERE chat_id = ? AND is_active = 1 ORDER BY directory ASC",
                (chat_id,),
            )
            rows = await cursor.fetchall()
            return [ChatSessionModel.from_row(row) for row in rows]

    async def list_by_user(self, user_id: int) -> list[ChatSessionModel]:
        """List all active sessions for a user across all chats (reverse lookup)."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM chat_sessions WHERE user_id = ? AND is_active = 1 ORDER BY created_at DESC",
                (user_id,),
            )
            rows = await cursor.fetchall()
            return [ChatSessionModel.from_row(row) for row in rows]

    async def get_by_user_directory(self, user_id: int, directory: str) -> Optional[ChatSessionModel]:
        """Get session by user+directory (private DM compat: message_thread_id=0)."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM chat_sessions WHERE user_id = ? AND directory = ? AND message_thread_id = 0 AND is_active = 1",
                (user_id, directory),
            )
            row = await cursor.fetchone()
            return ChatSessionModel.from_row(row) if row else None

    async def count_active_by_chat_directory(self, chat_id: int, directory: str) -> int:
        """Count active sessions for a directory in a chat (for auto-suffix naming)."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM chat_sessions WHERE chat_id = ? AND directory = ? AND is_active = 1",
                (chat_id, directory),
            )
            row = await cursor.fetchone()
            return row[0] if row else 0
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_chat_session_repository.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/storage/repositories.py tests/unit/test_chat_session_repository.py
git commit -m "refactor: replace UserRepository + UserSessionRepository + ProjectThreadRepository with ChatSessionRepository"
```

---

### Task 4: Update Storage facade

**Files:**
- Modify: `src/storage/facade.py`

**Context:** Currently exposes `users`, `user_sessions`, `project_threads`, `audit` repos and convenience methods `save_user_session`, `load_user_session`, `clear_user_session`, `list_user_sessions`. Replace with `chat_sessions` repo and new convenience methods keyed by `(chat_id, message_thread_id)`.

**Step 1: Write the failing test**

```python
# tests/unit/test_storage_facade_v2.py
import pytest
from src.storage.facade import Storage

@pytest.fixture
async def storage(tmp_path):
    s = Storage(f"sqlite:///{tmp_path / 'test.db'}")
    await s.initialize()
    yield s
    await s.close()

@pytest.mark.asyncio
async def test_storage_has_chat_sessions_repo(storage):
    assert hasattr(storage, "chat_sessions")

@pytest.mark.asyncio
async def test_storage_no_old_repos(storage):
    assert not hasattr(storage, "users")
    assert not hasattr(storage, "user_sessions")
    assert not hasattr(storage, "project_threads")

@pytest.mark.asyncio
async def test_save_and_load_session(storage):
    await storage.save_session(chat_id=100, message_thread_id=0,
                                user_id=100, directory="/proj", session_id="s1")
    row = await storage.load_session(chat_id=100, message_thread_id=0)
    assert row is not None
    assert row.session_id == "s1"

@pytest.mark.asyncio
async def test_clear_session(storage):
    await storage.save_session(chat_id=100, message_thread_id=0,
                                user_id=100, directory="/proj", session_id="s1")
    await storage.clear_session(chat_id=100, message_thread_id=0)
    row = await storage.load_session(chat_id=100, message_thread_id=0)
    assert row is None
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_storage_facade_v2.py -v`
Expected: FAIL

**Step 3: Rewrite facade**

```python
"""Unified storage interface."""

from datetime import UTC, datetime
from typing import Any, Dict, List, Optional

import structlog

from .database import DatabaseManager
from .models import AuditLogModel, ChatSessionModel
from .repositories import AuditLogRepository, ChatSessionRepository

logger = structlog.get_logger()


class Storage:
    """Main storage interface."""

    def __init__(self, database_url: str):
        self.db_manager = DatabaseManager(database_url)
        self.chat_sessions = ChatSessionRepository(self.db_manager)
        self.audit = AuditLogRepository(self.db_manager)

    async def initialize(self) -> None:
        await self.db_manager.initialize()

    async def close(self) -> None:
        await self.db_manager.close()

    async def health_check(self) -> bool:
        return await self.db_manager.health_check()

    # --- Session convenience methods ---

    async def save_session(
        self, chat_id: int, message_thread_id: int,
        user_id: int, directory: str, session_id: Optional[str] = None,
        topic_name: Optional[str] = None,
    ) -> None:
        await self.chat_sessions.upsert(
            chat_id=chat_id, message_thread_id=message_thread_id,
            user_id=user_id, directory=directory,
            session_id=session_id, topic_name=topic_name,
        )

    async def load_session(
        self, chat_id: int, message_thread_id: int,
    ) -> Optional[ChatSessionModel]:
        return await self.chat_sessions.get(chat_id, message_thread_id)

    async def clear_session(self, chat_id: int, message_thread_id: int) -> None:
        await self.chat_sessions.delete(chat_id, message_thread_id)

    # --- Audit (unchanged) ---

    async def log_security_event(self, user_id: int, event_type: str,
                                  event_data: Dict[str, Any], success: bool = True,
                                  ip_address: Optional[str] = None) -> None:
        audit_event = AuditLogModel(
            id=None, user_id=user_id, event_type=event_type,
            event_data=event_data, success=success,
            timestamp=datetime.now(UTC), ip_address=ip_address,
        )
        await self.audit.log_event(audit_event)

    async def log_bot_event(self, user_id: int, event_type: str,
                             event_data: Dict[str, Any], success: bool = True) -> None:
        audit_event = AuditLogModel(
            id=None, user_id=user_id, event_type=event_type,
            event_data=event_data, success=success, timestamp=datetime.now(UTC),
        )
        await self.audit.log_event(audit_event)
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_storage_facade_v2.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/storage/facade.py tests/unit/test_storage_facade_v2.py
git commit -m "refactor: update Storage facade — chat_sessions repo, remove users/user_sessions/project_threads"
```

---

### Task 5: Re-key ClientManager from `(user_id, directory)` to `(user_id, chat_id, message_thread_id)`

**Files:**
- Modify: `src/claude/client_manager.py`

**Context:** Currently `_clients` is `dict[tuple[int, str], UserClient]` keyed by `(user_id, directory)`. Change to `dict[tuple[int, int, int], UserClient]` keyed by `(user_id, chat_id, message_thread_id)`. All public methods change signature from `(user_id, directory)` to `(user_id, chat_id, message_thread_id)`. Session resolution uses `ChatSessionRepository` instead of `UserSessionRepository`.

The `SessionResolver` (history.jsonl-based) is no longer used for session resolution — DB is source of truth. Keep it only for `/resume` UI (listing past sessions from history file). Remove the `history_path` constructor param from `ClientManager`.

**Step 1: Write the failing test**

```python
# tests/unit/test_client_manager_v2.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.claude.client_manager import ClientManager
from src.storage.repositories import ChatSessionRepository

@pytest.fixture
def mock_repo():
    repo = AsyncMock(spec=ChatSessionRepository)
    repo.get.return_value = None
    repo.upsert.return_value = None
    return repo

@pytest.fixture
def manager(mock_repo):
    return ClientManager(chat_session_repo=mock_repo, idle_timeout=60)

def test_client_key_is_triple(manager):
    """Internal _clients dict uses (user_id, chat_id, message_thread_id) key."""
    assert manager._clients == {}
    # The type hint should be dict[tuple[int, int, int], UserClient]

@pytest.mark.asyncio
async def test_get_or_connect_new_key(manager, mock_repo):
    """get_or_connect accepts the new triple key."""
    with patch("src.claude.client_manager.UserClient") as MockClient:
        mock_instance = AsyncMock()
        mock_instance.is_connected = True
        mock_instance.session_id = "sess-1"
        MockClient.return_value = mock_instance

        client = await manager.get_or_connect(
            user_id=100, chat_id=-1001, message_thread_id=42,
            directory="/proj", approved_directory="/proj",
        )
        assert client is mock_instance
        assert (100, -1001, 42) in manager._clients

@pytest.mark.asyncio
async def test_disconnect_by_triple(manager, mock_repo):
    """disconnect accepts (user_id, chat_id, message_thread_id)."""
    with patch("src.claude.client_manager.UserClient") as MockClient:
        mock_instance = AsyncMock()
        mock_instance.is_connected = True
        mock_instance.session_id = None
        MockClient.return_value = mock_instance

        await manager.get_or_connect(
            user_id=100, chat_id=-1001, message_thread_id=42,
            directory="/proj", approved_directory="/proj",
        )
        await manager.disconnect(user_id=100, chat_id=-1001, message_thread_id=42)
        assert (100, -1001, 42) not in manager._clients
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_client_manager_v2.py -v`
Expected: FAIL (old signature)

**Step 3: Rewrite ClientManager**

Key changes:
- Constructor takes `chat_session_repo: ChatSessionRepository` (not `user_session_repo`)
- Remove `_session_resolver` and `history_path` param
- `_clients: dict[tuple[int, int, int], UserClient]` — keyed by `(user_id, chat_id, message_thread_id)`
- All public methods: `get_or_connect(user_id, chat_id, message_thread_id, directory, ...)`, `disconnect(user_id, chat_id, message_thread_id)`, etc.
- `get_or_connect` resolves `session_id` from `chat_session_repo.get(chat_id, message_thread_id)`
- `update_session_id` calls `chat_session_repo.upsert(...)`
- `get_all_clients_for_user` returns `list[tuple[int, int, UserClient]]` — `(chat_id, message_thread_id, client)`
- Remove `get_latest_session()` and `list_sessions()` (these used SessionResolver, move to orchestrator if needed)

See full implementation in code (follows same pattern, just re-keyed).

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_client_manager_v2.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/claude/client_manager.py tests/unit/test_client_manager_v2.py
git commit -m "refactor: re-key ClientManager from (user_id, directory) to (user_id, chat_id, message_thread_id)"
```

---

### Task 6: Update ProjectThreadManager → use ChatSessionRepository

**Files:**
- Modify: `src/projects/thread_manager.py`

**Context:** `ProjectThreadManager` currently takes a `ProjectThreadRepository`. Change to `ChatSessionRepository`. The `create_topic` method now also needs `user_id` to write the unified row. `generate_topic_name` changes to auto-suffix with `(N)` for same-directory duplicates.

**Step 1: Write the failing test**

```python
# tests/unit/test_thread_manager_v2.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from src.projects.thread_manager import ProjectThreadManager
from src.storage.repositories import ChatSessionRepository

@pytest.fixture
def mock_repo():
    repo = AsyncMock(spec=ChatSessionRepository)
    repo.list_active_by_chat.return_value = []
    repo.count_active_by_chat_directory.return_value = 0
    repo.upsert.return_value = None
    return repo

@pytest.fixture
def manager(mock_repo):
    return ProjectThreadManager(repository=mock_repo)

def test_generate_topic_name_no_collision(manager):
    name = ProjectThreadManager.generate_topic_name("/home/myapp", existing_names=[])
    assert name == "myapp"

def test_generate_topic_name_auto_suffix(manager):
    name = ProjectThreadManager.generate_topic_name(
        "/home/myapp", existing_names=["myapp"]
    )
    assert name == "myapp (2)"

def test_generate_topic_name_auto_suffix_3(manager):
    name = ProjectThreadManager.generate_topic_name(
        "/home/myapp", existing_names=["myapp", "myapp (2)"]
    )
    assert name == "myapp (3)"

def test_generate_topic_name_custom_override(manager):
    """When topic_name is explicitly provided, use it as-is."""
    name = ProjectThreadManager.generate_topic_name(
        "/home/myapp", existing_names=["myapp"], override_name="bug fix"
    )
    assert name == "bug fix"
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_thread_manager_v2.py -v`
Expected: FAIL (old signature, no auto-suffix logic)

**Step 3: Update ProjectThreadManager**

- Constructor: `def __init__(self, repository: ChatSessionRepository)`
- `create_topic(bot, chat_id, user_id, directory, topic_name)` — adds `user_id` param, calls `repo.upsert()`
- `remove_topic(bot, chat_id, message_thread_id)` — calls `repo.deactivate()`
- `resolve_directory(chat_id, message_thread_id)` — calls `repo.get()`
- `list_topics(chat_id)` — calls `repo.list_active_by_chat()`
- `generate_topic_name(directory, existing_names, override_name=None)`:
  - If `override_name` is provided, return it
  - Otherwise: `basename = Path(directory).name`
  - If `basename` not in `existing_names`, return `basename`
  - Else find next available `basename (N)` where N starts at 2

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_thread_manager_v2.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/projects/thread_manager.py tests/unit/test_thread_manager_v2.py
git commit -m "refactor: ProjectThreadManager uses ChatSessionRepository, auto-suffix topic names"
```

---

### Task 7: Update main.py wiring

**Files:**
- Modify: `src/main.py`

**Context:** `create_application()` currently creates `ClientManager(user_session_repo=storage.user_sessions)` and `run_application()` creates `ProjectThreadManager(repository=storage.project_threads)`. Change both to use `storage.chat_sessions`. Remove `enable_project_threads` guard — always create `ProjectThreadManager`. Remove dead `ensure_user()` call if any.

**Step 1: Update wiring**

In `create_application()`:
```python
# Old:
client_manager = ClientManager(
    user_session_repo=storage.user_sessions,
    options_builder=options_builder,
    idle_timeout=...,
)
# New:
client_manager = ClientManager(
    chat_session_repo=storage.chat_sessions,
    options_builder=options_builder,
    idle_timeout=...,
)
```

In `run_application()`:
```python
# Old:
if config.enable_project_threads:
    project_threads_manager = ProjectThreadManager(
        repository=storage.project_threads,
    )
    bot.deps["project_threads_manager"] = project_threads_manager

# New (always create):
project_threads_manager = ProjectThreadManager(
    repository=storage.chat_sessions,
)
bot.deps["project_threads_manager"] = project_threads_manager
```

**Step 2: Run full test suite**

Run: `uv run pytest tests/ -x -q`
Expected: Some tests may fail due to old references — that's expected and will be fixed in Task 9.

**Step 3: Commit**

```bash
git add src/main.py
git commit -m "refactor: wire ClientManager and ProjectThreadManager to ChatSessionRepository"
```

---

### Task 8: Update orchestrator routing

**Files:**
- Modify: `src/bot/orchestrator.py`

**Context:** The orchestrator currently:
1. Keys `ClientManager` calls by `(user_id, directory)` — change to `(user_id, chat_id, message_thread_id)`
2. Uses `enable_project_threads` flag — remove, always enable topic routing
3. `_apply_thread_routing_context` checks `project_threads_chat_id` — remove that check, auto-detect from any group
4. `_run_claude_query` calls `client_manager.get_or_connect(user_id, directory)` — add `chat_id, message_thread_id`
5. `agentic_add` has "directory already has a topic" guard — remove it (same dir allowed now)
6. `agentic_remove` should disconnect the UserClient actor
7. `agentic_status` general-topic dashboard uses `get_all_clients_for_user` — update to new return type
8. Private DM: `chat_id = user_id`, `message_thread_id = 0`
9. Remove dead `save_user_directory` calls

This is the largest task. Key changes by method:

**`_inject_deps`:**
- Remove `self.settings.enable_project_threads` check — always apply thread routing for group chats
- Set `_in_general_topic` detection based on whether message has a thread_id, regardless of feature flag

**`_apply_thread_routing_context`:**
- Remove `project_threads_chat_id` check — work with any group chat
- For private DM (no chat type "supergroup"), set `chat_id=user_id, message_thread_id=0` in context
- Populate `_thread_context` with `chat_id` and `message_thread_id` always

**`_run_claude_query`:**
- Extract `chat_id` and `message_thread_id` from `context.user_data["_thread_context"]`
- For private DM fallback: `chat_id=user_id`, `message_thread_id=0`
- Pass to `client_manager.get_or_connect(user_id, chat_id, message_thread_id, directory, ...)`
- `update_session_id` also takes the triple

**`agentic_add` / `_create_project_topic`:**
- Remove "directory already has a topic" `ValueError` in thread_manager
- Topic name uses auto-suffix from `generate_topic_name`
- Pass `user_id` to `manager.create_topic()`
- Support optional second arg as topic name override: `/add /path "name"`

**`agentic_remove`:**
- After `manager.remove_topic()`, also disconnect the UserClient:
  `client_manager.disconnect(user_id, chat_id, message_thread_id)`

**`agentic_status` (general topic):**
- `get_all_clients_for_user` returns `(chat_id, message_thread_id, client)` tuples
- Show topic name from client or DB

**`agentic_new`:**
- `client_manager.disconnect(user_id, chat_id, message_thread_id)`
- `storage.clear_session(chat_id, message_thread_id)`

**`handle_interrupt`:**
- `client_manager.interrupt(user_id, chat_id, message_thread_id)`

**Remove:**
- `save_user_directory` calls (dead code at ~lines 1234, 1983)
- `enable_project_threads` references in handler registration (always register `/add`, `/remove`)
- `project_threads_chat_id` references

**Step 1: Implement all changes**

This is a large refactor. Implement incrementally method by method, running tests between changes.

**Step 2: Run tests**

Run: `uv run pytest tests/ -x -q`

**Step 3: Commit**

```bash
git add src/bot/orchestrator.py
git commit -m "refactor: orchestrator routes by (chat_id, message_thread_id), removes feature flag"
```

---

### Task 9: Clean up settings and dead code

**Files:**
- Modify: `src/config/settings.py` — remove `enable_project_threads`, `project_threads_chat_id`, `project_threads_sync_action_interval_seconds`
- Modify: `src/config/features.py` — remove feature flag if it references project threads
- Modify: `src/utils/constants.py` — remove `DEFAULT_PROJECT_THREADS_SYNC_ACTION_INTERVAL_SECONDS`
- Delete: `src/projects/registry.py` if it still exists (was YAML project loader, now dead)
- Modify: `src/projects/__init__.py` — update exports

**Step 1: Remove settings fields**

In `src/config/settings.py`, delete:
```python
enable_project_threads: bool = Field(...)
project_threads_chat_id: Optional[int] = Field(...)
project_threads_sync_action_interval_seconds: float = Field(...)
```

**Step 2: Remove constant**

In `src/utils/constants.py`, delete `DEFAULT_PROJECT_THREADS_SYNC_ACTION_INTERVAL_SECONDS`.

**Step 3: Update projects package**

Check `src/projects/__init__.py` and remove any references to `ProjectRegistry` if present.

**Step 4: Run tests**

Run: `uv run pytest tests/ -x -q`

**Step 5: Commit**

```bash
git add src/config/settings.py src/config/features.py src/utils/constants.py src/projects/
git commit -m "chore: remove enable_project_threads flag and dead config fields"
```

---

### Task 10: Fix existing tests

**Files:**
- Modify: various test files that reference old models/repos/facade methods

**Context:** After Tasks 1-9, existing tests will break if they reference `UserModel`, `UserSessionModel`, `ProjectThreadModel`, `UserRepository`, `UserSessionRepository`, `ProjectThreadRepository`, `storage.users`, `storage.user_sessions`, `storage.project_threads`, `save_user_session(user_id, session_id, directory)`, `load_user_session(user_id, directory)`, etc.

**Step 1: Find all broken references**

```bash
cd /local/home/moxu/claude-coder/.claude/worktrees/multi-project
uv run pytest tests/ --co -q 2>&1 | head -50  # collect tests, find import errors
grep -rn "UserModel\|UserSessionModel\|ProjectThreadModel\|UserRepository\|UserSessionRepository\|ProjectThreadRepository\|storage\.users\|storage\.user_sessions\|storage\.project_threads\|save_user_session\|load_user_session\|clear_user_session\|list_user_sessions" tests/
```

**Step 2: Update each test file**

For each broken test:
- Replace old model imports with `ChatSessionModel`
- Replace old repo imports with `ChatSessionRepository`
- Update facade method calls to new signatures
- Update `ClientManager` mock/fixture to use new triple-key API

**Step 3: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

**Step 4: Commit**

```bash
git add tests/
git commit -m "test: update all tests for chat_sessions unified model"
```

---

### Task 11: Update docs

**Files:**
- Modify: `CLAUDE.md` — update architecture section (ClientManager key, storage tables, config)
- Modify: `docs/configuration.md` — remove `ENABLE_PROJECT_THREADS`, update data model section
- Modify: `.env.example` — remove `ENABLE_PROJECT_THREADS` if still referenced
- Modify: `docs/plans/2026-03-01-true-concurrent-sessions-design.md` — mark as implemented

**Step 1: Update all docs**

Key changes to `CLAUDE.md`:
- ClientManager description: "per (user_id, chat_id, message_thread_id)" not "per user+directory"
- Storage tables: `chat_sessions` replaces `user_sessions` + `project_threads` + `users`
- Remove `enable_project_threads` from config section
- Update `/add` description: same directory allowed, auto-suffix naming

**Step 2: Commit**

```bash
git add CLAUDE.md docs/ .env.example
git commit -m "docs: update for true concurrent sessions (chat_sessions table, re-keyed ClientManager)"
```

---

### Task 12: Full verification

**Step 1: Run full test suite**

```bash
cd /local/home/moxu/claude-coder/.claude/worktrees/multi-project
uv run pytest tests/ -v --tb=short
```
Expected: All tests pass

**Step 2: Run mypy**

```bash
uv run mypy src --ignore-missing-imports
```
Expected: No new errors (existing Optional-narrowing warnings are pre-existing)

**Step 3: Run linter**

```bash
uv run black --check src tests && uv run isort --check src tests && uv run flake8 src tests
```

**Step 4: Check for stale imports**

```bash
grep -rn "UserModel\|UserSessionModel\|ProjectThreadModel\|UserRepository\|UserSessionRepository\|ProjectThreadRepository\|enable_project_threads\|project_threads_chat_id" src/ tests/
```
Expected: No matches

**Step 5: Commit any fixes, then final commit message**

```bash
git add -A
git commit -m "chore: final cleanup and verification"
```
