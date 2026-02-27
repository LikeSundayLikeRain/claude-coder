# Resume Bot Command Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a Claude Code slash command (`/resume-bot`) that hands off the current CLI session to the Telegram bot, and a bot-side `/resume` handler that resumes the session with transcript preview.

**Architecture:** Two components — (1) a Claude Code skill file that sends the session ID to Telegram via `curl`, and (2) a bot command handler that parses the session ID, looks up the project directory from history.jsonl, and shows a transcript preview. All heavy lifting reuses existing `history.py` and `ClientManager` infrastructure.

**Tech Stack:** Python 3.10+, python-telegram-bot, Claude Code skill system (SKILL.md), Telegram Bot API

---

### Task 1: Add `find_session_by_id` helper to history.py

The bot needs to look up a session's `project` directory given just a session ID. No such function exists yet — `read_claude_history()` returns all entries and `filter_by_directory()` filters by directory (not ID). Add a small helper.

**Files:**
- Modify: `src/claude/history.py` (after `filter_by_directory`, around line 160)
- Test: `tests/unit/test_claude/test_history.py`

**Step 1: Write the failing test**

In `tests/unit/test_claude/test_history.py`, add:

```python
class TestFindSessionById:
    def test_finds_existing_session(self) -> None:
        entries = [
            HistoryEntry(session_id="aaa", display="first", timestamp=1000, project="/proj/a"),
            HistoryEntry(session_id="bbb", display="second", timestamp=2000, project="/proj/b"),
        ]
        result = find_session_by_id(entries, "bbb")
        assert result is not None
        assert result.session_id == "bbb"
        assert result.project == "/proj/b"

    def test_returns_none_for_missing(self) -> None:
        entries = [
            HistoryEntry(session_id="aaa", display="first", timestamp=1000, project="/proj/a"),
        ]
        result = find_session_by_id(entries, "zzz")
        assert result is None

    def test_returns_none_for_empty_list(self) -> None:
        result = find_session_by_id([], "aaa")
        assert result is None
```

Add the import at top of file:
```python
from src.claude.history import find_session_by_id
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_claude/test_history.py::TestFindSessionById -v`
Expected: FAIL with `ImportError: cannot import name 'find_session_by_id'`

**Step 3: Write minimal implementation**

In `src/claude/history.py`, after `filter_by_directory` function (around line 160), add:

```python
def find_session_by_id(
    entries: list[HistoryEntry], session_id: str
) -> Optional[HistoryEntry]:
    """Find a specific session entry by its ID.

    Returns the first matching entry, or None if not found.
    """
    for entry in entries:
        if entry.session_id == session_id:
            return entry
    return None
```

Ensure `Optional` is imported from `typing` (check existing imports at top of file).

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_claude/test_history.py::TestFindSessionById -v`
Expected: PASS (3 tests)

**Step 5: Commit**

```bash
git add src/claude/history.py tests/unit/test_claude/test_history.py
git commit -m "feat: add find_session_by_id helper to history module"
```

---

### Task 2: Add `agentic_resume` handler to orchestrator

This is the core bot-side handler. It parses the session ID from `/resume <session_id>`, looks up the project directory, sets user context, and shows a transcript preview.

**Files:**
- Modify: `src/bot/orchestrator.py`
  - `_register_agentic_handlers` (line ~304): add `("resume", self.agentic_resume)`
  - `get_bot_commands` (line ~416): add `BotCommand("resume", "Resume a session by ID")`
  - Add new method `agentic_resume` (after `agentic_sessions`, around line 1876)
- Modify: imports at top of `orchestrator.py` — add `find_session_by_id`

**Step 1: Add the import**

In `src/bot/orchestrator.py`, find the import block for history functions (search for `read_claude_history`). Add `find_session_by_id` to the import:

```python
from ..claude.history import (
    read_claude_history,
    read_session_transcript,
    filter_by_directory,
    check_history_format_health,
    find_session_by_id,
)
```

**Step 2: Register the handler**

In `_register_agentic_handlers` (around line 304), add to the `handlers` list:

```python
("resume", self.agentic_resume),
```

Place it after the `("sessions", self.agentic_sessions)` entry.

**Step 3: Add to command menu**

In `get_bot_commands` (around line 416), add after the `"sessions"` BotCommand:

```python
BotCommand("resume", "Resume a session by ID"),
```

**Step 4: Write the handler**

Add the `agentic_resume` method after `agentic_sessions` (around line 1876):

```python
async def agentic_resume(
    self, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Resume a session by its ID, with transcript preview."""
    if not update.message or not update.message.text:
        return

    parts = update.message.text.strip().split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text(
            "Usage: /resume <session_id>",
            parse_mode="HTML",
        )
        return

    session_id = parts[1].strip()

    # Look up session in history to get project directory
    history_entries = read_claude_history()
    entry = find_session_by_id(history_entries, session_id)

    if entry is None:
        await update.message.reply_text(
            "Session not found in history.",
            parse_mode="HTML",
        )
        return

    # Set session and directory in user context
    context.user_data["claude_session_id"] = session_id
    context.user_data["current_directory"] = entry.project

    # Build transcript preview
    lines: list[str] = ["\U0001f4c2 <b>Session resumed</b>\n"]

    try:
        transcript = read_session_transcript(
            session_id=session_id,
            project_dir=entry.project,
            limit=3,
        )
        if transcript:
            lines.append("<b>Recent:</b>")
            for msg in transcript:
                preview = msg.text[:120]
                if len(msg.text) > 120:
                    preview += "\u2026"
                label = "You" if msg.role == "user" else "Claude"
                lines.append(
                    f"  <b>{label}:</b> {escape_html(preview)}"
                )
    except Exception:
        pass

    lines.append("\nSend your next message to continue.")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
    )
```

Note: `escape_html` should already be imported — check existing imports (it's from `telegram.helpers`). If not, add: `from telegram.helpers import escape_html`.

**Step 5: Run lint**

Run: `uv run mypy src/bot/orchestrator.py --no-error-summary`
Expected: No new errors

**Step 6: Commit**

```bash
git add src/bot/orchestrator.py
git commit -m "feat: add /resume command to resume session by ID with transcript preview"
```

---

### Task 3: Write tests for `agentic_resume`

Follow the test patterns from `tests/unit/test_bot/test_sessions_command.py`.

**Files:**
- Create: `tests/unit/test_bot/test_resume_command.py`

**Step 1: Write the tests**

```python
"""Tests for the /resume agentic command."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bot.orchestrator import MessageOrchestrator
from src.claude.history import HistoryEntry, TranscriptMessage


@pytest.fixture
def mock_settings() -> MagicMock:
    settings = MagicMock()
    settings.approved_directories = ["/test/project"]
    settings.agentic_mode = True
    settings.enable_project_threads = False
    return settings


@pytest.fixture
def orchestrator(mock_settings: MagicMock) -> MessageOrchestrator:
    return MessageOrchestrator(mock_settings)


@pytest.fixture
def mock_update() -> MagicMock:
    update = MagicMock()
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    update.effective_user = MagicMock()
    update.effective_user.id = 123
    return update


@pytest.fixture
def mock_context() -> MagicMock:
    context = MagicMock()
    context.user_data = {}
    context.bot_data = {}
    return context


class TestAgenticResume:
    async def test_missing_session_id_shows_usage(
        self,
        orchestrator: MessageOrchestrator,
        mock_update: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        mock_update.message.text = "/resume"
        await orchestrator.agentic_resume(mock_update, mock_context)
        mock_update.message.reply_text.assert_called_once()
        msg = mock_update.message.reply_text.call_args.args[0]
        assert "Usage" in msg

    async def test_session_not_found(
        self,
        orchestrator: MessageOrchestrator,
        mock_update: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        mock_update.message.text = "/resume nonexistent-id"
        with patch(
            "src.bot.orchestrator.read_claude_history", return_value=[]
        ):
            with patch(
                "src.bot.orchestrator.find_session_by_id", return_value=None
            ):
                await orchestrator.agentic_resume(mock_update, mock_context)
        msg = mock_update.message.reply_text.call_args.args[0]
        assert "not found" in msg

    async def test_successful_resume_sets_context(
        self,
        orchestrator: MessageOrchestrator,
        mock_update: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        mock_update.message.text = "/resume sess-abc123"
        entry = HistoryEntry(
            session_id="sess-abc123",
            display="Fix auth bug",
            timestamp=1700000000000,
            project="/test/project",
        )
        with patch(
            "src.bot.orchestrator.read_claude_history",
            return_value=[entry],
        ):
            with patch(
                "src.bot.orchestrator.find_session_by_id",
                return_value=entry,
            ):
                with patch(
                    "src.bot.orchestrator.read_session_transcript",
                    return_value=[],
                ):
                    await orchestrator.agentic_resume(
                        mock_update, mock_context
                    )
        assert mock_context.user_data["claude_session_id"] == "sess-abc123"
        assert mock_context.user_data["current_directory"] == "/test/project"

    async def test_successful_resume_shows_transcript(
        self,
        orchestrator: MessageOrchestrator,
        mock_update: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        mock_update.message.text = "/resume sess-abc123"
        entry = HistoryEntry(
            session_id="sess-abc123",
            display="Fix auth bug",
            timestamp=1700000000000,
            project="/test/project",
        )
        transcript = [
            TranscriptMessage(role="user", text="Fix the auth middleware"),
            TranscriptMessage(
                role="assistant", text="I updated src/middleware/auth.py"
            ),
        ]
        with patch(
            "src.bot.orchestrator.read_claude_history",
            return_value=[entry],
        ):
            with patch(
                "src.bot.orchestrator.find_session_by_id",
                return_value=entry,
            ):
                with patch(
                    "src.bot.orchestrator.read_session_transcript",
                    return_value=transcript,
                ):
                    await orchestrator.agentic_resume(
                        mock_update, mock_context
                    )
        msg = mock_update.message.reply_text.call_args.args[0]
        assert "Session resumed" in msg
        assert "Fix the auth middleware" in msg
        assert "I updated src/middleware/auth.py" in msg

    async def test_resume_with_no_message(
        self,
        orchestrator: MessageOrchestrator,
        mock_update: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        mock_update.message = None
        await orchestrator.agentic_resume(mock_update, mock_context)
        # Should return silently without error
```

**Step 2: Run tests**

Run: `uv run pytest tests/unit/test_bot/test_resume_command.py -v`
Expected: All 5 tests PASS

**Step 3: Commit**

```bash
git add tests/unit/test_bot/test_resume_command.py
git commit -m "test: add tests for /resume command handler"
```

---

### Task 4: Create the Claude Code skill

This is the skill file that runs in Claude Code CLI when the user types `/resume-bot`.

**Files:**
- Create: `.claude/skills/resume-bot/SKILL.md`

**Step 1: Create the skill file**

```markdown
---
name: resume-bot
description: Hand off the current Claude Code session to the Telegram bot
argument-hint: ""
user-invocable: true
allowed-tools: ["Bash", "Read"]
---

# Resume Bot

Hand off the current Claude Code session to the Telegram bot so you can continue the conversation in Telegram.

## Instructions

1. Read the config file at `.claude/resume-bot.json` in the project root. It contains:
   - `bot_token`: The Telegram bot token
   - `chat_id`: Your Telegram chat ID

2. Send the current session to the bot by running:

```bash
curl -s -X POST "https://api.telegram.org/bot<bot_token>/sendMessage" \
  -d "chat_id=<chat_id>" \
  -d "text=/resume ${CLAUDE_SESSION_ID}"
```

3. Check the response. If `"ok": true`, report: "Session handed off to Telegram bot. You can continue the conversation there."

4. If the config file is missing, tell the user to create `.claude/resume-bot.json` with:
```json
{
  "bot_token": "<your-telegram-bot-token>",
  "chat_id": "<your-telegram-chat-id>"
}
```
```

**Step 2: Commit**

```bash
git add .claude/skills/resume-bot/SKILL.md
git commit -m "feat: add resume-bot Claude Code skill"
```

---

### Task 5: End-to-end verification

Verify the full flow works.

**Step 1: Run all tests**

Run: `uv run pytest tests/unit/test_claude/test_history.py tests/unit/test_bot/test_resume_command.py -v`
Expected: All tests PASS

**Step 2: Run lint**

Run: `make lint`
Expected: No errors

**Step 3: Verify skill is discoverable**

Check that the skill file has correct frontmatter by reading it back:
```bash
cat .claude/skills/resume-bot/SKILL.md
```

**Step 4: Verify handler registration**

Search for the resume handler in the orchestrator to confirm it's wired up:
```bash
grep -n "resume" src/bot/orchestrator.py
```

Expected: handler registration, bot command, and method definition all present.

**Step 5: Final commit (if any fixes needed)**

If lint or tests required fixes, commit those fixes.
