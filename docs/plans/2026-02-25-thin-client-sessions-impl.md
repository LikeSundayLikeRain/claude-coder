# Thin Client Session Management — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Remove the bot's SQLite session tracking and make it a stateless pass-through that uses `~/.claude/history.jsonl` as the single source of truth, fixing CLI-to-bot session resume.

**Architecture:** The bot stores no session state in SQLite. Session discovery uses `history.jsonl` (shared with CLI). Session resume passes the session_id directly to the SDK via `options.resume`. The facade becomes a thin wrapper around the SDK manager + history reader.

**Tech Stack:** Python 3.10+, claude-agent-sdk, aiosqlite (kept for non-session tables), structlog

---

### Task 1: Add migration to drop sessions table

**Files:**
- Modify: `src/storage/database.py` (add migration 8)

**Step 1: Add migration**

In `_get_migrations()`, add after the existing migration 7 tuple:

```python
(
    8,
    """
    -- Session state now lives in Claude CLI's history.jsonl
    DROP TABLE IF EXISTS sessions;
    """,
),
```

**Step 2: Run tests to verify migration doesn't break anything**

Run: `uv run pytest tests/unit/test_storage/test_database.py -v`
Expected: PASS (existing tests should still pass)

**Step 3: Commit**

```bash
git add src/storage/database.py
git commit -m "chore: add migration 8 to drop sessions table"
```

---

### Task 2: Delete session storage file

**Files:**
- Delete: `src/storage/session_storage.py`

**Step 1: Delete the file**

```bash
rm src/storage/session_storage.py
```

**Step 2: Verify no remaining imports**

Run: `grep -r "session_storage" src/ --include="*.py"`
Expected: hits in `src/main.py` and possibly `src/claude/__init__.py` — these will be fixed in later tasks.

**Step 3: Commit**

```bash
git add -A
git commit -m "chore: delete SQLiteSessionStorage (sessions now in history.jsonl)"
```

---

### Task 3: Gut session.py — remove all session storage classes

**Files:**
- Rewrite: `src/claude/session.py` → empty module (or delete entirely)
- Delete: `tests/unit/test_claude/test_session.py`

**Step 1: Replace session.py with empty module**

Replace entire contents of `src/claude/session.py` with:

```python
"""Claude Code session management — intentionally empty.

Session state lives in Claude CLI's ~/.claude/history.jsonl.
The bot is a stateless pass-through; no local session tracking.
"""
```

**Step 2: Delete test file**

```bash
rm tests/unit/test_claude/test_session.py
```

**Step 3: Commit**

```bash
git add -A
git commit -m "chore: remove SessionManager and session storage classes"
```

---

### Task 4: Update claude __init__.py exports

**Files:**
- Modify: `src/claude/__init__.py`

**Step 1: Remove session imports and exports**

Replace entire file with:

```python
"""Claude Code integration module."""

from .exceptions import (
    ClaudeError,
    ClaudeParsingError,
    ClaudeProcessError,
    ClaudeSessionError,
    ClaudeTimeoutError,
)
from .facade import ClaudeIntegration
from .sdk_integration import ClaudeResponse, ClaudeSDKManager, StreamUpdate

__all__ = [
    # Exceptions
    "ClaudeError",
    "ClaudeParsingError",
    "ClaudeProcessError",
    "ClaudeSessionError",
    "ClaudeTimeoutError",
    # Main integration
    "ClaudeIntegration",
    # Core components
    "ClaudeSDKManager",
    "ClaudeResponse",
    "StreamUpdate",
]
```

**Step 2: Commit**

```bash
git add src/claude/__init__.py
git commit -m "chore: remove session classes from claude module exports"
```

---

### Task 5: Remove SessionModel from storage models

**Files:**
- Modify: `src/storage/models.py`

**Step 1: Delete the SessionModel class**

Remove the entire `SessionModel` class (lines 64-109). Keep all other models
(`UserModel`, `ProjectThreadModel`, `AuditLogModel`, `WebhookEventModel`,
`ScheduledJobModel`).

**Step 2: Commit**

```bash
git add src/storage/models.py
git commit -m "chore: remove SessionModel (no longer needed)"
```

---

### Task 6: Rewrite facade as thin client

This is the core fix. The facade no longer depends on SessionManager.

**Files:**
- Rewrite: `src/claude/facade.py`
- Rewrite: `tests/unit/test_claude/test_facade.py`

**Step 1: Write the failing test for the new facade**

Replace `tests/unit/test_claude/test_facade.py` with:

```python
"""Test ClaudeIntegration facade — thin client over SDK + history.jsonl."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.claude.facade import ClaudeIntegration
from src.claude.sdk_integration import ClaudeResponse
from src.config.settings import Settings


def _make_response(session_id: str = "sdk-session-123") -> ClaudeResponse:
    return ClaudeResponse(
        content="ok",
        session_id=session_id,
        cost=0.01,
        duration_ms=100,
        num_turns=1,
        tools_used=[],
    )


@pytest.fixture
def config(tmp_path):
    return Settings(
        telegram_bot_token="test:token",
        telegram_bot_username="testbot",
        approved_directory=tmp_path,
        session_timeout_hours=24,
    )


@pytest.fixture
def sdk_manager():
    mgr = MagicMock()
    mgr.execute_command = AsyncMock(return_value=_make_response())
    return mgr


@pytest.fixture
def facade(config, sdk_manager):
    return ClaudeIntegration(config=config, sdk_manager=sdk_manager)


class TestRunCommand:
    """Core run_command behavior."""

    async def test_new_session_no_resume(self, facade, sdk_manager):
        """Without session_id, SDK is called without resume."""
        resp = await facade.run_command(
            prompt="hello",
            working_directory=Path("/test"),
            user_id=123,
        )

        call_kwargs = sdk_manager.execute_command.call_args
        assert call_kwargs.kwargs.get("session_id") is None
        assert call_kwargs.kwargs.get("continue_session") is False
        assert resp.session_id == "sdk-session-123"

    async def test_resume_existing_session(self, facade, sdk_manager):
        """With session_id, SDK is called with resume."""
        resp = await facade.run_command(
            prompt="hello",
            working_directory=Path("/test"),
            user_id=123,
            session_id="existing-session-abc",
        )

        call_kwargs = sdk_manager.execute_command.call_args
        assert call_kwargs.kwargs.get("session_id") == "existing-session-abc"
        assert call_kwargs.kwargs.get("continue_session") is True

    async def test_force_new_ignores_session_id(self, facade, sdk_manager):
        """force_new=True overrides any provided session_id."""
        resp = await facade.run_command(
            prompt="hello",
            working_directory=Path("/test"),
            user_id=123,
            session_id="should-be-ignored",
            force_new=True,
        )

        call_kwargs = sdk_manager.execute_command.call_args
        assert call_kwargs.kwargs.get("session_id") is None
        assert call_kwargs.kwargs.get("continue_session") is False

    async def test_resume_failure_retries_fresh(self, facade, sdk_manager):
        """If resume fails, retries as fresh session."""
        fresh_response = _make_response("fresh-session-456")
        sdk_manager.execute_command = AsyncMock(
            side_effect=[RuntimeError("session gone"), fresh_response]
        )

        resp = await facade.run_command(
            prompt="hello",
            working_directory=Path("/test"),
            user_id=123,
            session_id="dead-session",
        )

        assert resp.session_id == "fresh-session-456"
        assert sdk_manager.execute_command.call_count == 2

        # Second call should be fresh (no resume)
        second_call = sdk_manager.execute_command.call_args_list[1]
        assert second_call.kwargs.get("session_id") is None
        assert second_call.kwargs.get("continue_session") is False


class TestAutoResume:
    """Auto-resume from history.jsonl."""

    async def test_auto_resume_picks_most_recent(self, facade, sdk_manager, tmp_path):
        """Without session_id, auto-resume reads history.jsonl."""
        history_entries = [
            MagicMock(session_id="old-session", timestamp=1000),
            MagicMock(session_id="recent-session", timestamp=2000),
        ]

        with patch(
            "src.claude.facade.read_claude_history", return_value=history_entries
        ):
            with patch(
                "src.claude.facade.filter_by_directory",
                return_value=history_entries,
            ):
                resp = await facade.run_command(
                    prompt="hello",
                    working_directory=tmp_path,
                    user_id=123,
                )

        call_kwargs = sdk_manager.execute_command.call_args
        assert call_kwargs.kwargs.get("session_id") == "recent-session"
        assert call_kwargs.kwargs.get("continue_session") is True

    async def test_auto_resume_skipped_when_force_new(
        self, facade, sdk_manager, tmp_path
    ):
        """force_new=True skips auto-resume entirely."""
        with patch(
            "src.claude.facade.read_claude_history"
        ) as mock_history:
            resp = await facade.run_command(
                prompt="hello",
                working_directory=tmp_path,
                user_id=123,
                force_new=True,
            )

        mock_history.assert_not_called()

    async def test_auto_resume_no_history(self, facade, sdk_manager, tmp_path):
        """When history.jsonl is empty, starts fresh session."""
        with patch("src.claude.facade.read_claude_history", return_value=[]):
            resp = await facade.run_command(
                prompt="hello",
                working_directory=tmp_path,
                user_id=123,
            )

        call_kwargs = sdk_manager.execute_command.call_args
        assert call_kwargs.kwargs.get("session_id") is None
        assert call_kwargs.kwargs.get("continue_session") is False
```

**Step 2: Run tests — they should fail**

Run: `uv run pytest tests/unit/test_claude/test_facade.py -v`
Expected: FAIL (facade still has old SessionManager-based API)

**Step 3: Rewrite the facade**

Replace entire contents of `src/claude/facade.py` with:

```python
"""High-level Claude Code integration facade.

Thin client: no local session state. Uses ~/.claude/history.jsonl as
the shared session index and passes session_id directly to the SDK.
"""

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import structlog

from ..config.settings import Settings
from .history import filter_by_directory, read_claude_history
from .sdk_integration import ClaudeResponse, ClaudeSDKManager, StreamUpdate

logger = structlog.get_logger()


class ClaudeIntegration:
    """Main integration point for Claude Code.

    Stateless: all session state lives in Claude CLI's history.jsonl.
    """

    def __init__(
        self,
        config: Settings,
        sdk_manager: Optional[ClaudeSDKManager] = None,
    ):
        """Initialize Claude integration facade."""
        self.config = config
        self.sdk_manager = sdk_manager or ClaudeSDKManager(config)

    async def run_command(
        self,
        prompt: str,
        working_directory: Path,
        user_id: int,
        session_id: Optional[str] = None,
        on_stream: Optional[Callable[[StreamUpdate], None]] = None,
        force_new: bool = False,
    ) -> ClaudeResponse:
        """Run Claude Code command with session management.

        Args:
            prompt: The user's message.
            working_directory: Project directory.
            user_id: Telegram user ID (for logging only).
            session_id: Session to resume. If None and not force_new,
                        auto-resumes from history.jsonl.
            on_stream: Streaming callback.
            force_new: If True, always start a fresh session.
        """
        # Determine whether to resume
        should_resume = False

        if force_new:
            session_id = None
        elif not session_id:
            # Auto-resume: pick most recent session for this directory
            session_id = self._find_resumable_session_id(working_directory)

        should_resume = bool(session_id)

        logger.info(
            "Running Claude command",
            user_id=user_id,
            working_directory=str(working_directory),
            session_id=session_id,
            should_resume=should_resume,
            force_new=force_new,
        )

        try:
            response = await self.sdk_manager.execute_command(
                prompt=prompt,
                working_directory=working_directory,
                session_id=session_id,
                continue_session=should_resume,
                stream_callback=on_stream,
            )
        except Exception as resume_error:
            if should_resume:
                logger.warning(
                    "Session resume failed, starting fresh",
                    failed_session_id=session_id,
                    error=str(resume_error),
                )
                response = await self.sdk_manager.execute_command(
                    prompt=prompt,
                    working_directory=working_directory,
                    session_id=None,
                    continue_session=False,
                    stream_callback=on_stream,
                )
            else:
                raise

        logger.info(
            "Claude command completed",
            session_id=response.session_id,
            cost=response.cost,
            duration_ms=response.duration_ms,
            num_turns=response.num_turns,
            is_error=response.is_error,
        )

        return response

    def _find_resumable_session_id(
        self, working_directory: Path
    ) -> Optional[str]:
        """Find the most recent session for a directory from history.jsonl.

        Returns session_id or None.
        """
        try:
            entries = read_claude_history()
            filtered = filter_by_directory(entries, working_directory)

            if not filtered:
                return None

            # Entries are already sorted newest-first by read_claude_history
            return filtered[0].session_id
        except Exception as e:
            logger.warning(
                "Failed to read session history for auto-resume",
                error=str(e),
            )
            return None

    async def shutdown(self) -> None:
        """Shutdown integration."""
        logger.info("Claude integration shutdown complete")
```

**Step 4: Run tests — they should pass**

Run: `uv run pytest tests/unit/test_claude/test_facade.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/claude/facade.py tests/unit/test_claude/test_facade.py
git commit -m "feat: rewrite facade as thin client using history.jsonl"
```

---

### Task 7: Update main.py wiring

**Files:**
- Modify: `src/main.py`

**Step 1: Remove session imports and wiring**

Remove these imports:
```python
from src.claude import SessionManager  # delete this line
from src.storage.session_storage import SQLiteSessionStorage  # delete this line
```

Replace the session wiring block (lines 140-151):
```python
    # Create Claude integration components with persistent storage
    session_storage = SQLiteSessionStorage(storage.db_manager)
    session_manager = SessionManager(config, session_storage)

    # Create Claude SDK manager and integration facade
    logger.info("Using Claude Python SDK integration")
    sdk_manager = ClaudeSDKManager(config, security_validator=security_validator)

    claude_integration = ClaudeIntegration(
        config=config,
        sdk_manager=sdk_manager,
        session_manager=session_manager,
    )
```

With:
```python
    # Create Claude SDK manager and integration facade
    logger.info("Using Claude Python SDK integration")
    sdk_manager = ClaudeSDKManager(config, security_validator=security_validator)

    claude_integration = ClaudeIntegration(
        config=config,
        sdk_manager=sdk_manager,
    )
```

**Step 2: Commit**

```bash
git add src/main.py
git commit -m "chore: remove session manager wiring from main.py"
```

---

### Task 8: Remove session config fields that are no longer used

**Files:**
- Modify: `src/config/settings.py`

**Step 1: Check which session-related settings exist**

Look for `session_timeout_hours`, `max_sessions_per_user` in settings.
These are no longer used by the facade (it has no session expiry or limit logic).

Keep `session_timeout_hours` if it's used elsewhere (e.g. the `/status` display).
Remove `max_sessions_per_user` if it's only used by SessionManager.

Verify with: `grep -rn "max_sessions_per_user" src/ --include="*.py"`

If only referenced in `session.py` and `settings.py`, remove it from settings.

**Step 2: Commit if changes were made**

```bash
git add src/config/settings.py
git commit -m "chore: remove unused session config fields"
```

---

### Task 9: Clean up orchestrator session-persist logic

The orchestrator currently calls `append_history_entry` after Claude responds (which
is correct and should stay) and stores `claude_session_id` in `user_data` (also correct).
No changes needed to the orchestrator's core flow — it already does the right thing.

**Files:**
- Verify: `src/bot/orchestrator.py` — confirm no references to SessionManager or session_storage remain

**Step 1: Verify**

Run: `grep -n "session_manager\|session_storage\|SessionManager\|SQLiteSessionStorage" src/bot/orchestrator.py`
Expected: no matches

**Step 2: Commit if cleanup needed**

---

### Task 10: Full test suite and lint

**Step 1: Run full test suite**

Run: `uv run pytest -v`
Expected: All tests pass (some old session tests were deleted in Task 3)

**Step 2: Run linter**

Run: `uv run make lint` or `uv run black src tests && uv run isort src tests && uv run flake8 src tests && uv run mypy src`
Expected: Clean

**Step 3: Fix any issues, then commit**

```bash
git add -A
git commit -m "chore: clean up after session storage removal"
```

---

### Task 11: Smoke test — verify CLI session resume works

**Step 1: Manual verification checklist**

1. Start bot, run `/sessions` — should show CLI sessions from history.jsonl
2. Click a CLI-initiated session — should show "Resumed session" with transcript preview
3. Send a message — should resume that CLI session (not start new)
4. Check bot logs for: `"Resuming previous session"` with the correct session_id
5. Start a new session from bot, verify it appears in `claude --resume` from CLI

**Step 2: Final commit if any fixes needed**
