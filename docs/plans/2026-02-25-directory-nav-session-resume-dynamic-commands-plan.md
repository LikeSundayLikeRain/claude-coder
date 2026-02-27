# Directory Navigation, Session Resume & Dynamic Commands — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Enable multi-root directory navigation, session picker with resume, and dynamic Claude Code skill loading from the Telegram bot.

**Architecture:** Extends the existing agentic mode with three new commands (`/sessions`, `/commands`, `/compact`) and enhances `/repo` for multi-root support. Session discovery reads Claude Code's `~/.claude/history.jsonl` backed by a thin SQLite registry. Skill discovery scans `.claude/skills/` directories and parses YAML frontmatter from `SKILL.md` files. All changes follow TDD and maintain backward compatibility.

**Tech Stack:** Python 3.12, python-telegram-bot (inline keyboards, switch_inline_query_current_chat), aiosqlite, PyYAML (frontmatter parsing), claude-agent-sdk, pytest-asyncio.

**Design doc:** `docs/plans/2026-02-25-directory-nav-session-resume-dynamic-commands-design.md`

---

## Phase 1: Multi-Root Directory Support

### Task 1: Add `APPROVED_DIRECTORIES` config field

**Files:**
- Modify: `src/config/settings.py:42` (add new field near existing `approved_directory`)
- Test: `tests/unit/test_config.py`

**Step 1: Write the failing tests**

Add to `tests/unit/test_config.py`:

```python
class TestApprovedDirectories:
    """Tests for APPROVED_DIRECTORIES multi-root support."""

    def test_single_approved_directory_backward_compat(self, tmp_path):
        """APPROVED_DIRECTORY still works as before."""
        d = tmp_path / "myproject"
        d.mkdir()
        settings = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=d,
        )
        assert settings.approved_directories == [d]

    def test_multiple_approved_directories(self, tmp_path):
        """APPROVED_DIRECTORIES accepts comma-separated paths."""
        d1 = tmp_path / "project1"
        d2 = tmp_path / "project2"
        d1.mkdir()
        d2.mkdir()
        settings = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=d1,
            approved_directories_str=f"{d1},{d2}",
        )
        assert settings.approved_directories == [d1, d2]

    def test_approved_directories_rejects_nonexistent(self, tmp_path):
        """Rejects directories that don't exist."""
        d1 = tmp_path / "exists"
        d1.mkdir()
        d2 = tmp_path / "nope"
        with pytest.raises(ValidationError):
            Settings(
                telegram_bot_token="test:token",
                telegram_bot_username="testbot",
                approved_directory=d1,
                approved_directories_str=f"{d1},{d2}",
            )

    def test_approved_directories_rejects_overlapping(self, tmp_path):
        """Rejects overlapping directory paths."""
        parent = tmp_path / "projects"
        child = parent / "myapp"
        parent.mkdir()
        child.mkdir()
        with pytest.raises(ValidationError):
            Settings(
                telegram_bot_token="test:token",
                telegram_bot_username="testbot",
                approved_directory=parent,
                approved_directories_str=f"{parent},{child}",
            )
```

**Step 2: Run tests to verify they fail**

Run: `cd /local/home/moxu/claude-coder && python -m pytest tests/unit/test_config.py::TestApprovedDirectories -v`
Expected: FAIL — `approved_directories_str` field doesn't exist

**Step 3: Implement in settings.py**

In `src/config/settings.py`, add near line 42 (after `approved_directory`):

```python
    approved_directories_str: str = Field(
        default="",
        alias="APPROVED_DIRECTORIES",
        description="Comma-separated list of approved directory roots",
    )
```

Add a computed property after existing properties (~line 389):

```python
    @computed_field
    @property
    def approved_directories(self) -> list[Path]:
        """Return list of approved directories. Falls back to single approved_directory."""
        if self.approved_directories_str:
            return [Path(p.strip()).resolve() for p in self.approved_directories_str.split(",") if p.strip()]
        return [self.approved_directory]
```

Add a validator after `validate_cross_field_dependencies` (~line 360):

```python
    @model_validator(mode="after")
    def validate_approved_directories(self) -> "Settings":
        """Validate all approved directories exist and don't overlap."""
        dirs = self.approved_directories
        for d in dirs:
            if not d.exists():
                raise ValueError(f"Approved directory does not exist: {d}")
            if not d.is_dir():
                raise ValueError(f"Approved directory is not a directory: {d}")
            if not d.is_absolute():
                raise ValueError(f"Approved directory must be absolute: {d}")
        # Check for overlapping paths
        for i, d1 in enumerate(dirs):
            for d2 in dirs[i + 1:]:
                try:
                    d1.relative_to(d2)
                    raise ValueError(f"Overlapping approved directories: {d1} is within {d2}")
                except ValueError as e:
                    if "Overlapping" in str(e):
                        raise
                try:
                    d2.relative_to(d1)
                    raise ValueError(f"Overlapping approved directories: {d2} is within {d1}")
                except ValueError as e:
                    if "Overlapping" in str(e):
                        raise
        return self
```

**Step 4: Run tests to verify they pass**

Run: `cd /local/home/moxu/claude-coder && python -m pytest tests/unit/test_config.py::TestApprovedDirectories -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/config/settings.py tests/unit/test_config.py
git commit -m "feat: add APPROVED_DIRECTORIES multi-root config support"
```

---

### Task 2: Update SecurityValidator for multi-root

**Files:**
- Modify: `src/security/validators.py:134-216`
- Test: `tests/unit/test_security/test_validators.py`

**Step 1: Write the failing tests**

Add to `tests/unit/test_security/test_validators.py`:

```python
class TestMultiRootValidation:
    """Tests for multi-root directory validation."""

    def test_validate_path_within_any_root(self, tmp_path):
        """Path valid if within any approved directory."""
        root1 = tmp_path / "root1"
        root2 = tmp_path / "root2"
        root1.mkdir()
        root2.mkdir()
        (root2 / "file.py").touch()
        validator = SecurityValidator(approved_directories=[root1, root2])
        is_valid, resolved, err = validator.validate_path("file.py", root2)
        assert is_valid
        assert resolved == root2 / "file.py"

    def test_validate_path_outside_all_roots(self, tmp_path):
        """Path rejected if outside all approved directories."""
        root1 = tmp_path / "root1"
        root2 = tmp_path / "root2"
        outside = tmp_path / "outside"
        root1.mkdir()
        root2.mkdir()
        outside.mkdir()
        validator = SecurityValidator(approved_directories=[root1, root2])
        is_valid, _, err = validator.validate_path(str(outside / "file.py"), root1)
        assert not is_valid

    def test_backward_compat_single_directory(self, tmp_path):
        """Single approved_directory still works via positional arg."""
        root = tmp_path / "root"
        root.mkdir()
        (root / "file.py").touch()
        validator = SecurityValidator(root)
        is_valid, resolved, _ = validator.validate_path("file.py", root)
        assert is_valid
```

**Step 2: Run tests to verify they fail**

Run: `cd /local/home/moxu/claude-coder && python -m pytest tests/unit/test_security/test_validators.py::TestMultiRootValidation -v`
Expected: FAIL — `approved_directories` kwarg not accepted

**Step 3: Update SecurityValidator.__init__ and validate_path**

In `src/security/validators.py`, update `__init__` (~line 134):

```python
    def __init__(
        self,
        approved_directory: Optional[Path] = None,
        disable_security_patterns: bool = False,
        approved_directories: Optional[list[Path]] = None,
    ) -> None:
        if approved_directories:
            self.approved_directories = [Path(d).resolve() for d in approved_directories]
        elif approved_directory:
            self.approved_directories = [Path(approved_directory).resolve()]
        else:
            raise ValueError("Must provide approved_directory or approved_directories")
        # Backward compat
        self.approved_directory = self.approved_directories[0]
        self.disable_security_patterns = disable_security_patterns
```

Update `validate_path` (~line 146) — change the boundary check (~line 190-197):

```python
        # Check against all approved directories
        within_any = any(
            self._is_within_directory(resolved_path, root)
            for root in self.approved_directories
        )
        if not within_any:
            return (False, None, f"Path is outside all approved directories")
```

Update `_is_within_directory` — no change needed (already generic).

**Step 4: Run tests to verify they pass**

Run: `cd /local/home/moxu/claude-coder && python -m pytest tests/unit/test_security/test_validators.py -v`
Expected: ALL PASS (new + existing)

**Step 5: Update SDK can_use_tool callback**

In `src/claude/sdk_integration.py`, update `_make_can_use_tool_callback()` (~line 135). The callback uses `self.security_validator.validate_path()` which now handles multi-root. Also update `check_bash_directory_boundary()` if it hardcodes a single root — pass all approved directories.

**Step 6: Run full test suite**

Run: `cd /local/home/moxu/claude-coder && python -m pytest tests/ -v`
Expected: ALL PASS

**Step 7: Commit**

```bash
git add src/security/validators.py src/claude/sdk_integration.py tests/unit/test_security/
git commit -m "feat: update SecurityValidator for multi-root directory support"
```

---

### Task 3: Update `/repo` command for multi-root + persist working directory

**Files:**
- Modify: `src/bot/orchestrator.py:1077-1171` (agentic_repo)
- Modify: `src/storage/database.py` (add migration for users.current_directory)
- Test: `tests/unit/test_bot/test_orchestrator.py`

**Step 1: Write the failing test for directory persistence**

```python
class TestWorkingDirectoryPersistence:
    """Tests for persisting current_directory across restarts."""

    @pytest.mark.asyncio
    async def test_current_directory_saved_to_db(self, setup_bot):
        """Switching dirs persists to database."""
        # ... test that after /repo switch, current_directory is in SQLite

    @pytest.mark.asyncio
    async def test_current_directory_restored_on_startup(self, setup_bot):
        """Current directory restored from DB when user first interacts."""
        # ... test that user_data["current_directory"] is loaded from DB
```

**Step 2: Run tests to verify they fail**

Run: `cd /local/home/moxu/claude-coder && python -m pytest tests/unit/test_bot/test_orchestrator.py::TestWorkingDirectoryPersistence -v`
Expected: FAIL

**Step 3: Add database migration**

In `src/storage/database.py`, add Migration 5 to the migrations list (~line 218):

```python
    # Migration 5: Add current_directory to users table
    (
        5,
        """
        ALTER TABLE users ADD COLUMN current_directory TEXT;
        """,
    ),
```

**Step 4: Update agentic_repo for multi-root**

In `src/bot/orchestrator.py`, update `agentic_repo()` (~line 1077):

- When no arg: list all workspace roots from `settings.approved_directories`, then subdirs of each
- Group buttons by root:
  ```
  📁 Workspaces
  [myapp]  [infra]  [work]

  📂 myapp/
  [backend]  [frontend]
  ```
- On directory switch: persist `current_directory` to the `users` table

**Step 5: Add storage method for directory persistence**

In `src/storage/session_storage.py`, add:

```python
    async def save_user_directory(self, user_id: int, directory: str) -> None:
        """Persist user's current working directory."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                "UPDATE users SET current_directory = ? WHERE user_id = ?",
                (directory, user_id),
            )
            await conn.commit()

    async def load_user_directory(self, user_id: int) -> Optional[str]:
        """Load user's persisted working directory."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT current_directory FROM users WHERE user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
            return row[0] if row and row[0] else None
```

**Step 6: Run tests**

Run: `cd /local/home/moxu/claude-coder && python -m pytest tests/ -v`
Expected: ALL PASS

**Step 7: Commit**

```bash
git add src/bot/orchestrator.py src/storage/database.py src/storage/session_storage.py tests/
git commit -m "feat: multi-root /repo with persistent working directory"
```

---

## Phase 2: Session Picker

### Task 4: Create history.jsonl reader

**Files:**
- Create: `src/claude/history.py`
- Test: `tests/unit/test_claude/test_history.py`

**Step 1: Write the failing tests**

Create `tests/unit/test_claude/test_history.py`:

```python
import json
from datetime import datetime, UTC
from pathlib import Path
from unittest.mock import patch

import pytest

from src.claude.history import HistoryEntry, read_claude_history, filter_by_directory


class TestReadClaudeHistory:
    """Tests for reading ~/.claude/history.jsonl."""

    def test_reads_valid_entries(self, tmp_path):
        """Parses well-formed history.jsonl lines."""
        history_file = tmp_path / "history.jsonl"
        history_file.write_text(
            json.dumps({
                "display": "fix auth bug",
                "timestamp": 1740000000000,
                "project": "/home/user/myapp",
                "sessionId": "abc-123",
            }) + "\n"
            + json.dumps({
                "display": "add tests",
                "timestamp": 1740100000000,
                "project": "/home/user/myapp",
                "sessionId": "def-456",
            }) + "\n"
        )
        entries = read_claude_history(history_file)
        assert len(entries) == 2
        assert entries[0].session_id == "abc-123"
        assert entries[0].display == "fix auth bug"
        assert entries[1].session_id == "def-456"

    def test_skips_malformed_lines(self, tmp_path):
        """Malformed lines are skipped, not crashed on."""
        history_file = tmp_path / "history.jsonl"
        history_file.write_text(
            "not json\n"
            + json.dumps({
                "display": "valid",
                "timestamp": 1740000000000,
                "project": "/home/user/myapp",
                "sessionId": "abc-123",
            }) + "\n"
            + '{"missing": "sessionId"}\n'
        )
        entries = read_claude_history(history_file)
        assert len(entries) == 1
        assert entries[0].session_id == "abc-123"

    def test_missing_file_returns_empty(self, tmp_path):
        """Returns empty list if history.jsonl doesn't exist."""
        entries = read_claude_history(tmp_path / "nonexistent.jsonl")
        assert entries == []

    def test_filter_by_directory(self, tmp_path):
        """Filters entries to matching project directory."""
        entries = [
            HistoryEntry(
                session_id="abc-123",
                display="fix auth",
                timestamp=1740000000000,
                project="/home/user/myapp",
            ),
            HistoryEntry(
                session_id="def-456",
                display="add tests",
                timestamp=1740100000000,
                project="/home/user/other",
            ),
        ]
        filtered = filter_by_directory(entries, Path("/home/user/myapp"))
        assert len(filtered) == 1
        assert filtered[0].session_id == "abc-123"

    def test_entries_sorted_by_timestamp_descending(self, tmp_path):
        """Entries returned newest first."""
        history_file = tmp_path / "history.jsonl"
        history_file.write_text(
            json.dumps({
                "display": "old",
                "timestamp": 1740000000000,
                "project": "/home/user/myapp",
                "sessionId": "old-1",
            }) + "\n"
            + json.dumps({
                "display": "new",
                "timestamp": 1740200000000,
                "project": "/home/user/myapp",
                "sessionId": "new-1",
            }) + "\n"
        )
        entries = read_claude_history(history_file)
        assert entries[0].session_id == "new-1"
        assert entries[1].session_id == "old-1"
```

**Step 2: Run tests to verify they fail**

Run: `cd /local/home/moxu/claude-coder && python -m pytest tests/unit/test_claude/test_history.py -v`
Expected: FAIL — module doesn't exist

**Step 3: Implement history.py**

Create `src/claude/history.py`:

```python
"""Reader for Claude Code's ~/.claude/history.jsonl session history."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

DEFAULT_HISTORY_PATH = Path.home() / ".claude" / "history.jsonl"


@dataclass(frozen=True)
class HistoryEntry:
    """A single session entry from Claude Code's history."""

    session_id: str
    display: str
    timestamp: int  # milliseconds since epoch
    project: str


def read_claude_history(
    history_path: Path = DEFAULT_HISTORY_PATH,
) -> list[HistoryEntry]:
    """Read and parse Claude Code's history.jsonl.

    Returns entries sorted by timestamp descending (newest first).
    Skips malformed lines with a warning.
    """
    if not history_path.exists():
        logger.info("claude_history_not_found", path=str(history_path))
        return []

    entries: list[HistoryEntry] = []
    with open(history_path, "r") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                entry = HistoryEntry(
                    session_id=data["sessionId"],
                    display=data.get("display", ""),
                    timestamp=data["timestamp"],
                    project=data["project"],
                )
                entries.append(entry)
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.warning(
                    "claude_history_malformed_line",
                    line_num=line_num,
                    error=str(e),
                )
                continue

    entries.sort(key=lambda e: e.timestamp, reverse=True)
    return entries


def filter_by_directory(
    entries: list[HistoryEntry], directory: Path
) -> list[HistoryEntry]:
    """Filter history entries to those matching a specific project directory."""
    resolved = str(directory.resolve())
    return [e for e in entries if e.project == resolved]
```

**Step 4: Run tests to verify they pass**

Run: `cd /local/home/moxu/claude-coder && python -m pytest tests/unit/test_claude/test_history.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/claude/history.py tests/unit/test_claude/test_history.py
git commit -m "feat: add Claude Code history.jsonl reader"
```

---

### Task 5: Add `bot_sessions` table and sync logic

**Files:**
- Modify: `src/storage/database.py` (add migration)
- Create: `src/storage/bot_session_storage.py`
- Test: `tests/unit/test_storage/test_bot_session_storage.py`

**Step 1: Write the failing tests**

Create `tests/unit/test_storage/test_bot_session_storage.py`:

```python
import pytest
from datetime import datetime, UTC
from pathlib import Path

from src.storage.bot_session_storage import BotSessionStorage, BotSessionRecord
from src.storage.database import DatabaseManager


class TestBotSessionStorage:
    """Tests for the thin bot_sessions registry."""

    @pytest.fixture
    async def storage(self, tmp_path):
        db = DatabaseManager(f"sqlite:///{tmp_path}/test.db")
        await db.initialize()
        return BotSessionStorage(db)

    @pytest.mark.asyncio
    async def test_upsert_and_list(self, storage):
        """Can save and retrieve sessions for a user+directory."""
        record = BotSessionRecord(
            session_id="abc-123",
            user_id=1,
            directory="/home/user/myapp",
            display_name="fix auth bug",
            first_seen_at=datetime.now(UTC),
            last_used_at=datetime.now(UTC),
            source="bot",
        )
        await storage.upsert(record)
        sessions = await storage.list_for_directory(user_id=1, directory="/home/user/myapp")
        assert len(sessions) == 1
        assert sessions[0].session_id == "abc-123"
        assert sessions[0].display_name == "fix auth bug"

    @pytest.mark.asyncio
    async def test_sync_from_history(self, storage):
        """Sync merges history.jsonl entries into bot_sessions."""
        from src.claude.history import HistoryEntry

        entries = [
            HistoryEntry(
                session_id="from-cli-1",
                display="cli session",
                timestamp=1740000000000,
                project="/home/user/myapp",
            ),
        ]
        await storage.sync_from_history(entries, user_id=1)
        sessions = await storage.list_for_directory(user_id=1, directory="/home/user/myapp")
        assert len(sessions) == 1
        assert sessions[0].source == "cli"

    @pytest.mark.asyncio
    async def test_sync_does_not_duplicate(self, storage):
        """Syncing the same entry twice doesn't create duplicates."""
        from src.claude.history import HistoryEntry

        entry = HistoryEntry(
            session_id="abc-123",
            display="some work",
            timestamp=1740000000000,
            project="/home/user/myapp",
        )
        await storage.sync_from_history([entry], user_id=1)
        await storage.sync_from_history([entry], user_id=1)
        sessions = await storage.list_for_directory(user_id=1, directory="/home/user/myapp")
        assert len(sessions) == 1
```

**Step 2: Run tests to verify they fail**

Run: `cd /local/home/moxu/claude-coder && python -m pytest tests/unit/test_storage/test_bot_session_storage.py -v`
Expected: FAIL — module doesn't exist

**Step 3: Add database migration**

In `src/storage/database.py`, add Migration 6:

```python
    # Migration 6: bot_sessions registry for session picker
    (
        6,
        """
        CREATE TABLE IF NOT EXISTS bot_sessions (
            session_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            directory TEXT NOT NULL,
            display_name TEXT DEFAULT '',
            first_seen_at TIMESTAMP NOT NULL,
            last_used_at TIMESTAMP NOT NULL,
            source TEXT DEFAULT 'bot'
        );
        CREATE INDEX IF NOT EXISTS idx_bot_sessions_user_dir
            ON bot_sessions(user_id, directory);
        """,
    ),
```

**Step 4: Implement BotSessionStorage**

Create `src/storage/bot_session_storage.py`:

```python
"""Thin session registry for the session picker UI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, UTC
from typing import Optional

from src.claude.history import HistoryEntry
from src.storage.database import DatabaseManager


@dataclass(frozen=True)
class BotSessionRecord:
    """A session record in the bot's registry."""

    session_id: str
    user_id: int
    directory: str
    display_name: str
    first_seen_at: datetime
    last_used_at: datetime
    source: str  # 'bot' or 'cli'


class BotSessionStorage:
    """Storage layer for the bot_sessions table."""

    def __init__(self, db: DatabaseManager) -> None:
        self.db = db

    async def upsert(self, record: BotSessionRecord) -> None:
        """Insert or update a session record."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO bot_sessions
                    (session_id, user_id, directory, display_name, first_seen_at, last_used_at, source)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    last_used_at = excluded.last_used_at,
                    display_name = COALESCE(NULLIF(excluded.display_name, ''), display_name)
                """,
                (
                    record.session_id,
                    record.user_id,
                    record.directory,
                    record.display_name,
                    record.first_seen_at.isoformat(),
                    record.last_used_at.isoformat(),
                    record.source,
                ),
            )
            await conn.commit()

    async def list_for_directory(
        self, user_id: int, directory: str
    ) -> list[BotSessionRecord]:
        """List sessions for a user+directory, newest first."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT session_id, user_id, directory, display_name,
                       first_seen_at, last_used_at, source
                FROM bot_sessions
                WHERE user_id = ? AND directory = ?
                ORDER BY last_used_at DESC
                """,
                (user_id, directory),
            )
            rows = await cursor.fetchall()
            return [
                BotSessionRecord(
                    session_id=row[0],
                    user_id=row[1],
                    directory=row[2],
                    display_name=row[3],
                    first_seen_at=datetime.fromisoformat(row[4]) if isinstance(row[4], str) else row[4],
                    last_used_at=datetime.fromisoformat(row[5]) if isinstance(row[5], str) else row[5],
                    source=row[6],
                )
                for row in rows
            ]

    async def sync_from_history(
        self, entries: list[HistoryEntry], user_id: int
    ) -> None:
        """Merge history.jsonl entries into bot_sessions."""
        for entry in entries:
            ts = datetime.fromtimestamp(entry.timestamp / 1000, tz=UTC)
            record = BotSessionRecord(
                session_id=entry.session_id,
                user_id=user_id,
                directory=entry.project,
                display_name=entry.display,
                first_seen_at=ts,
                last_used_at=ts,
                source="cli",
            )
            await self.upsert(record)
```

**Step 5: Run tests to verify they pass**

Run: `cd /local/home/moxu/claude-coder && python -m pytest tests/unit/test_storage/test_bot_session_storage.py -v`
Expected: PASS

**Step 6: Commit**

```bash
git add src/storage/database.py src/storage/bot_session_storage.py tests/unit/test_storage/test_bot_session_storage.py
git commit -m "feat: add bot_sessions table and sync from history.jsonl"
```

---

### Task 6: Add `/sessions` command with inline keyboard

**Files:**
- Modify: `src/bot/orchestrator.py` (add handler + register)
- Test: `tests/unit/test_bot/test_orchestrator.py`

**Step 1: Write the failing test**

```python
class TestSessionsCommand:
    """Tests for /sessions inline keyboard."""

    @pytest.mark.asyncio
    async def test_sessions_shows_picker(self, setup_bot, mock_update):
        """Shows inline keyboard with available sessions."""
        # Setup: mock bot_session_storage.list_for_directory returns 2 sessions
        # Call agentic_sessions handler
        # Assert: reply contains InlineKeyboardMarkup with 2 session buttons + New Session

    @pytest.mark.asyncio
    async def test_sessions_empty_shows_new_only(self, setup_bot, mock_update):
        """No sessions available shows only New Session button."""

    @pytest.mark.asyncio
    async def test_session_callback_resumes(self, setup_bot, mock_callback_query):
        """Tapping a session button sets session_id in context."""
```

**Step 2: Run tests to verify they fail**

Run: `cd /local/home/moxu/claude-coder && python -m pytest tests/unit/test_bot/test_orchestrator.py::TestSessionsCommand -v`
Expected: FAIL

**Step 3: Implement /sessions handler**

In `src/bot/orchestrator.py`, add `agentic_sessions()` method:

```python
    async def agentic_sessions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show session picker for current directory."""
        user = update.effective_user
        current_dir = context.user_data.get("current_directory", self.settings.approved_directory)

        # Sync from history.jsonl
        bot_session_storage = context.bot_data["bot_session_storage"]
        history_entries = read_claude_history()
        dir_entries = filter_by_directory(history_entries, Path(current_dir))
        await bot_session_storage.sync_from_history(dir_entries, user_id=user.id)

        # Get merged list
        sessions = await bot_session_storage.list_for_directory(
            user_id=user.id, directory=str(current_dir)
        )

        # Build inline keyboard
        buttons = []
        for s in sessions[:10]:  # Cap at 10 buttons
            ts = s.last_used_at.strftime("%b %d")
            label = f"{ts} — {s.display_name[:45]}" if s.display_name else f"{ts} — (unnamed)"
            buttons.append([InlineKeyboardButton(label, callback_data=f"session:{s.session_id}")])

        buttons.append([InlineKeyboardButton("+ New Session", callback_data="session:new")])

        await update.message.reply_text(
            f"Sessions in `{current_dir}`:",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown",
        )
```

Register in `_register_agentic_handlers()` (~line 286):

```python
    ("sessions", self.agentic_sessions),
```

Add callback handling in `_agentic_callback()` (~line 1173) for `session:` prefix:

```python
        if data.startswith("session:"):
            session_id = data[len("session:"):]
            if session_id == "new":
                context.user_data["force_new_session"] = True
                await query.edit_message_text("Starting new session. Send a message to begin.")
            else:
                context.user_data["claude_session_id"] = session_id
                await query.edit_message_text(f"Resumed session. Send a message to continue.")
            return
```

Update callback handler pattern (~line 324) to match both `cd:` and `session:`:

```python
    CallbackQueryHandler(self._agentic_callback, pattern=r"^(cd:|session:)")
```

**Step 4: Update auto-resume to show picker when 2+ sessions**

In `src/claude/facade.py`, update `_find_resumable_session()` (~line 165) to return all matching sessions instead of just the latest. Let the orchestrator decide whether to auto-resume or show picker.

Add a new method:

```python
    def find_resumable_sessions(self, user_id: int, working_directory: Path) -> list[ClaudeSession]:
        """Find all resumable sessions for a user+directory."""
        # Filter active sessions by project_path and non-expired
        ...
```

In the orchestrator's `agentic_text()`, before calling `run_command()`:
- If no `session_id` set and not `force_new`:
  - Count available sessions for this directory
  - If 2+: show session picker, return (don't execute yet)
  - If 1: auto-resume as before
  - If 0: start new

**Step 5: Add to get_bot_commands()**

In `get_bot_commands()` (~line 381), add:

```python
    BotCommand("sessions", "Choose a session to resume"),
```

**Step 6: Run tests**

Run: `cd /local/home/moxu/claude-coder && python -m pytest tests/ -v`
Expected: ALL PASS

**Step 7: Commit**

```bash
git add src/bot/orchestrator.py src/claude/facade.py tests/
git commit -m "feat: add /sessions command with inline keyboard picker"
```

---

## Phase 3: Dynamic Skill Loading

### Task 7: Create skill discovery and parsing module

**Files:**
- Create: `src/skills/__init__.py`
- Create: `src/skills/loader.py`
- Test: `tests/unit/test_skills/test_loader.py`

**Step 1: Write the failing tests**

Create `tests/unit/test_skills/__init__.py` (empty) and `tests/unit/test_skills/test_loader.py`:

```python
import pytest
from pathlib import Path

from src.skills.loader import (
    SkillMetadata,
    discover_skills,
    load_skill_body,
    resolve_skill_prompt,
)


class TestDiscoverSkills:
    """Tests for filesystem skill discovery."""

    def test_discovers_project_skills(self, tmp_path):
        """Finds SKILL.md files in .claude/skills/."""
        skill_dir = tmp_path / ".claude" / "skills" / "review"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: review\n"
            "description: Review code\n"
            "user-invocable: true\n"
            "argument-hint: '[file]'\n"
            "---\n"
            "Review $ARGUMENTS for issues.\n"
        )
        skills = discover_skills(project_dir=tmp_path)
        assert len(skills) == 1
        assert skills[0].name == "review"
        assert skills[0].description == "Review code"
        assert skills[0].argument_hint == "[file]"
        assert skills[0].user_invocable is True
        assert skills[0].source == "project"

    def test_discovers_personal_skills(self, tmp_path, monkeypatch):
        """Finds skills in ~/.claude/skills/."""
        personal_dir = tmp_path / "home_claude" / "skills" / "commit"
        personal_dir.mkdir(parents=True)
        (personal_dir / "SKILL.md").write_text(
            "---\n"
            "name: commit\n"
            "description: Smart commit\n"
            "---\n"
            "Create a commit message.\n"
        )
        skills = discover_skills(
            project_dir=tmp_path / "project",
            personal_skills_dir=tmp_path / "home_claude" / "skills",
        )
        assert len(skills) == 1
        assert skills[0].name == "commit"
        assert skills[0].source == "personal"

    def test_skips_non_invocable_skills(self, tmp_path):
        """Skills with user-invocable: false are excluded."""
        skill_dir = tmp_path / ".claude" / "skills" / "hidden"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: hidden\n"
            "user-invocable: false\n"
            "---\n"
            "Internal skill.\n"
        )
        skills = discover_skills(project_dir=tmp_path)
        assert len(skills) == 0

    def test_discovers_legacy_commands(self, tmp_path):
        """Finds .claude/commands/*.md files."""
        cmd_dir = tmp_path / ".claude" / "commands"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "deploy.md").write_text("Deploy the app to staging.\n")
        skills = discover_skills(project_dir=tmp_path)
        assert len(skills) == 1
        assert skills[0].name == "deploy"
        assert skills[0].source == "legacy_project"

    def test_malformed_frontmatter_skipped(self, tmp_path):
        """Skills with unparseable frontmatter are skipped."""
        skill_dir = tmp_path / ".claude" / "skills" / "broken"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: [invalid yaml\n"
            "---\n"
            "Content.\n"
        )
        skills = discover_skills(project_dir=tmp_path)
        assert len(skills) == 0


class TestLoadSkillBody:
    """Tests for loading full skill content."""

    def test_loads_body_without_frontmatter(self, tmp_path):
        """Returns content after the YAML frontmatter."""
        skill_dir = tmp_path / ".claude" / "skills" / "review"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: review\n"
            "---\n"
            "Review the code.\n"
            "Check for bugs.\n"
        )
        metadata = SkillMetadata(
            name="review",
            description="",
            argument_hint=None,
            user_invocable=True,
            allowed_tools=[],
            source="project",
            file_path=skill_dir / "SKILL.md",
        )
        body = load_skill_body(metadata)
        assert body == "Review the code.\nCheck for bugs.\n"

    def test_loads_legacy_command_as_full_body(self, tmp_path):
        """Legacy .md commands have no frontmatter — return entire content."""
        cmd_dir = tmp_path / ".claude" / "commands"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "deploy.md").write_text("Deploy $ARGUMENTS to staging.\n")
        metadata = SkillMetadata(
            name="deploy",
            description="",
            argument_hint=None,
            user_invocable=True,
            allowed_tools=[],
            source="legacy_project",
            file_path=cmd_dir / "deploy.md",
        )
        body = load_skill_body(metadata)
        assert body == "Deploy $ARGUMENTS to staging.\n"


class TestResolveSkillPrompt:
    """Tests for $ARGUMENTS substitution."""

    def test_replaces_arguments(self):
        body = "Review $ARGUMENTS for issues."
        result = resolve_skill_prompt(body, arguments="src/auth.py", session_id="s1")
        assert result == "Review src/auth.py for issues."

    def test_replaces_positional_arguments(self):
        body = "Compare $ARGUMENTS[0] with $ARGUMENTS[1]."
        result = resolve_skill_prompt(body, arguments="old.py new.py", session_id="s1")
        assert result == "Compare old.py with new.py."

    def test_replaces_dollar_positional(self):
        body = "Fix $0 in $1."
        result = resolve_skill_prompt(body, arguments="bug main.py", session_id="s1")
        assert result == "Fix bug in main.py."

    def test_replaces_session_id(self):
        body = "Session: ${CLAUDE_SESSION_ID}"
        result = resolve_skill_prompt(body, arguments="", session_id="abc-123")
        assert result == "Session: abc-123"

    def test_no_arguments_leaves_placeholder(self):
        body = "Do the thing with $ARGUMENTS."
        result = resolve_skill_prompt(body, arguments="", session_id="s1")
        assert result == "Do the thing with ."
```

**Step 2: Run tests to verify they fail**

Run: `cd /local/home/moxu/claude-coder && python -m pytest tests/unit/test_skills/test_loader.py -v`
Expected: FAIL — module doesn't exist

**Step 3: Implement skills/loader.py**

Create `src/skills/__init__.py` (empty) and `src/skills/loader.py`:

```python
"""Skill discovery and loading for Claude Code custom skills."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import structlog
import yaml

logger = structlog.get_logger(__name__)

DEFAULT_PERSONAL_SKILLS_DIR = Path.home() / ".claude" / "skills"
DEFAULT_PERSONAL_COMMANDS_DIR = Path.home() / ".claude" / "commands"


@dataclass(frozen=True)
class SkillMetadata:
    """Parsed metadata from a SKILL.md frontmatter."""

    name: str
    description: str
    argument_hint: Optional[str]
    user_invocable: bool
    allowed_tools: list[str]
    source: str  # 'project' | 'personal' | 'legacy_project' | 'legacy_personal'
    file_path: Path


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """Split YAML frontmatter from markdown body.

    Returns (frontmatter_dict, body_text).
    If no frontmatter found, returns ({}, full_content).
    """
    if not content.startswith("---"):
        return {}, content

    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content

    try:
        fm = yaml.safe_load(parts[1])
        if not isinstance(fm, dict):
            return {}, content
        body = parts[2].lstrip("\n")
        return fm, body
    except yaml.YAMLError:
        return {}, content


def _scan_skills_dir(
    skills_dir: Path, source: str
) -> list[SkillMetadata]:
    """Scan a skills/ directory for SKILL.md files."""
    results: list[SkillMetadata] = []
    if not skills_dir.exists():
        return results

    for skill_path in sorted(skills_dir.iterdir()):
        if not skill_path.is_dir():
            continue
        skill_file = skill_path / "SKILL.md"
        if not skill_file.exists():
            continue

        try:
            content = skill_file.read_text()
            fm, _ = _parse_frontmatter(content)
            name = fm.get("name", skill_path.name)
            user_invocable = fm.get("user-invocable", True)
            if not user_invocable:
                continue

            allowed_tools_raw = fm.get("allowed-tools", "")
            allowed_tools = (
                [t.strip() for t in allowed_tools_raw.split(",") if t.strip()]
                if isinstance(allowed_tools_raw, str)
                else []
            )

            results.append(
                SkillMetadata(
                    name=name,
                    description=fm.get("description", ""),
                    argument_hint=fm.get("argument-hint"),
                    user_invocable=True,
                    allowed_tools=allowed_tools,
                    source=source,
                    file_path=skill_file,
                )
            )
        except Exception as e:
            logger.warning("skill_parse_error", path=str(skill_file), error=str(e))
            continue

    return results


def _scan_legacy_commands_dir(
    commands_dir: Path, source: str
) -> list[SkillMetadata]:
    """Scan a commands/ directory for legacy .md files."""
    results: list[SkillMetadata] = []
    if not commands_dir.exists():
        return results

    for cmd_file in sorted(commands_dir.glob("*.md")):
        name = cmd_file.stem
        results.append(
            SkillMetadata(
                name=name,
                description="",
                argument_hint=None,
                user_invocable=True,
                allowed_tools=[],
                source=source,
                file_path=cmd_file,
            )
        )

    return results


def discover_skills(
    project_dir: Path,
    personal_skills_dir: Path = DEFAULT_PERSONAL_SKILLS_DIR,
    personal_commands_dir: Path = DEFAULT_PERSONAL_COMMANDS_DIR,
) -> list[SkillMetadata]:
    """Discover all available skills from filesystem.

    Scan order (matches Claude Code CLI priority):
    1. Project skills: {project}/.claude/skills/
    2. Personal skills: ~/.claude/skills/
    3. Legacy project commands: {project}/.claude/commands/
    4. Legacy personal commands: ~/.claude/commands/
    """
    skills: list[SkillMetadata] = []

    skills.extend(_scan_skills_dir(project_dir / ".claude" / "skills", "project"))
    skills.extend(_scan_skills_dir(personal_skills_dir, "personal"))
    skills.extend(
        _scan_legacy_commands_dir(project_dir / ".claude" / "commands", "legacy_project")
    )
    skills.extend(_scan_legacy_commands_dir(personal_commands_dir, "legacy_personal"))

    # Deduplicate by name (first seen wins, matching CLI priority)
    seen: set[str] = set()
    unique: list[SkillMetadata] = []
    for s in skills:
        if s.name not in seen:
            seen.add(s.name)
            unique.append(s)

    return unique


def load_skill_body(skill: SkillMetadata) -> str:
    """Load the full body content of a skill (without frontmatter)."""
    content = skill.file_path.read_text()
    if skill.source.startswith("legacy_"):
        return content
    _, body = _parse_frontmatter(content)
    return body


def resolve_skill_prompt(
    body: str, arguments: str, session_id: str
) -> str:
    """Substitute variables in a skill prompt template."""
    args_list = arguments.split() if arguments else []

    # Replace $ARGUMENTS[N] and $N (positional)
    def replace_positional(match: re.Match) -> str:
        idx = int(match.group(1))
        return args_list[idx] if idx < len(args_list) else ""

    result = re.sub(r"\$ARGUMENTS\[(\d+)\]", replace_positional, body)
    result = re.sub(r"\$(\d+)", replace_positional, result)

    # Replace $ARGUMENTS (full string) — must come after positional
    result = result.replace("$ARGUMENTS", arguments)

    # Replace ${CLAUDE_SESSION_ID}
    result = result.replace("${CLAUDE_SESSION_ID}", session_id)

    return result
```

**Step 4: Run tests to verify they pass**

Run: `cd /local/home/moxu/claude-coder && python -m pytest tests/unit/test_skills/test_loader.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/skills/ tests/unit/test_skills/
git commit -m "feat: add skill discovery, parsing, and prompt resolution"
```

---

### Task 8: Add `/commands` command with inline keyboard

**Files:**
- Modify: `src/bot/orchestrator.py`
- Test: `tests/unit/test_bot/test_orchestrator.py`

**Step 1: Write the failing test**

```python
class TestCommandsCommand:
    """Tests for /commands inline keyboard."""

    @pytest.mark.asyncio
    async def test_commands_shows_skills(self, setup_bot, mock_update, tmp_path):
        """Shows inline keyboard with discovered skills."""
        # Setup: create mock skill files in tmp_path
        # Call agentic_commands handler
        # Assert: reply contains InlineKeyboardMarkup with skill buttons

    @pytest.mark.asyncio
    async def test_commands_no_arg_skill_uses_callback(self, setup_bot):
        """Skills without argument-hint use callback_data."""

    @pytest.mark.asyncio
    async def test_commands_arg_skill_uses_switch_inline(self, setup_bot):
        """Skills with argument-hint use switch_inline_query_current_chat."""

    @pytest.mark.asyncio
    async def test_skill_callback_executes(self, setup_bot, mock_callback_query):
        """Tapping a no-arg skill button sends prompt to Claude."""
```

**Step 2: Run tests to verify they fail**

**Step 3: Implement /commands handler**

In `src/bot/orchestrator.py`, add `agentic_commands()`:

```python
    async def agentic_commands(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show available skills as inline keyboard buttons."""
        current_dir = context.user_data.get("current_directory", self.settings.approved_directory)

        skills = discover_skills(project_dir=Path(current_dir))

        if not skills:
            await update.message.reply_text(
                "No skills found.\n"
                "Create one at `.claude/skills/<name>/SKILL.md`"
            )
            return

        # Group by source
        project_skills = [s for s in skills if s.source in ("project", "legacy_project")]
        personal_skills = [s for s in skills if s.source in ("personal", "legacy_personal")]

        buttons = []
        if project_skills:
            for s in project_skills:
                label = f"{s.name} ..." if s.argument_hint else s.name
                if s.argument_hint:
                    buttons.append([InlineKeyboardButton(
                        label,
                        switch_inline_query_current_chat=f"/{s.name} ",
                    )])
                else:
                    buttons.append([InlineKeyboardButton(
                        label,
                        callback_data=f"skill:{s.name}",
                    )])

        if personal_skills:
            for s in personal_skills:
                label = f"{s.name} ..." if s.argument_hint else s.name
                if s.argument_hint:
                    buttons.append([InlineKeyboardButton(
                        label,
                        switch_inline_query_current_chat=f"/{s.name} ",
                    )])
                else:
                    buttons.append([InlineKeyboardButton(
                        label,
                        callback_data=f"skill:{s.name}",
                    )])

        header_parts = []
        if project_skills:
            header_parts.append(f"📁 *Project Skills* ({len(project_skills)})")
        if personal_skills:
            header_parts.append(f"🌐 *Personal Skills* ({len(personal_skills)})")

        await update.message.reply_text(
            "\n".join(header_parts),
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown",
        )
```

Add callback handling for `skill:` prefix:

```python
        if data.startswith("skill:"):
            skill_name = data[len("skill:"):]
            current_dir = context.user_data.get("current_directory", self.settings.approved_directory)
            skills = discover_skills(project_dir=Path(current_dir))
            skill = next((s for s in skills if s.name == skill_name), None)
            if not skill:
                await query.edit_message_text(f"Skill '{skill_name}' not found.")
                return
            body = load_skill_body(skill)
            session_id = context.user_data.get("claude_session_id", "")
            prompt = resolve_skill_prompt(body, arguments="", session_id=session_id)
            await query.edit_message_text(f"Running skill: {skill_name}...")
            # Execute via Claude — reuse agentic_text flow
            # ... send prompt through claude_integration.run_command()
```

Add text handler for skill invocations (when user sends `/<skill-name> args` after pre-fill). In `agentic_text()`, before sending to Claude:

```python
        # Check if text matches a skill invocation (from switch_inline_query_current_chat)
        if text.startswith("/") and not text.startswith("/start"):
            parts = text.split(maxsplit=1)
            skill_name = parts[0].lstrip("/")
            skill_args = parts[1] if len(parts) > 1 else ""
            skills = discover_skills(project_dir=Path(current_dir))
            skill = next((s for s in skills if s.name == skill_name), None)
            if skill:
                body = load_skill_body(skill)
                session_id = context.user_data.get("claude_session_id", "")
                text = resolve_skill_prompt(body, arguments=skill_args, session_id=session_id)
```

Register command and update callback pattern:

```python
    ("commands", self.agentic_commands),
```

```python
    CallbackQueryHandler(self._agentic_callback, pattern=r"^(cd:|session:|skill:)")
```

Add to `get_bot_commands()`:

```python
    BotCommand("commands", "Browse available skills"),
```

**Step 4: Run tests**

Run: `cd /local/home/moxu/claude-coder && python -m pytest tests/ -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add src/bot/orchestrator.py tests/
git commit -m "feat: add /commands with skill discovery and inline keyboard"
```

---

## Phase 4: /compact Command

### Task 9: Implement `/compact`

**Files:**
- Modify: `src/bot/orchestrator.py`
- Modify: `src/claude/facade.py`
- Test: `tests/unit/test_bot/test_orchestrator.py`

**Step 1: Write the failing test**

```python
class TestCompactCommand:
    """Tests for /compact session compression."""

    @pytest.mark.asyncio
    async def test_compact_creates_new_session_with_summary(self, setup_bot, mock_update):
        """Compact summarizes then starts new session seeded with summary."""
        # Setup: mock an active session with session_id
        # Call agentic_compact
        # Assert: run_command called with summary prompt, force_new=True
        # Assert: user notified "Context compacted"

    @pytest.mark.asyncio
    async def test_compact_no_active_session(self, setup_bot, mock_update):
        """Compact with no session tells user there's nothing to compact."""
```

**Step 2: Run tests to verify they fail**

**Step 3: Implement /compact handler**

In `src/bot/orchestrator.py`:

```python
    async def agentic_compact(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Compress conversation context while keeping continuity."""
        session_id = context.user_data.get("claude_session_id")
        if not session_id:
            await update.message.reply_text("No active session to compact.")
            return

        current_dir = context.user_data.get("current_directory", self.settings.approved_directory)
        claude_integration = context.bot_data["claude_integration"]

        # Step 1: Ask Claude to summarize the current session
        await update.message.reply_text("Compacting context...")
        summary_response = await claude_integration.run_command(
            prompt=(
                "Summarize our conversation so far in a concise but comprehensive way. "
                "Include: key decisions made, current state of the work, any pending tasks, "
                "and important context I should remember. Format as bullet points."
            ),
            working_directory=current_dir,
            user_id=update.effective_user.id,
            session_id=session_id,
        )

        # Step 2: Start new session seeded with the summary
        context.user_data["force_new_session"] = True
        seed_response = await claude_integration.run_command(
            prompt=(
                f"This is a compacted session. Here is the context from our previous conversation:\n\n"
                f"{summary_response.response}\n\n"
                f"Please acknowledge this context briefly. We're continuing our work."
            ),
            working_directory=current_dir,
            user_id=update.effective_user.id,
            force_new=True,
        )

        # Step 3: Update session tracking
        context.user_data["force_new_session"] = False
        if seed_response.session_id:
            context.user_data["claude_session_id"] = seed_response.session_id

        await update.message.reply_text("Context compacted. Session continues with summary.")
```

Register:

```python
    ("compact", self.agentic_compact),
```

Add to `get_bot_commands()`:

```python
    BotCommand("compact", "Compress context, keep continuity"),
```

**Step 4: Run tests**

Run: `cd /local/home/moxu/claude-coder && python -m pytest tests/ -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add src/bot/orchestrator.py tests/
git commit -m "feat: add /compact command for session context compression"
```

---

## Phase 5: Wiring & Integration

### Task 10: Wire up dependencies and update main.py

**Files:**
- Modify: `src/main.py` (inject BotSessionStorage into bot_data)
- Modify: `src/bot/orchestrator.py` (update _inject_deps)
- Test: integration test

**Step 1: Update main.py**

In `src/main.py`, after database initialization, create and inject `BotSessionStorage`:

```python
    bot_session_storage = BotSessionStorage(database_manager)
    # Add to deps dict passed to orchestrator
```

In `src/bot/orchestrator.py`, update `_inject_deps()` (~line 106):

```python
    context.bot_data["bot_session_storage"] = self.deps.bot_session_storage
```

**Step 2: Update SecurityValidator construction**

Wherever `SecurityValidator` is created (likely `main.py` or orchestrator), pass `approved_directories`:

```python
    security_validator = SecurityValidator(
        approved_directories=settings.approved_directories
    )
```

**Step 3: Add format change notification**

In `src/claude/history.py`, add a function to detect unexpected format:

```python
def check_history_format_health(history_path: Path) -> Optional[str]:
    """Check if history.jsonl format looks healthy.

    Returns a warning message if format issues detected, None if OK.
    """
    if not history_path.exists():
        return None

    total_lines = 0
    failed_lines = 0
    with open(history_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            total_lines += 1
            try:
                data = json.loads(line)
                if "sessionId" not in data or "project" not in data:
                    failed_lines += 1
            except (json.JSONDecodeError, TypeError):
                failed_lines += 1

    if total_lines > 0 and failed_lines / total_lines > 0.5:
        return (
            f"Warning: {failed_lines}/{total_lines} lines in Claude Code session history "
            f"could not be parsed. The format may have changed. "
            f"/sessions may be incomplete."
        )
    return None
```

Call this from the `/sessions` handler and send the warning to the user if non-None.

**Step 4: Run full test suite**

Run: `cd /local/home/moxu/claude-coder && python -m pytest tests/ -v`
Expected: ALL PASS

**Step 5: Run linter**

Run: `cd /local/home/moxu/claude-coder && make lint`
Expected: PASS

**Step 6: Commit**

```bash
git add src/main.py src/bot/orchestrator.py src/claude/history.py
git commit -m "feat: wire up multi-root, sessions, commands, and compact"
```

---

## Phase 6: Update /status and /start

### Task 11: Update existing commands for new features

**Files:**
- Modify: `src/bot/orchestrator.py`

**Step 1: Update /start to list all commands**

Update `agentic_start()` (~line 416) to include new commands in the welcome message.

**Step 2: Update /status to show workspace info**

Update `agentic_status()` (~line 480) to show:
- Current workspace root (which approved directory)
- Current subdirectory
- Active session ID and display name
- Available sessions count for this directory

**Step 3: Run tests**

Run: `cd /local/home/moxu/claude-coder && python -m pytest tests/ -v`
Expected: ALL PASS

**Step 4: Commit**

```bash
git add src/bot/orchestrator.py
git commit -m "chore: update /start and /status for new features"
```

---

## Summary

| Phase | Tasks | What it delivers |
|-------|-------|-----------------|
| 1 | Tasks 1-3 | Multi-root directory navigation with persistence |
| 2 | Tasks 4-6 | Session picker with history.jsonl sync |
| 3 | Tasks 7-8 | Dynamic skill discovery and execution |
| 4 | Task 9 | /compact context compression |
| 5 | Task 10 | Dependency wiring and format health checks |
| 6 | Task 11 | Updated /start and /status |

**New commands:** `/sessions`, `/commands`, `/compact`
**Enhanced commands:** `/repo` (multi-root), `/start` (updated help), `/status` (richer info)
**New modules:** `src/skills/loader.py`, `src/claude/history.py`, `src/storage/bot_session_storage.py`
**New DB tables:** `bot_sessions` (migration 6), `users.current_directory` column (migration 5)
