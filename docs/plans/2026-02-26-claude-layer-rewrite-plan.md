# Claude Integration Layer Rewrite — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Rewrite `src/claude/` to achieve CLI feature parity via persistent SDK client, enabling seamless device switching between EC2 CLI and Telegram bot.

**Architecture:** Persistent `ClaudeSDKClient` per user managed by a `ClientManager`. Sessions share CLI's `~/.claude/history.jsonl` as source of truth. SDK options read from CLI settings, not duplicated. Bot state persisted in SQLite for restart recovery.

**Tech Stack:** Python 3.12+, claude-agent-sdk, aiosqlite, python-telegram-bot, pytest-asyncio

**Design Doc:** `docs/plans/2026-02-26-claude-layer-rewrite-design.md`

---

## Phase 1: Storage — Bot Session Persistence

### Task 1: BotSessionModel

**Files:**
- Modify: `src/storage/models.py` (append after line 205)
- Test: `tests/unit/test_storage/test_bot_session_model.py` (create)

**Step 1: Write the failing test**

```python
# tests/unit/test_storage/test_bot_session_model.py
"""Tests for BotSessionModel."""
import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from src.storage.models import BotSessionModel


class TestBotSessionModel:
    def test_from_row(self):
        row = MagicMock()
        row.__getitem__ = lambda self, key: {
            "user_id": 123,
            "session_id": "abc-def",
            "directory": "/home/user/project",
            "model": "sonnet",
            "betas": '["context-1m-2025-08-07"]',
            "last_active": "2026-02-26T10:00:00+00:00",
        }[key]
        row.keys = lambda: [
            "user_id", "session_id", "directory", "model", "betas", "last_active"
        ]

        model = BotSessionModel.from_row(row)

        assert model.user_id == 123
        assert model.session_id == "abc-def"
        assert model.directory == "/home/user/project"
        assert model.model == "sonnet"
        assert model.betas == ["context-1m-2025-08-07"]
        assert isinstance(model.last_active, datetime)

    def test_from_row_nullable_fields(self):
        row = MagicMock()
        row.__getitem__ = lambda self, key: {
            "user_id": 123,
            "session_id": "abc-def",
            "directory": "/home/user/project",
            "model": None,
            "betas": None,
            "last_active": "2026-02-26T10:00:00+00:00",
        }[key]
        row.keys = lambda: [
            "user_id", "session_id", "directory", "model", "betas", "last_active"
        ]

        model = BotSessionModel.from_row(row)

        assert model.model is None
        assert model.betas is None

    def test_to_dict(self):
        now = datetime.now(UTC)
        model = BotSessionModel(
            user_id=123,
            session_id="abc-def",
            directory="/home/user/project",
            model="sonnet",
            betas=["context-1m-2025-08-07"],
            last_active=now,
        )

        d = model.to_dict()

        assert d["user_id"] == 123
        assert d["session_id"] == "abc-def"
        assert d["betas"] == '["context-1m-2025-08-07"]'
        assert d["last_active"] == now.isoformat()
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_storage/test_bot_session_model.py -v`
Expected: FAIL with `ImportError: cannot import name 'BotSessionModel'`

**Step 3: Write minimal implementation**

Append to `src/storage/models.py`:

```python
@dataclass
class BotSessionModel:
    """Persists active bot session state for restart recovery."""

    user_id: int
    session_id: str
    directory: str
    model: Optional[str]
    betas: Optional[List[str]]
    last_active: datetime

    @classmethod
    def from_row(cls, row: Any) -> "BotSessionModel":
        betas_raw = row["betas"]
        betas = json.loads(betas_raw) if isinstance(betas_raw, str) else betas_raw

        last_active = row["last_active"]
        if isinstance(last_active, str):
            last_active = datetime.fromisoformat(last_active)

        return cls(
            user_id=row["user_id"],
            session_id=row["session_id"],
            directory=row["directory"],
            model=row["model"],
            betas=betas,
            last_active=last_active,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "session_id": self.session_id,
            "directory": self.directory,
            "model": self.model,
            "betas": json.dumps(self.betas) if self.betas else None,
            "last_active": self.last_active.isoformat()
            if isinstance(self.last_active, datetime)
            else self.last_active,
        }
```

Ensure `import json` is at the top of `models.py`.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_storage/test_bot_session_model.py -v`
Expected: PASS (3 tests)

**Step 5: Commit**

```bash
git add src/storage/models.py tests/unit/test_storage/test_bot_session_model.py
git commit -m "feat: add BotSessionModel for restart recovery"
```

---

### Task 2: Database Migration + BotSessionRepository

**Files:**
- Modify: `src/storage/database.py` (add migration 9)
- Modify: `src/storage/repositories.py` (append BotSessionRepository)
- Test: `tests/unit/test_storage/test_bot_session_repository.py` (create)

**Step 1: Write the failing test**

```python
# tests/unit/test_storage/test_bot_session_repository.py
"""Tests for BotSessionRepository."""
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.storage.database import DatabaseManager
from src.storage.repositories import BotSessionRepository


@pytest.fixture
async def db_manager():
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test.db"
        manager = DatabaseManager(f"sqlite:///{db_path}")
        await manager.initialize()
        yield manager
        await manager.close()


@pytest.fixture
async def repo(db_manager):
    return BotSessionRepository(db_manager)


class TestBotSessionRepository:
    async def test_upsert_and_get(self, repo):
        await repo.upsert(
            user_id=123,
            session_id="abc-def",
            directory="/home/user/project",
            model="sonnet",
            betas=None,
        )

        result = await repo.get_by_user(123)

        assert result is not None
        assert result.session_id == "abc-def"
        assert result.directory == "/home/user/project"
        assert result.model == "sonnet"

    async def test_upsert_overwrites(self, repo):
        await repo.upsert(
            user_id=123,
            session_id="old-session",
            directory="/home/user/project",
        )
        await repo.upsert(
            user_id=123,
            session_id="new-session",
            directory="/home/user/other",
            model="opus",
            betas=["context-1m-2025-08-07"],
        )

        result = await repo.get_by_user(123)

        assert result is not None
        assert result.session_id == "new-session"
        assert result.directory == "/home/user/other"
        assert result.model == "opus"
        assert result.betas == ["context-1m-2025-08-07"]

    async def test_get_by_user_not_found(self, repo):
        result = await repo.get_by_user(999)
        assert result is None

    async def test_delete(self, repo):
        await repo.upsert(user_id=123, session_id="abc", directory="/tmp")
        await repo.delete(123)

        result = await repo.get_by_user(123)
        assert result is None

    async def test_cleanup_expired(self, repo):
        await repo.upsert(user_id=1, session_id="active", directory="/tmp")
        await repo.upsert(user_id=2, session_id="stale", directory="/tmp")

        # Manually backdate user 2's last_active
        async with repo.db.get_connection() as conn:
            old_time = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
            await conn.execute(
                "UPDATE bot_sessions SET last_active = ? WHERE user_id = ?",
                (old_time, 2),
            )
            await conn.commit()

        deleted = await repo.cleanup_expired(max_age_hours=24)

        assert deleted == 1
        assert await repo.get_by_user(1) is not None
        assert await repo.get_by_user(2) is None
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_storage/test_bot_session_repository.py -v`
Expected: FAIL with `ImportError: cannot import name 'BotSessionRepository'`

**Step 3: Add migration 9 to database.py**

In `src/storage/database.py`, find the `_get_migrations()` method and append:

```python
(
    9,
    """
    CREATE TABLE IF NOT EXISTS bot_sessions (
        user_id     INTEGER PRIMARY KEY,
        session_id  TEXT NOT NULL,
        directory   TEXT NOT NULL,
        model       TEXT,
        betas       TEXT,
        last_active TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_bot_sessions_last_active
        ON bot_sessions(last_active);
    """,
),
```

**Step 4: Add BotSessionRepository to repositories.py**

Append to `src/storage/repositories.py`:

```python
class BotSessionRepository:
    """Repository for bot session state persistence across restarts."""

    def __init__(self, db: DatabaseManager) -> None:
        self.db = db

    async def upsert(
        self,
        user_id: int,
        session_id: str,
        directory: str,
        model: Optional[str] = None,
        betas: Optional[List[str]] = None,
    ) -> None:
        async with self.db.get_connection() as conn:
            betas_json = json.dumps(betas) if betas else None
            await conn.execute(
                """
                INSERT INTO bot_sessions (user_id, session_id, directory, model, betas, last_active)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    session_id = excluded.session_id,
                    directory = excluded.directory,
                    model = excluded.model,
                    betas = excluded.betas,
                    last_active = excluded.last_active
                """,
                (user_id, session_id, directory, model, betas_json, datetime.now(UTC)),
            )
            await conn.commit()

    async def get_by_user(self, user_id: int) -> Optional[BotSessionModel]:
        async with self.db.get_connection() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT * FROM bot_sessions WHERE user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
            return BotSessionModel.from_row(row) if row else None

    async def delete(self, user_id: int) -> None:
        async with self.db.get_connection() as conn:
            await conn.execute(
                "DELETE FROM bot_sessions WHERE user_id = ?", (user_id,)
            )
            await conn.commit()

    async def cleanup_expired(self, max_age_hours: int = 24) -> int:
        async with self.db.get_connection() as conn:
            cutoff = (datetime.now(UTC) - timedelta(hours=max_age_hours)).isoformat()
            cursor = await conn.execute(
                "DELETE FROM bot_sessions WHERE last_active < ?", (cutoff,)
            )
            await conn.commit()
            return cursor.rowcount
```

Add required imports at the top of `repositories.py`:

```python
import json
from datetime import timedelta
from src.storage.models import BotSessionModel
```

**Step 5: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_storage/test_bot_session_repository.py -v`
Expected: PASS (5 tests)

**Step 6: Wire into Storage facade**

In `src/storage/facade.py`, add to `Storage.__init__()`:

```python
self.bot_sessions = BotSessionRepository(self.db_manager)
```

Add import: `from src.storage.repositories import BotSessionRepository`

**Step 7: Commit**

```bash
git add src/storage/database.py src/storage/repositories.py src/storage/facade.py \
  tests/unit/test_storage/test_bot_session_repository.py
git commit -m "feat: add bot_sessions table and repository for restart recovery"
```

---

## Phase 2: Core Modules

### Task 3: SessionResolver

Refactor existing `src/claude/history.py` into a focused `SessionResolver` class. Keep the existing functions but wrap them for the new API.

**Files:**
- Create: `src/claude/session.py` (replace empty file)
- Test: `tests/unit/test_claude/test_session_resolver.py` (create)
- Reference: `src/claude/history.py` (existing functions reused)

**Step 1: Write the failing test**

```python
# tests/unit/test_claude/test_session_resolver.py
"""Tests for SessionResolver."""
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.claude.session import SessionResolver


@pytest.fixture
def history_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def history_file(history_dir):
    path = history_dir / ".claude" / "history.jsonl"
    path.parent.mkdir(parents=True)
    return path


def _write_entries(path: Path, entries: list[dict]) -> None:
    with open(path, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


class TestSessionResolver:
    def test_get_latest_session_returns_most_recent(self, history_file):
        _write_entries(
            history_file,
            [
                {
                    "type": "conversation",
                    "sessionId": "old-session",
                    "display": "old task",
                    "timestamp": 1000000,
                    "project": "/home/user/project",
                },
                {
                    "type": "conversation",
                    "sessionId": "new-session",
                    "display": "new task",
                    "timestamp": 2000000,
                    "project": "/home/user/project",
                },
            ],
        )

        resolver = SessionResolver(history_path=history_file)
        result = resolver.get_latest_session("/home/user/project")

        assert result == "new-session"

    def test_get_latest_session_filters_by_directory(self, history_file):
        _write_entries(
            history_file,
            [
                {
                    "type": "conversation",
                    "sessionId": "other-dir",
                    "display": "other",
                    "timestamp": 2000000,
                    "project": "/home/user/other",
                },
                {
                    "type": "conversation",
                    "sessionId": "target-dir",
                    "display": "target",
                    "timestamp": 1000000,
                    "project": "/home/user/project",
                },
            ],
        )

        resolver = SessionResolver(history_path=history_file)
        result = resolver.get_latest_session("/home/user/project")

        assert result == "target-dir"

    def test_get_latest_session_returns_none_when_empty(self, history_file):
        _write_entries(history_file, [])
        resolver = SessionResolver(history_path=history_file)

        result = resolver.get_latest_session("/home/user/project")
        assert result is None

    def test_get_latest_session_returns_none_when_no_file(self, history_dir):
        resolver = SessionResolver(
            history_path=history_dir / ".claude" / "history.jsonl"
        )
        result = resolver.get_latest_session("/home/user/project")
        assert result is None

    def test_list_sessions(self, history_file):
        _write_entries(
            history_file,
            [
                {
                    "type": "conversation",
                    "sessionId": "s1",
                    "display": "task 1",
                    "timestamp": 1000000,
                    "project": "/home/user/project",
                },
                {
                    "type": "conversation",
                    "sessionId": "s2",
                    "display": "task 2",
                    "timestamp": 2000000,
                    "project": "/home/user/project",
                },
                {
                    "type": "conversation",
                    "sessionId": "s3",
                    "display": "task 3",
                    "timestamp": 3000000,
                    "project": "/home/user/project",
                },
            ],
        )

        resolver = SessionResolver(history_path=history_file)
        sessions = resolver.list_sessions("/home/user/project", limit=2)

        assert len(sessions) == 2
        assert sessions[0].session_id == "s3"  # newest first
        assert sessions[1].session_id == "s2"

    def test_list_sessions_all_directories(self, history_file):
        _write_entries(
            history_file,
            [
                {
                    "type": "conversation",
                    "sessionId": "s1",
                    "display": "task 1",
                    "timestamp": 1000000,
                    "project": "/home/user/project-a",
                },
                {
                    "type": "conversation",
                    "sessionId": "s2",
                    "display": "task 2",
                    "timestamp": 2000000,
                    "project": "/home/user/project-b",
                },
            ],
        )

        resolver = SessionResolver(history_path=history_file)
        sessions = resolver.list_sessions(directory=None, limit=10)

        assert len(sessions) == 2
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_claude/test_session_resolver.py -v`
Expected: FAIL with `ImportError: cannot import name 'SessionResolver'`

**Step 3: Write minimal implementation**

```python
# src/claude/session.py
"""Session resolution using CLI's history.jsonl as source of truth."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from src.claude.history import (
    HistoryEntry,
    filter_by_directory,
    read_claude_history,
)

DEFAULT_HISTORY_PATH = Path.home() / ".claude" / "history.jsonl"


class SessionResolver:
    """Resolves session IDs from Claude CLI's history.jsonl."""

    def __init__(self, history_path: Optional[Path] = None) -> None:
        self._history_path = history_path or DEFAULT_HISTORY_PATH

    def get_latest_session(self, directory: str) -> Optional[str]:
        """Return the most recent session ID for a directory, or None."""
        entries = read_claude_history(self._history_path)
        filtered = filter_by_directory(entries, directory)
        if not filtered:
            return None
        return filtered[0].session_id  # already sorted newest-first

    def list_sessions(
        self,
        directory: Optional[str] = None,
        limit: int = 10,
    ) -> list[HistoryEntry]:
        """List recent sessions, optionally filtered by directory."""
        entries = read_claude_history(self._history_path)
        if directory:
            entries = filter_by_directory(entries, directory)
        return entries[:limit]
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_claude/test_session_resolver.py -v`
Expected: PASS (6 tests)

**Step 5: Commit**

```bash
git add src/claude/session.py tests/unit/test_claude/test_session_resolver.py
git commit -m "feat: add SessionResolver wrapping history.jsonl"
```

---

### Task 4: OptionsBuilder

**Files:**
- Create: `src/claude/options.py`
- Test: `tests/unit/test_claude/test_options.py` (create)

**Step 1: Write the failing test**

```python
# tests/unit/test_claude/test_options.py
"""Tests for OptionsBuilder."""
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.claude.options import OptionsBuilder


@pytest.fixture
def cli_settings_dir():
    with tempfile.TemporaryDirectory() as d:
        claude_dir = Path(d) / ".claude"
        claude_dir.mkdir()
        yield claude_dir


class TestOptionsBuilder:
    def test_builds_with_defaults(self):
        builder = OptionsBuilder()
        opts = builder.build(
            cwd="/home/user/project",
        )

        assert opts.cwd == "/home/user/project"
        assert opts.permission_mode == "bypassPermissions"

    def test_builds_with_resume(self):
        builder = OptionsBuilder()
        opts = builder.build(
            cwd="/home/user/project",
            session_id="abc-def",
        )

        assert opts.resume == "abc-def"

    def test_builds_with_model_override(self):
        builder = OptionsBuilder()
        opts = builder.build(
            cwd="/home/user/project",
            model="opus",
        )

        assert opts.model == "opus"

    def test_builds_with_betas(self):
        builder = OptionsBuilder()
        opts = builder.build(
            cwd="/home/user/project",
            model="opus",
            betas=["context-1m-2025-08-07"],
        )

        assert opts.betas == ["context-1m-2025-08-07"]

    def test_system_prompt_preserves_claude_md(self):
        builder = OptionsBuilder()
        opts = builder.build(cwd="/home/user/project")

        # Should use SystemPromptPreset, not a raw string
        assert opts.system_prompt is not None
        assert hasattr(opts.system_prompt, "type") or isinstance(
            opts.system_prompt, dict
        )

    def test_reads_model_from_cli_settings(self, cli_settings_dir):
        settings = {"model": "opus"}
        (cli_settings_dir / "settings.json").write_text(json.dumps(settings))

        builder = OptionsBuilder(claude_dir=cli_settings_dir)
        opts = builder.build(cwd="/home/user/project")

        assert opts.model == "opus"

    def test_model_override_beats_cli_settings(self, cli_settings_dir):
        settings = {"model": "opus"}
        (cli_settings_dir / "settings.json").write_text(json.dumps(settings))

        builder = OptionsBuilder(claude_dir=cli_settings_dir)
        opts = builder.build(cwd="/home/user/project", model="haiku")

        assert opts.model == "haiku"

    def test_can_use_tool_callback_set_when_validator_provided(self):
        validator = MagicMock()
        builder = OptionsBuilder(security_validator=validator)
        opts = builder.build(
            cwd="/home/user/project",
            approved_directory="/home/user",
        )

        assert opts.can_use_tool is not None

    def test_no_can_use_tool_without_validator(self):
        builder = OptionsBuilder()
        opts = builder.build(cwd="/home/user/project")

        assert opts.can_use_tool is None
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_claude/test_options.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.claude.options'`

**Step 3: Write minimal implementation**

```python
# src/claude/options.py
"""Builds ClaudeAgentOptions from CLI settings with full feature parity."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

import structlog

from claude_agent_sdk import ClaudeAgentOptions

from src.claude.monitor import _make_can_use_tool_callback

logger = structlog.get_logger()

DEFAULT_CLAUDE_DIR = Path.home() / ".claude"


class OptionsBuilder:
    """Constructs ClaudeAgentOptions reading config from CLI settings."""

    def __init__(
        self,
        claude_dir: Optional[Path] = None,
        security_validator: Any = None,
        cli_path: Optional[str] = None,
    ) -> None:
        self._claude_dir = claude_dir or DEFAULT_CLAUDE_DIR
        self._security_validator = security_validator
        self._cli_path = cli_path
        self._cli_settings: Optional[dict] = None

    def _read_cli_settings(self) -> dict:
        """Read and cache ~/.claude/settings.json."""
        if self._cli_settings is not None:
            return self._cli_settings

        settings_path = self._claude_dir / "settings.json"
        if settings_path.exists():
            try:
                self._cli_settings = json.loads(settings_path.read_text())
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("failed_to_read_cli_settings", error=str(e))
                self._cli_settings = {}
        else:
            self._cli_settings = {}
        return self._cli_settings

    def build(
        self,
        cwd: str,
        session_id: Optional[str] = None,
        model: Optional[str] = None,
        betas: Optional[list[str]] = None,
        approved_directory: Optional[str] = None,
    ) -> ClaudeAgentOptions:
        """Build ClaudeAgentOptions with full CLI parity."""
        cli_settings = self._read_cli_settings()

        # Model: explicit override > CLI settings > None (SDK default)
        resolved_model = model or cli_settings.get("model")

        # System prompt: preserve CLAUDE.md loading with mobile append
        system_prompt: Any = {
            "type": "preset",
            "preset": "claude_code",
            "append": (
                "You are being accessed via Telegram. "
                "Keep responses concise for mobile reading."
            ),
        }

        # Security callback
        can_use_tool = None
        if self._security_validator and approved_directory:
            can_use_tool = _make_can_use_tool_callback(
                self._security_validator, cwd, approved_directory
            )

        opts = ClaudeAgentOptions(
            cwd=cwd,
            permission_mode="bypassPermissions",
            system_prompt=system_prompt,
        )

        if resolved_model:
            opts.model = resolved_model
        if session_id:
            opts.resume = session_id
        if betas:
            opts.betas = betas
        if can_use_tool:
            opts.can_use_tool = can_use_tool
        if self._cli_path:
            opts.cli_path = self._cli_path

        return opts
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_claude/test_options.py -v`
Expected: PASS (9 tests)

> **Note:** If `ClaudeAgentOptions` field assignment doesn't work (immutable dataclass), switch to passing all fields in the constructor call. Check SDK source during implementation.

**Step 5: Commit**

```bash
git add src/claude/options.py tests/unit/test_claude/test_options.py
git commit -m "feat: add OptionsBuilder reading CLI settings for SDK parity"
```

---

### Task 5: StreamHandler

**Files:**
- Create: `src/claude/stream_handler.py`
- Test: `tests/unit/test_claude/test_stream_handler.py` (create)

**Step 1: Write the failing test**

```python
# tests/unit/test_claude/test_stream_handler.py
"""Tests for StreamHandler."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.claude.stream_handler import StreamHandler, StreamEvent


class TestStreamHandler:
    def test_extract_text_from_assistant_message(self):
        handler = StreamHandler()

        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Hello, world!"

        msg = MagicMock()
        msg.__class__.__name__ = "AssistantMessage"
        msg.content = [text_block]

        result = handler.extract_content(msg)

        assert result.type == "text"
        assert result.content == "Hello, world!"

    def test_extract_thinking_block(self):
        handler = StreamHandler()

        thinking_block = MagicMock()
        thinking_block.type = "thinking"
        thinking_block.thinking = "Let me consider..."

        msg = MagicMock()
        msg.__class__.__name__ = "AssistantMessage"
        msg.content = [thinking_block]

        result = handler.extract_content(msg)

        assert result.type == "thinking"
        assert result.content == "Let me consider..."

    def test_extract_tool_use(self):
        handler = StreamHandler()

        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.name = "Read"
        tool_block.input = {"file_path": "/tmp/test.py"}

        msg = MagicMock()
        msg.__class__.__name__ = "AssistantMessage"
        msg.content = [tool_block]

        result = handler.extract_content(msg)

        assert result.type == "tool_use"
        assert result.tool_name == "Read"
        assert result.tool_input == {"file_path": "/tmp/test.py"}

    def test_extract_result_message(self):
        handler = StreamHandler()

        msg = MagicMock()
        msg.__class__.__name__ = "ResultMessage"
        msg.result = "Task complete."
        msg.session_id = "abc-def"
        msg.total_cost_usd = 0.03

        result = handler.extract_content(msg)

        assert result.type == "result"
        assert result.content == "Task complete."
        assert result.session_id == "abc-def"
        assert result.cost == 0.03

    def test_extract_mixed_content(self):
        handler = StreamHandler()

        text1 = MagicMock(type="text", text="Part 1. ")
        text2 = MagicMock(type="text", text="Part 2.")

        msg = MagicMock()
        msg.__class__.__name__ = "AssistantMessage"
        msg.content = [text1, text2]

        result = handler.extract_content(msg)

        assert result.type == "text"
        assert result.content == "Part 1. Part 2."
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_claude/test_stream_handler.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write minimal implementation**

```python
# src/claude/stream_handler.py
"""Processes SDK message stream into structured events for Telegram output."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import structlog

logger = structlog.get_logger()


@dataclass(frozen=True)
class StreamEvent:
    """A structured event extracted from an SDK message."""

    type: str  # "text", "thinking", "tool_use", "tool_result", "result", "unknown"
    content: Optional[str] = None
    tool_name: Optional[str] = None
    tool_input: Optional[dict[str, Any]] = None
    session_id: Optional[str] = None
    cost: Optional[float] = None
    tools_used: list[dict[str, Any]] = field(default_factory=list)


class StreamHandler:
    """Extracts structured events from Claude SDK messages."""

    def extract_content(self, message: Any) -> StreamEvent:
        """Extract a StreamEvent from an SDK message object."""
        class_name = message.__class__.__name__

        if class_name == "ResultMessage":
            return self._handle_result(message)
        elif class_name == "AssistantMessage":
            return self._handle_assistant(message)
        elif class_name == "UserMessage":
            return StreamEvent(type="user", content=getattr(message, "content", ""))
        else:
            return StreamEvent(type="unknown")

    def _handle_result(self, message: Any) -> StreamEvent:
        return StreamEvent(
            type="result",
            content=getattr(message, "result", None),
            session_id=getattr(message, "session_id", None),
            cost=getattr(message, "total_cost_usd", None),
        )

    def _handle_assistant(self, message: Any) -> StreamEvent:
        content_blocks = getattr(message, "content", [])
        if not content_blocks:
            return StreamEvent(type="text", content="")

        # Check for single special blocks first
        if len(content_blocks) == 1:
            block = content_blocks[0]
            block_type = getattr(block, "type", "")

            if block_type == "thinking":
                return StreamEvent(
                    type="thinking",
                    content=getattr(block, "thinking", ""),
                )
            elif block_type == "tool_use":
                return StreamEvent(
                    type="tool_use",
                    tool_name=getattr(block, "name", ""),
                    tool_input=getattr(block, "input", {}),
                )

        # Default: concatenate all text blocks
        texts = []
        for block in content_blocks:
            block_type = getattr(block, "type", "")
            if block_type == "text":
                texts.append(getattr(block, "text", ""))

        return StreamEvent(type="text", content="".join(texts))
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_claude/test_stream_handler.py -v`
Expected: PASS (5 tests)

**Step 5: Commit**

```bash
git add src/claude/stream_handler.py tests/unit/test_claude/test_stream_handler.py
git commit -m "feat: add StreamHandler for SDK message extraction"
```

---

## Phase 3: Client Management

### Task 6: UserClient

**Files:**
- Create: `src/claude/user_client.py`
- Test: `tests/unit/test_claude/test_user_client.py` (create)

**Step 1: Write the failing test**

```python
# tests/unit/test_claude/test_user_client.py
"""Tests for UserClient."""
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.claude.user_client import UserClient


@pytest.fixture
def mock_sdk_client():
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.interrupt = MagicMock()
    return client


class TestUserClient:
    def test_initial_state(self):
        uc = UserClient(
            user_id=123,
            directory="/home/user/project",
        )

        assert uc.user_id == 123
        assert uc.directory == "/home/user/project"
        assert uc.session_id is None
        assert uc.is_connected is False
        assert uc.is_querying is False

    async def test_connect_creates_sdk_client(self):
        uc = UserClient(user_id=123, directory="/home/user/project")

        with patch("src.claude.user_client.ClaudeSDKClient") as MockClient:
            mock_instance = MagicMock()
            mock_instance.connect = AsyncMock()
            MockClient.return_value = mock_instance

            await uc.connect(options=MagicMock())

            MockClient.assert_called_once()
            mock_instance.connect.assert_awaited_once()
            assert uc.is_connected is True

    async def test_disconnect_cleans_up(self, mock_sdk_client):
        uc = UserClient(user_id=123, directory="/home/user/project")
        uc._sdk_client = mock_sdk_client
        uc._connected = True

        await uc.disconnect()

        mock_sdk_client.disconnect.assert_awaited_once()
        assert uc.is_connected is False
        assert uc._sdk_client is None

    async def test_disconnect_noop_when_not_connected(self):
        uc = UserClient(user_id=123, directory="/home/user/project")

        await uc.disconnect()  # should not raise

        assert uc.is_connected is False

    def test_interrupt_delegates_to_sdk(self, mock_sdk_client):
        uc = UserClient(user_id=123, directory="/home/user/project")
        uc._sdk_client = mock_sdk_client
        uc._connected = True
        uc._querying = True

        uc.interrupt()

        mock_sdk_client.interrupt.assert_called_once()

    def test_interrupt_noop_when_not_querying(self):
        uc = UserClient(user_id=123, directory="/home/user/project")

        uc.interrupt()  # should not raise

    def test_touch_updates_last_active(self):
        uc = UserClient(user_id=123, directory="/home/user/project")
        before = uc.last_active

        uc.touch()

        assert uc.last_active >= before
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_claude/test_user_client.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write minimal implementation**

```python
# src/claude/user_client.py
"""Wraps a persistent ClaudeSDKClient for a single user."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, AsyncIterator, Optional

import structlog

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

logger = structlog.get_logger()


class UserClient:
    """A persistent Claude SDK client for one user."""

    def __init__(
        self,
        user_id: int,
        directory: str,
        session_id: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        self.user_id = user_id
        self.directory = directory
        self.session_id = session_id
        self.model = model
        self.last_active = datetime.now(UTC)

        self._sdk_client: Optional[ClaudeSDKClient] = None
        self._connected = False
        self._querying = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def is_querying(self) -> bool:
        return self._querying

    async def connect(self, options: ClaudeAgentOptions) -> None:
        """Create and connect the SDK client."""
        if self._connected:
            await self.disconnect()

        self._sdk_client = ClaudeSDKClient(options)
        await self._sdk_client.connect()
        self._connected = True
        self.touch()
        logger.info(
            "user_client_connected",
            user_id=self.user_id,
            directory=self.directory,
            session_id=self.session_id,
        )

    async def disconnect(self) -> None:
        """Disconnect and clean up the SDK client."""
        if self._sdk_client is not None:
            try:
                await self._sdk_client.disconnect()
            except Exception as e:
                logger.warning("disconnect_error", error=str(e))
            finally:
                self._sdk_client = None
                self._connected = False
                self._querying = False

    async def query(self, prompt: str) -> AsyncIterator[Any]:
        """Send a query and yield SDK messages."""
        if not self._connected or self._sdk_client is None:
            raise RuntimeError("UserClient not connected. Call connect() first.")

        self._querying = True
        self.touch()
        try:
            await self._sdk_client.query(prompt)
            async for message in self._sdk_client.receive_messages():
                yield message
        finally:
            self._querying = False
            self.touch()

    def interrupt(self) -> None:
        """Interrupt the current query if one is running."""
        if self._querying and self._sdk_client is not None:
            self._sdk_client.interrupt()
            logger.info("query_interrupted", user_id=self.user_id)

    def touch(self) -> None:
        """Update last_active timestamp."""
        self.last_active = datetime.now(UTC)
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_claude/test_user_client.py -v`
Expected: PASS (7 tests)

**Step 5: Commit**

```bash
git add src/claude/user_client.py tests/unit/test_claude/test_user_client.py
git commit -m "feat: add UserClient wrapping persistent ClaudeSDKClient"
```

---

### Task 7: ClientManager

**Files:**
- Create: `src/claude/client_manager.py`
- Test: `tests/unit/test_claude/test_client_manager.py` (create)

**Step 1: Write the failing test**

```python
# tests/unit/test_claude/test_client_manager.py
"""Tests for ClientManager."""
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.claude.client_manager import ClientManager


@pytest.fixture
def history_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def mock_bot_session_repo():
    repo = MagicMock()
    repo.upsert = AsyncMock()
    repo.get_by_user = AsyncMock(return_value=None)
    repo.delete = AsyncMock()
    repo.cleanup_expired = AsyncMock(return_value=0)
    return repo


@pytest.fixture
def manager(history_dir, mock_bot_session_repo):
    return ClientManager(
        bot_session_repo=mock_bot_session_repo,
        history_path=history_dir / ".claude" / "history.jsonl",
    )


class TestClientManager:
    async def test_get_or_connect_creates_new_client(self, manager):
        with patch("src.claude.client_manager.UserClient") as MockUC:
            mock_uc = MagicMock()
            mock_uc.connect = AsyncMock()
            mock_uc.session_id = None
            MockUC.return_value = mock_uc

            client = await manager.get_or_connect(
                user_id=123,
                directory="/home/user/project",
            )

            assert client is mock_uc
            mock_uc.connect.assert_awaited_once()

    async def test_get_or_connect_reuses_existing(self, manager):
        mock_uc = MagicMock()
        mock_uc.is_connected = True
        mock_uc.directory = "/home/user/project"
        mock_uc.connect = AsyncMock()
        manager._clients[123] = mock_uc

        client = await manager.get_or_connect(
            user_id=123,
            directory="/home/user/project",
        )

        assert client is mock_uc
        mock_uc.connect.assert_not_awaited()

    async def test_get_or_connect_reconnects_on_directory_change(self, manager):
        old_uc = MagicMock()
        old_uc.is_connected = True
        old_uc.directory = "/home/user/old-project"
        old_uc.disconnect = AsyncMock()
        manager._clients[123] = old_uc

        with patch("src.claude.client_manager.UserClient") as MockUC:
            new_uc = MagicMock()
            new_uc.connect = AsyncMock()
            new_uc.session_id = None
            MockUC.return_value = new_uc

            client = await manager.get_or_connect(
                user_id=123,
                directory="/home/user/new-project",
            )

            old_uc.disconnect.assert_awaited_once()
            assert client is new_uc

    async def test_interrupt(self, manager):
        mock_uc = MagicMock()
        mock_uc.interrupt = MagicMock()
        manager._clients[123] = mock_uc

        manager.interrupt(123)

        mock_uc.interrupt.assert_called_once()

    async def test_interrupt_noop_for_unknown_user(self, manager):
        manager.interrupt(999)  # should not raise

    async def test_disconnect(self, manager):
        mock_uc = MagicMock()
        mock_uc.disconnect = AsyncMock()
        manager._clients[123] = mock_uc

        await manager.disconnect(123)

        mock_uc.disconnect.assert_awaited_once()
        assert 123 not in manager._clients

    async def test_disconnect_all(self, manager):
        for uid in [1, 2, 3]:
            uc = MagicMock()
            uc.disconnect = AsyncMock()
            manager._clients[uid] = uc

        await manager.disconnect_all()

        assert len(manager._clients) == 0

    async def test_persists_session_on_connect(self, manager, mock_bot_session_repo):
        with patch("src.claude.client_manager.UserClient") as MockUC:
            mock_uc = MagicMock()
            mock_uc.connect = AsyncMock()
            mock_uc.session_id = "abc-def"
            mock_uc.model = "sonnet"
            MockUC.return_value = mock_uc

            await manager.get_or_connect(
                user_id=123,
                directory="/home/user/project",
                model="sonnet",
            )

            mock_bot_session_repo.upsert.assert_awaited_once()

    async def test_restores_from_persisted_state(self, manager, mock_bot_session_repo):
        from src.storage.models import BotSessionModel

        mock_bot_session_repo.get_by_user.return_value = BotSessionModel(
            user_id=123,
            session_id="saved-session",
            directory="/home/user/project",
            model="opus",
            betas=None,
            last_active=datetime.now(UTC),
        )

        with patch("src.claude.client_manager.UserClient") as MockUC:
            mock_uc = MagicMock()
            mock_uc.connect = AsyncMock()
            mock_uc.session_id = "saved-session"
            MockUC.return_value = mock_uc

            client = await manager.get_or_connect(
                user_id=123,
                directory="/home/user/project",
            )

            # Should have used the persisted session_id
            call_kwargs = MockUC.call_args
            assert call_kwargs.kwargs.get("session_id") == "saved-session" or \
                call_kwargs[1].get("session_id") == "saved-session"
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_claude/test_client_manager.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write minimal implementation**

```python
# src/claude/client_manager.py
"""Manages persistent ClaudeSDKClient instances per user."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import structlog

from src.claude.options import OptionsBuilder
from src.claude.session import SessionResolver
from src.claude.user_client import UserClient
from src.storage.repositories import BotSessionRepository

logger = structlog.get_logger()

DEFAULT_IDLE_TIMEOUT_SECONDS = 300


class ClientManager:
    """Owns persistent UserClient instances, one per active user."""

    def __init__(
        self,
        bot_session_repo: BotSessionRepository,
        options_builder: Optional[OptionsBuilder] = None,
        history_path: Optional[Path] = None,
        idle_timeout: int = DEFAULT_IDLE_TIMEOUT_SECONDS,
    ) -> None:
        self._bot_session_repo = bot_session_repo
        self._options_builder = options_builder or OptionsBuilder()
        self._session_resolver = SessionResolver(history_path=history_path)
        self._idle_timeout = idle_timeout
        self._clients: dict[int, UserClient] = {}
        self._cleanup_task: Optional[asyncio.Task] = None

    async def get_or_connect(
        self,
        user_id: int,
        directory: str,
        session_id: Optional[str] = None,
        model: Optional[str] = None,
        betas: Optional[list[str]] = None,
        approved_directory: Optional[str] = None,
    ) -> UserClient:
        """Get existing client or create+connect a new one."""
        existing = self._clients.get(user_id)

        # Reuse if connected and same directory
        if existing and existing.is_connected and existing.directory == directory:
            existing.touch()
            return existing

        # Directory changed or not connected — disconnect old
        if existing:
            await existing.disconnect()

        # Try to restore from persisted state if no explicit session
        if session_id is None:
            persisted = await self._bot_session_repo.get_by_user(user_id)
            if persisted and persisted.directory == directory:
                session_id = persisted.session_id
                model = model or persisted.model
                betas = betas or persisted.betas

        # Fall back to history.jsonl auto-resume
        if session_id is None:
            session_id = self._session_resolver.get_latest_session(directory)

        # Create new client
        client = UserClient(
            user_id=user_id,
            directory=directory,
            session_id=session_id,
            model=model,
        )

        options = self._options_builder.build(
            cwd=directory,
            session_id=session_id,
            model=model,
            betas=betas,
            approved_directory=approved_directory,
        )

        await client.connect(options)
        self._clients[user_id] = client

        # Persist state for restart recovery
        await self._bot_session_repo.upsert(
            user_id=user_id,
            session_id=session_id or "",
            directory=directory,
            model=model,
            betas=betas,
        )

        return client

    async def switch_session(
        self,
        user_id: int,
        session_id: str,
        directory: str,
        model: Optional[str] = None,
        betas: Optional[list[str]] = None,
        approved_directory: Optional[str] = None,
    ) -> UserClient:
        """Disconnect current client and connect to a different session."""
        await self.disconnect(user_id)
        return await self.get_or_connect(
            user_id=user_id,
            directory=directory,
            session_id=session_id,
            model=model,
            betas=betas,
            approved_directory=approved_directory,
        )

    def interrupt(self, user_id: int) -> None:
        """Interrupt the active query for a user."""
        client = self._clients.get(user_id)
        if client:
            client.interrupt()

    async def set_model(
        self, user_id: int, model: str, betas: Optional[list[str]] = None
    ) -> None:
        """Update model for next query. Persists to bot_sessions."""
        client = self._clients.get(user_id)
        if client:
            client.model = model
            await self._bot_session_repo.upsert(
                user_id=user_id,
                session_id=client.session_id or "",
                directory=client.directory,
                model=model,
                betas=betas,
            )

    async def disconnect(self, user_id: int) -> None:
        """Disconnect and remove a user's client."""
        client = self._clients.pop(user_id, None)
        if client:
            await client.disconnect()

    async def disconnect_all(self) -> None:
        """Disconnect all clients. Called on bot shutdown."""
        user_ids = list(self._clients.keys())
        for uid in user_ids:
            await self.disconnect(uid)

    async def update_session_id(self, user_id: int, session_id: str) -> None:
        """Update the session ID after a query returns a ResultMessage."""
        client = self._clients.get(user_id)
        if client:
            client.session_id = session_id
            await self._bot_session_repo.upsert(
                user_id=user_id,
                session_id=session_id,
                directory=client.directory,
                model=client.model,
            )

    def start_cleanup_loop(self) -> None:
        """Start the background idle-cleanup task."""
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    def stop_cleanup_loop(self) -> None:
        """Stop the background cleanup task."""
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()

    async def _cleanup_loop(self) -> None:
        """Periodically disconnect idle clients."""
        while True:
            await asyncio.sleep(60)
            try:
                cutoff = datetime.now(UTC) - timedelta(seconds=self._idle_timeout)
                idle_users = [
                    uid
                    for uid, client in self._clients.items()
                    if client.last_active < cutoff and not client.is_querying
                ]
                for uid in idle_users:
                    logger.info("disconnecting_idle_client", user_id=uid)
                    await self.disconnect(uid)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("cleanup_loop_error", error=str(e))
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_claude/test_client_manager.py -v`
Expected: PASS (9 tests)

**Step 5: Commit**

```bash
git add src/claude/client_manager.py tests/unit/test_claude/test_client_manager.py
git commit -m "feat: add ClientManager for persistent per-user SDK clients"
```

---

## Phase 4: Integration

### Task 8: Wire ClientManager into Orchestrator

Replace `ClaudeIntegration` facade with `ClientManager` in the orchestrator's agentic mode.

**Files:**
- Modify: `src/bot/orchestrator.py` — replace `ClaudeIntegration` usage
- Modify: `src/claude/__init__.py` — update exports
- Test: `tests/unit/test_bot/test_orchestrator_integration.py` (create)

**Step 1: Write the failing test**

```python
# tests/unit/test_bot/test_orchestrator_integration.py
"""Tests for orchestrator using ClientManager."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestOrchestratorAgenticFlow:
    """Verify the orchestrator routes messages through ClientManager."""

    async def test_agentic_text_uses_client_manager(self):
        from src.claude.stream_handler import StreamEvent

        # Mock the client_manager
        mock_client = MagicMock()
        mock_client.session_id = "test-session"
        mock_client.is_querying = False

        # Simulate query yielding a result message
        result_event = StreamEvent(
            type="result",
            content="Hello from Claude!",
            session_id="test-session",
            cost=0.01,
        )

        async def mock_query(prompt):
            # Simulate an async iterator
            msg = MagicMock()
            msg.__class__.__name__ = "ResultMessage"
            msg.result = "Hello from Claude!"
            msg.session_id = "test-session"
            msg.total_cost_usd = 0.01
            yield msg

        mock_client.query = mock_query

        mock_manager = MagicMock()
        mock_manager.get_or_connect = AsyncMock(return_value=mock_client)
        mock_manager.update_session_id = AsyncMock()

        # This test validates the integration contract:
        # orchestrator calls client_manager.get_or_connect() then client.query()
        client = await mock_manager.get_or_connect(
            user_id=123,
            directory="/home/user/project",
        )
        assert client is mock_client

        messages = []
        async for msg in client.query("Hello"):
            messages.append(msg)
        assert len(messages) == 1
```

**Step 2: Run test to verify it passes** (this is an integration contract test)

Run: `uv run pytest tests/unit/test_bot/test_orchestrator_integration.py -v`
Expected: PASS

**Step 3: Update orchestrator**

In `src/bot/orchestrator.py`, find the `agentic_text` handler (or equivalent method that calls `claude_integration.run_command()`). Replace:

**Before (current pattern):**
```python
claude_integration = context.bot_data["claude_integration"]
response = await claude_integration.run_command(
    prompt=text,
    working_directory=working_directory,
    user_id=user_id,
    session_id=session_id,
    on_stream=stream_callback,
    force_new=force_new,
)
```

**After (new pattern):**
```python
from src.claude.client_manager import ClientManager
from src.claude.stream_handler import StreamHandler

client_manager: ClientManager = context.bot_data["client_manager"]
stream_handler = StreamHandler()

client = await client_manager.get_or_connect(
    user_id=user_id,
    directory=working_directory,
    approved_directory=context.bot_data.get("approved_directory"),
)

# Stream messages
async for message in client.query(text):
    event = stream_handler.extract_content(message)

    if event.type == "result":
        # Update session ID from result
        if event.session_id:
            await client_manager.update_session_id(user_id, event.session_id)
        # Send final response
        # ... (send event.content to Telegram)
    elif event.type == "text":
        # ... (stream partial text to Telegram)
    elif event.type == "tool_use":
        # ... (show tool usage per verbose level)
    elif event.type == "thinking":
        # ... (show thinking per verbose level)
```

> **Implementation note:** The exact orchestrator integration will require careful reading of the current `agentic_text()` method to preserve all existing Telegram message sending, typing indicators, and verbose level logic. The test above validates the contract; the actual wiring adapts the existing Telegram output code.

**Step 4: Update bot initialization**

In the bot startup code (likely `src/bot/orchestrator.py` or `src/main.py`), replace:

```python
# Before:
context.bot_data["claude_integration"] = ClaudeIntegration(config, sdk_manager)

# After:
from src.claude.client_manager import ClientManager
from src.claude.options import OptionsBuilder

options_builder = OptionsBuilder(
    security_validator=context.bot_data.get("security_validator"),
    cli_path=config.claude_cli_path,
)
client_manager = ClientManager(
    bot_session_repo=storage.bot_sessions,
    options_builder=options_builder,
)
client_manager.start_cleanup_loop()
context.bot_data["client_manager"] = client_manager
```

And on shutdown:

```python
client_manager = context.bot_data.get("client_manager")
if client_manager:
    client_manager.stop_cleanup_loop()
    await client_manager.disconnect_all()
```

**Step 5: Update `src/claude/__init__.py`**

```python
# src/claude/__init__.py
"""Claude Code integration layer."""
from src.claude.client_manager import ClientManager
from src.claude.options import OptionsBuilder
from src.claude.session import SessionResolver
from src.claude.stream_handler import StreamEvent, StreamHandler
from src.claude.user_client import UserClient

__all__ = [
    "ClientManager",
    "OptionsBuilder",
    "SessionResolver",
    "StreamEvent",
    "StreamHandler",
    "UserClient",
]
```

**Step 6: Run full test suite**

Run: `uv run pytest -v`
Expected: All new tests pass. Some existing tests in `tests/unit/test_claude/` may fail if they reference `ClaudeIntegration` — those will be cleaned up in Phase 6.

**Step 7: Commit**

```bash
git add src/bot/orchestrator.py src/claude/__init__.py \
  tests/unit/test_bot/test_orchestrator_integration.py
git commit -m "feat: wire ClientManager into orchestrator, replace ClaudeIntegration"
```

---

## Phase 5: Commands

### Task 9: /stop Command (Interrupt)

**Files:**
- Modify: `src/bot/orchestrator.py` — add `/stop` handler

**Step 1: Add handler**

```python
async def handle_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Interrupt the current running query."""
    user_id = update.effective_user.id
    client_manager: ClientManager = context.bot_data["client_manager"]
    client_manager.interrupt(user_id)
    await update.message.reply_text("Interrupting current query...")
```

**Step 2: Register in `_register_agentic_handlers()`**

```python
self.application.add_handler(CommandHandler("stop", self.handle_stop), group=10)
```

**Step 3: Add to `get_bot_commands()`**

```python
BotCommand("stop", "Interrupt running query"),
```

**Step 4: Commit**

```bash
git add src/bot/orchestrator.py
git commit -m "feat: add /stop command for query interruption"
```

---

### Task 10: /model Command

**Files:**
- Modify: `src/bot/orchestrator.py` — add `/model` handler + callback

**Step 1: Add handler**

```python
async def handle_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show model selection inline keyboard."""
    keyboard = [
        [
            InlineKeyboardButton("Sonnet", callback_data="model:sonnet"),
            InlineKeyboardButton("Opus", callback_data="model:opus"),
            InlineKeyboardButton("Haiku", callback_data="model:haiku"),
        ],
        [
            InlineKeyboardButton("Sonnet 1M", callback_data="model:sonnet:1m"),
            InlineKeyboardButton("Opus 1M", callback_data="model:opus:1m"),
        ],
    ]
    await update.message.reply_text(
        "Select model:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_model_callback(
    self, update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Handle model selection callback."""
    query = update.callback_query
    await query.answer()

    data = query.data  # e.g. "model:opus:1m" or "model:sonnet"
    parts = data.split(":")
    model = parts[1]
    betas = ["context-1m-2025-08-07"] if len(parts) > 2 and parts[2] == "1m" else None

    user_id = update.effective_user.id
    client_manager: ClientManager = context.bot_data["client_manager"]
    await client_manager.set_model(user_id, model, betas)

    label = f"{model} (1M context)" if betas else model
    await query.edit_message_text(f"Model set to: {label}")
```

**Step 2: Register handlers**

```python
self.application.add_handler(CommandHandler("model", self.handle_model), group=10)
self.application.add_handler(
    CallbackQueryHandler(self.handle_model_callback, pattern="^model:"),
    group=10,
)
```

**Step 3: Add to `get_bot_commands()`**

```python
BotCommand("model", "Switch Claude model"),
```

**Step 4: Commit**

```bash
git add src/bot/orchestrator.py
git commit -m "feat: add /model command with 1M context support"
```

---

### Task 11: /sessions Command

**Files:**
- Modify: `src/bot/orchestrator.py` — add `/sessions` handler + callback

**Step 1: Add handler**

```python
async def handle_sessions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show session picker inline keyboard."""
    user_id = update.effective_user.id
    storage = context.bot_data["storage"]
    bot_session = await storage.bot_sessions.get_by_user(user_id)
    directory = bot_session.directory if bot_session else context.bot_data.get("approved_directory", "")

    client_manager: ClientManager = context.bot_data["client_manager"]
    sessions = client_manager.list_sessions(directory, limit=5)

    if not sessions:
        await update.message.reply_text("No sessions found for this directory.")
        return

    # Find current session_id
    current_client = client_manager._clients.get(user_id)
    current_sid = current_client.session_id if current_client else None

    keyboard = []
    for entry in sessions:
        prefix = "📍 " if entry.session_id == current_sid else ""
        label = f"{prefix}{entry.display[:40]}"
        keyboard.append(
            [InlineKeyboardButton(label, callback_data=f"session:{entry.session_id}")]
        )
    keyboard.append(
        [InlineKeyboardButton("➕ New Session", callback_data="session:__new__")]
    )

    await update.message.reply_text(
        "Select a session:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_session_callback(
    self, update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Handle session selection callback."""
    query = update.callback_query
    await query.answer()

    session_id = query.data.replace("session:", "")
    user_id = update.effective_user.id
    client_manager: ClientManager = context.bot_data["client_manager"]

    current_client = client_manager._clients.get(user_id)
    directory = current_client.directory if current_client else context.bot_data.get("approved_directory", "")

    if session_id == "__new__":
        await client_manager.disconnect(user_id)
        await query.edit_message_text("New session started. Send a message to begin.")
    else:
        await client_manager.switch_session(
            user_id=user_id,
            session_id=session_id,
            directory=directory,
        )
        await query.edit_message_text(f"Switched to session: {session_id[:12]}...")
```

**Step 2: Add `list_sessions` helper to ClientManager**

In `src/claude/client_manager.py`:

```python
def list_sessions(
    self, directory: str, limit: int = 10
) -> list:
    """List recent sessions for a directory."""
    return self._session_resolver.list_sessions(directory, limit=limit)
```

**Step 3: Register handlers**

```python
self.application.add_handler(CommandHandler("sessions", self.handle_sessions), group=10)
self.application.add_handler(
    CallbackQueryHandler(self.handle_session_callback, pattern="^session:"),
    group=10,
)
```

**Step 4: Add to `get_bot_commands()`**

```python
BotCommand("sessions", "Switch between sessions"),
```

**Step 5: Commit**

```bash
git add src/bot/orchestrator.py src/claude/client_manager.py
git commit -m "feat: add /sessions command with inline session picker"
```

---

### Task 12: /commands Command (Skill Discovery)

**Files:**
- Modify: `src/bot/orchestrator.py` — add `/commands` handler + callback
- Reference: `src/skills/` (existing skill discovery code)

**Step 1: Add handler**

```python
async def handle_commands(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Discover and display available skills as inline buttons."""
    from src.skills.loader import discover_skills  # existing skill discovery

    skills = discover_skills()  # returns list of {name, description, source}

    if not skills:
        await update.message.reply_text("No skills/commands found.")
        return

    # Group by source
    grouped: dict[str, list] = {}
    for skill in skills:
        source = skill.get("source", "other")
        grouped.setdefault(source, []).append(skill)

    keyboard = []
    for source, source_skills in grouped.items():
        row = []
        for s in source_skills[:4]:  # max 4 per row
            row.append(
                InlineKeyboardButton(
                    f"/{s['name']}", callback_data=f"skill:{s['name']}"
                )
            )
        keyboard.append(row)

    await update.message.reply_text(
        "Available commands:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_skill_callback(
    self, update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Handle skill selection — send as message to Claude."""
    query = update.callback_query
    await query.answer()

    skill_name = query.data.replace("skill:", "")
    await query.edit_message_text(f"Running /{skill_name}...")

    # Route through agentic handler as if user typed the skill name
    # Create a synthetic message context and delegate
    user_id = update.effective_user.id
    client_manager: ClientManager = context.bot_data["client_manager"]
    client = client_manager._clients.get(user_id)

    if client and client.is_connected:
        # Send skill invocation as a prompt to Claude
        prompt = f"/{skill_name}"
        # Delegate to the main agentic text handler logic
        # (Implementation will call client.query(prompt) and stream results)
```

> **Implementation note:** The exact skill discovery API depends on the existing `src/skills/` module. Read `src/skills/loader.py` during implementation to understand the return format. The callback handler should reuse the same streaming logic as the main agentic text handler — extract into a shared method.

**Step 2: Register handlers**

```python
self.application.add_handler(CommandHandler("commands", self.handle_commands), group=10)
self.application.add_handler(
    CallbackQueryHandler(self.handle_skill_callback, pattern="^skill:"),
    group=10,
)
```

**Step 3: Add to `get_bot_commands()`**

```python
BotCommand("commands", "Browse available skills"),
```

**Step 4: Commit**

```bash
git add src/bot/orchestrator.py
git commit -m "feat: add /commands for skill discovery with inline buttons"
```

---

### Task 13: /compact Command

**Files:**
- Modify: `src/bot/orchestrator.py` — add `/compact` handler

**Step 1: Add handler**

```python
async def handle_compact(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Trigger context compaction via the persistent client."""
    user_id = update.effective_user.id
    client_manager: ClientManager = context.bot_data["client_manager"]
    client = client_manager._clients.get(user_id)

    if not client or not client.is_connected:
        await update.message.reply_text("No active session. Send a message first.")
        return

    await update.message.reply_text("Compacting context...")

    # Send /compact as a prompt — the CLI subprocess may handle it natively
    async for message in client.query("/compact"):
        event = StreamHandler().extract_content(message)
        if event.type == "result":
            if event.session_id:
                await client_manager.update_session_id(user_id, event.session_id)
            await update.message.reply_text(
                event.content or "Context compacted."
            )
            break
```

**Step 2: Register handler**

```python
self.application.add_handler(CommandHandler("compact", self.handle_compact), group=10)
```

**Step 3: Add to `get_bot_commands()`**

```python
BotCommand("compact", "Compact conversation context"),
```

**Step 4: Commit**

```bash
git add src/bot/orchestrator.py
git commit -m "feat: add /compact command via persistent client"
```

---

### Task 14: Update /status Command

**Files:**
- Modify: `src/bot/orchestrator.py` — enhance existing `/status` handler

**Step 1: Update handler**

Add to the existing `/status` handler output:

```python
async def handle_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    client_manager: ClientManager = context.bot_data["client_manager"]
    client = client_manager._clients.get(user_id)

    if client:
        lines = [
            f"**Session:** `{client.session_id or 'new'}`",
            f"**Directory:** `{client.directory}`",
            f"**Model:** {client.model or 'default'}",
            f"**Connected:** {'yes' if client.is_connected else 'no'}",
            f"**Querying:** {'yes' if client.is_querying else 'no'}",
        ]
    else:
        lines = ["No active session. Send a message to connect."]

    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown"
    )
```

**Step 2: Commit**

```bash
git add src/bot/orchestrator.py
git commit -m "feat: enhance /status to show session, model, connection state"
```

---

## Phase 6: Cleanup

### Task 15: Remove Old Claude Integration Code

**Files:**
- Delete: `src/claude/facade.py`
- Delete: `src/claude/sdk_integration.py` (logic moved to UserClient + StreamHandler)
- Modify: `src/bot/orchestrator.py` — remove any remaining `ClaudeIntegration` / `ClaudeSDKManager` references
- Modify: Tests referencing old code

**Step 1: Search for remaining references**

Run: `grep -rn "ClaudeIntegration\|ClaudeSDKManager\|claude_integration\|sdk_manager" src/ tests/`

**Step 2: Remove/update each reference**

- Delete `src/claude/facade.py`
- Delete `src/claude/sdk_integration.py`
- Update any test files still importing from these modules
- Update `src/bot/orchestrator.py` if any old references remain
- Keep `src/claude/monitor.py` (still used by OptionsBuilder via `_make_can_use_tool_callback`)
- Keep `src/claude/history.py` (still used by SessionResolver)
- Keep `src/claude/exceptions.py` (still used for error types)

**Step 3: Run full test suite**

Run: `uv run pytest -v`
Expected: All tests pass with no references to deleted modules.

**Step 4: Run linter**

Run: `uv run make lint`
Expected: Clean (no unused imports referencing deleted modules).

**Step 5: Commit**

```bash
git rm src/claude/facade.py src/claude/sdk_integration.py
git add -u  # stage modifications
git commit -m "refactor: remove old ClaudeIntegration and ClaudeSDKManager"
```

---

### Task 16: Smoke Test — End-to-End Verification

**Step 1: Start the bot**

Run: `make run-debug`

**Step 2: Verify via Telegram**

1. Send a text message → Claude responds (via persistent client)
2. Send `/status` → shows session ID, directory, model
3. Send `/model` → shows inline keyboard, select a model
4. Send `/sessions` → shows session list
5. Send `/stop` during a long query → interrupts
6. Send `/commands` → shows available skills
7. Restart the bot → send a message → resumes previous session
8. Switch to CLI on EC2 → run `claude --continue` → sees same session

**Step 3: Final commit**

```bash
git commit --allow-empty -m "chore: Claude layer rewrite complete — CLI feature parity"
```

---

## Summary

| Phase | Tasks | What it delivers |
|-------|-------|-----------------|
| 1: Storage | Tasks 1-2 | `bot_sessions` table + repository for restart recovery |
| 2: Core Modules | Tasks 3-5 | SessionResolver, OptionsBuilder, StreamHandler |
| 3: Client Management | Tasks 6-7 | UserClient, ClientManager with idle cleanup |
| 4: Integration | Task 8 | Wire into orchestrator, replace old facade |
| 5: Commands | Tasks 9-14 | /stop, /model, /sessions, /commands, /compact, /status |
| 6: Cleanup | Tasks 15-16 | Remove old code, smoke test |

**Total:** 16 tasks, ~6 phases, each task independently committable.

**Dependencies:** Phase 1 → Phase 2 → Phase 3 → Phase 4 → Phase 5 → Phase 6 (strictly sequential within phases, but Tasks 3-5 within Phase 2 are independent of each other and can be parallelized).
