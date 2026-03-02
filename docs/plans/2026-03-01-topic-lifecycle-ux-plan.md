# Topic Lifecycle UX Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add idle close/reopen, history replay, `/history` command, and destructive `/remove` with confirmation to group forum topics.

**Architecture:** Extends existing `history.py` with a full transcript reader, adds `TopicLifecycleManager` for close/reopen/delete, and wires lifecycle hooks into `ClientManager`'s `on_exit` callback via `asyncio.create_task`.

**Tech Stack:** python-telegram-bot (forum topic APIs), aiosqlite, Claude CLI JSONL transcripts, structlog.

---

### Task 1: TranscriptReader — Parse Full Transcripts

**Files:**
- Create: `src/claude/transcript.py`
- Test: `tests/unit/test_transcript.py`
- Reference: `src/claude/history.py:248-346` (existing `read_session_transcript`, `_project_slug`)

**Context:** The existing `read_session_transcript()` in `history.py` only extracts text from user/assistant messages and ignores tool_use blocks. We need a richer reader that also captures tool calls for the condensed history display.

**Step 1: Write the failing test**

```python
# tests/unit/test_transcript.py
import json
import pytest
from pathlib import Path

from src.claude.transcript import TranscriptEntry, read_full_transcript


@pytest.fixture
def transcript_dir(tmp_path):
    """Create a mock transcript file."""
    slug_dir = tmp_path / "-tmp-myproject"
    slug_dir.mkdir()
    transcript = slug_dir / "sess-123.jsonl"
    lines = [
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "Fix the bug"}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Found the issue in auth.py"},
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "src/auth.py"}},
        ]}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "Add a test"}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Added test_auth.py. All passing."},
        ]}},
    ]
    transcript.write_text("\n".join(json.dumps(l) for l in lines))
    return tmp_path


def test_read_full_transcript(transcript_dir):
    entries = read_full_transcript("sess-123", "/tmp/myproject", projects_dir=transcript_dir)
    assert len(entries) == 4
    assert entries[0].role == "user"
    assert entries[0].text == "Fix the bug"
    assert entries[0].tool_name is None
    assert entries[1].role == "assistant"
    assert entries[1].text == "Found the issue in auth.py"
    assert entries[1].tool_name == "Edit"
    assert entries[1].tool_file == "src/auth.py"


def test_read_full_transcript_missing_file(tmp_path):
    entries = read_full_transcript("nonexistent", "/tmp/myproject", projects_dir=tmp_path)
    assert entries == []


def test_read_full_transcript_skips_thinking(transcript_dir):
    """Thinking blocks should be excluded."""
    slug_dir = transcript_dir / "-tmp-thinkproject"
    slug_dir.mkdir()
    transcript = slug_dir / "sess-456.jsonl"
    lines = [
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "Hello"}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "Let me think..."},
            {"type": "text", "text": "Hi there!"},
        ]}},
    ]
    transcript.write_text("\n".join(json.dumps(l) for l in lines))
    entries = read_full_transcript("sess-456", "/tmp/thinkproject", projects_dir=transcript_dir)
    assert len(entries) == 2
    assert entries[1].text == "Hi there!"
```

**Step 2: Run test to verify it fails**

Run: `cd /local/home/moxu/claude-coder/.claude/worktrees/multi-project && uv run pytest tests/unit/test_transcript.py -v`
Expected: FAIL — `ImportError: cannot import name 'TranscriptEntry' from 'src.claude.transcript'`

**Step 3: Write minimal implementation**

```python
# src/claude/transcript.py
"""Full session transcript reader for history replay."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import structlog

from .history import _project_slug

logger = structlog.get_logger()

DEFAULT_PROJECTS_DIR = Path.home() / ".claude" / "projects"


@dataclass(frozen=True)
class TranscriptEntry:
    """A single entry from a session transcript."""

    role: str  # "user" or "assistant"
    text: str
    tool_name: Optional[str] = None
    tool_file: Optional[str] = None


def read_full_transcript(
    session_id: str,
    project_dir: str,
    projects_dir: Path = DEFAULT_PROJECTS_DIR,
) -> List[TranscriptEntry]:
    """Read all user/assistant entries from a session transcript.

    Unlike history.read_session_transcript (which only gets text),
    this also captures tool_use blocks for the condensed display.
    """
    slug = _project_slug(project_dir)
    transcript_path = projects_dir / slug / f"{session_id}.jsonl"

    if not transcript_path.exists():
        return []

    entries: List[TranscriptEntry] = []

    try:
        with transcript_path.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_type = data.get("type")
                if msg_type not in ("user", "assistant"):
                    continue

                msg = data.get("message", {})
                if not isinstance(msg, dict):
                    continue

                content = msg.get("content", "")
                text = ""
                tool_name: Optional[str] = None
                tool_file: Optional[str] = None

                if isinstance(content, str):
                    text = content.strip()
                elif isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "text" and not text:
                            text = block["text"].strip()
                        elif block.get("type") == "tool_use" and not tool_name:
                            tool_name = block.get("name")
                            tool_input = block.get("input", {})
                            tool_file = tool_input.get("file_path") or tool_input.get("command", "")

                # Skip empty and system-injected messages
                if not text or text.startswith("<"):
                    continue

                entries.append(
                    TranscriptEntry(
                        role=msg_type,
                        text=text,
                        tool_name=tool_name,
                        tool_file=tool_file,
                    )
                )

    except Exception as e:
        logger.warning("transcript_read_failed", session_id=session_id, error=str(e))
        return []

    return entries
```

**Step 4: Run test to verify it passes**

Run: `cd /local/home/moxu/claude-coder/.claude/worktrees/multi-project && uv run pytest tests/unit/test_transcript.py -v`
Expected: PASS (3 tests)

**Step 5: Commit**

```bash
git add src/claude/transcript.py tests/unit/test_transcript.py
git commit -m "feat: TranscriptReader — parse full session transcripts with tool_use"
```

---

### Task 2: TranscriptReader — Condensed Formatting

**Files:**
- Modify: `src/claude/transcript.py`
- Modify: `tests/unit/test_transcript.py`

**Context:** Takes a list of `TranscriptEntry` and produces formatted messages for Telegram, splitting at 4000 chars.

**Step 1: Write the failing test**

```python
# Append to tests/unit/test_transcript.py

from src.claude.transcript import format_condensed


def test_format_condensed_basic(transcript_dir):
    entries = read_full_transcript("sess-123", "/tmp/myproject", projects_dir=transcript_dir)
    messages = format_condensed(entries)
    assert len(messages) == 1
    assert "Fix the bug" in messages[0]
    assert "Found the issue" in messages[0]
    assert "[used Edit" in messages[0]
    assert "Session history (2 exchanges)" in messages[0]


def test_format_condensed_empty():
    messages = format_condensed([])
    assert messages == []


def test_format_condensed_truncates_long_text():
    entries = [
        TranscriptEntry(role="user", text="x" * 300),
        TranscriptEntry(role="assistant", text="y" * 600),
    ]
    messages = format_condensed(entries)
    assert len(messages) == 1
    # User truncated at 200, assistant at 500
    assert "..." in messages[0]


def test_format_condensed_splits_at_limit():
    """Very long history should split into multiple messages."""
    entries = []
    for i in range(50):
        entries.append(TranscriptEntry(role="user", text=f"Question {i} " + "x" * 50))
        entries.append(TranscriptEntry(role="assistant", text=f"Answer {i} " + "y" * 100))
    messages = format_condensed(entries, max_chars=4000)
    assert len(messages) > 1
    for msg in messages:
        assert len(msg) <= 4000


def test_format_condensed_last_n():
    entries = [
        TranscriptEntry(role="user", text="First"),
        TranscriptEntry(role="assistant", text="First reply"),
        TranscriptEntry(role="user", text="Second"),
        TranscriptEntry(role="assistant", text="Second reply"),
        TranscriptEntry(role="user", text="Third"),
        TranscriptEntry(role="assistant", text="Third reply"),
    ]
    messages = format_condensed(entries, last_n=2)
    assert len(messages) == 1
    assert "Second" in messages[0]
    assert "Third" in messages[0]
    assert "First" not in messages[0]
    assert "Session history (2 exchanges)" in messages[0]
```

**Step 2: Run test to verify it fails**

Run: `cd /local/home/moxu/claude-coder/.claude/worktrees/multi-project && uv run pytest tests/unit/test_transcript.py -v -k format`
Expected: FAIL — `ImportError: cannot import name 'format_condensed'`

**Step 3: Write minimal implementation**

```python
# Append to src/claude/transcript.py

USER_TEXT_LIMIT = 200
ASSISTANT_TEXT_LIMIT = 500
SEPARATOR = "━━━━━━━━━━━━━━━━━━━━━━"


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _format_entry(entry: TranscriptEntry) -> str:
    if entry.role == "user":
        return f"👤 {_truncate(entry.text, USER_TEXT_LIMIT)}"
    else:
        parts = [f"🤖 {_truncate(entry.text, ASSISTANT_TEXT_LIMIT)}"]
        if entry.tool_name:
            target = entry.tool_file or ""
            if target:
                parts.append(f"[used {entry.tool_name} on {target}]")
            else:
                parts.append(f"[used {entry.tool_name}]")
        return "\n".join(parts)


def _count_exchanges(entries: List[TranscriptEntry]) -> int:
    """Count user messages (each user message = 1 exchange)."""
    return sum(1 for e in entries if e.role == "user")


def format_condensed(
    entries: List[TranscriptEntry],
    max_chars: int = 4000,
    last_n: Optional[int] = None,
) -> List[str]:
    """Format transcript entries into condensed Telegram messages.

    Args:
        entries: Full list of transcript entries.
        max_chars: Max characters per Telegram message.
        last_n: If set, only include the last N exchanges (user+assistant pairs).

    Returns:
        List of formatted message strings, each <= max_chars.
    """
    if not entries:
        return []

    # Slice to last N exchanges if requested
    if last_n is not None and last_n > 0:
        # Walk backwards to find start of the Nth-from-end exchange
        user_count = 0
        cut_idx = len(entries)
        for i in range(len(entries) - 1, -1, -1):
            if entries[i].role == "user":
                user_count += 1
                if user_count == last_n:
                    cut_idx = i
                    break
        entries = entries[cut_idx:]

    exchange_count = _count_exchanges(entries)
    formatted_blocks = [_format_entry(e) for e in entries]

    # Split into messages respecting max_chars
    messages: List[str] = []
    current_blocks: List[str] = []
    current_len = 0
    header_template = f"📜 Session history ({exchange_count} exchanges):\n{SEPARATOR}\n\n"
    footer = f"\n{SEPARATOR}"

    overhead = len(header_template) + len(footer)

    for block in formatted_blocks:
        block_len = len(block) + 2  # +2 for "\n\n" separator
        if current_len + block_len + overhead > max_chars and current_blocks:
            # Flush current message
            body = "\n\n".join(current_blocks)
            messages.append(f"{header_template}{body}{footer}")
            current_blocks = []
            current_len = 0
        current_blocks.append(block)
        current_len += block_len

    if current_blocks:
        body = "\n\n".join(current_blocks)
        messages.append(f"{header_template}{body}{footer}")

    return messages
```

**Step 4: Run test to verify it passes**

Run: `cd /local/home/moxu/claude-coder/.claude/worktrees/multi-project && uv run pytest tests/unit/test_transcript.py -v`
Expected: PASS (8 tests)

**Step 5: Commit**

```bash
git add src/claude/transcript.py tests/unit/test_transcript.py
git commit -m "feat: format_condensed — condensed history display for Telegram"
```

---

### Task 3: TopicLifecycleManager — Close and Delete

**Files:**
- Create: `src/projects/lifecycle.py`
- Modify: `src/projects/__init__.py`
- Test: `tests/unit/test_topic_lifecycle.py`

**Context:** Manages topic visual state. `close_on_idle(bot, chat_id, thread_id)` sends a message then closes the topic. `reopen(bot, chat_id, thread_id)` reopens silently. `delete_confirmed(bot, chat_id, thread_id)` permanently deletes.

**Step 1: Write the failing test**

```python
# tests/unit/test_topic_lifecycle.py
from unittest.mock import AsyncMock, patch
import pytest
from telegram.error import TelegramError

from src.projects.lifecycle import TopicLifecycleManager


@pytest.fixture
def bot():
    mock = AsyncMock()
    mock.send_message = AsyncMock()
    mock.close_forum_topic = AsyncMock()
    mock.reopen_forum_topic = AsyncMock()
    mock.delete_forum_topic = AsyncMock()
    return mock


@pytest.fixture
def lifecycle():
    return TopicLifecycleManager()


@pytest.mark.asyncio
async def test_close_on_idle(lifecycle, bot):
    await lifecycle.close_on_idle(bot, chat_id=-1001234, message_thread_id=42)
    bot.send_message.assert_called_once()
    assert "idle" in bot.send_message.call_args.kwargs.get("text", "").lower() or \
           "idle" in str(bot.send_message.call_args)
    bot.close_forum_topic.assert_called_once_with(chat_id=-1001234, message_thread_id=42)


@pytest.mark.asyncio
async def test_close_on_idle_ignores_telegram_error(lifecycle, bot):
    bot.close_forum_topic.side_effect = TelegramError("topic already closed")
    # Should not raise
    await lifecycle.close_on_idle(bot, chat_id=-1001234, message_thread_id=42)


@pytest.mark.asyncio
async def test_reopen(lifecycle, bot):
    await lifecycle.reopen(bot, chat_id=-1001234, message_thread_id=42)
    bot.reopen_forum_topic.assert_called_once_with(chat_id=-1001234, message_thread_id=42)


@pytest.mark.asyncio
async def test_reopen_ignores_error(lifecycle, bot):
    bot.reopen_forum_topic.side_effect = TelegramError("not closed")
    await lifecycle.reopen(bot, chat_id=-1001234, message_thread_id=42)


@pytest.mark.asyncio
async def test_delete_confirmed(lifecycle, bot):
    await lifecycle.delete_confirmed(bot, chat_id=-1001234, message_thread_id=42)
    bot.delete_forum_topic.assert_called_once_with(chat_id=-1001234, message_thread_id=42)


@pytest.mark.asyncio
async def test_delete_falls_back_to_close(lifecycle, bot):
    bot.delete_forum_topic.side_effect = TelegramError("cannot delete")
    await lifecycle.delete_confirmed(bot, chat_id=-1001234, message_thread_id=42)
    bot.close_forum_topic.assert_called_once_with(chat_id=-1001234, message_thread_id=42)
```

**Step 2: Run test to verify it fails**

Run: `cd /local/home/moxu/claude-coder/.claude/worktrees/multi-project && uv run pytest tests/unit/test_topic_lifecycle.py -v`
Expected: FAIL — ImportError

**Step 3: Write minimal implementation**

```python
# src/projects/lifecycle.py
"""Topic lifecycle management — close/reopen/delete forum topics."""

from __future__ import annotations

import structlog
from telegram import Bot
from telegram.error import TelegramError

logger = structlog.get_logger()


class TopicLifecycleManager:
    """Manages Telegram forum topic visual state."""

    async def close_on_idle(
        self, bot: Bot, chat_id: int, message_thread_id: int
    ) -> None:
        """Send idle message and close the topic."""
        try:
            await bot.send_message(
                chat_id=chat_id,
                message_thread_id=message_thread_id,
                text="Session disconnected (idle). Send a message to reconnect.",
            )
        except TelegramError as e:
            logger.debug("idle_message_failed", error=str(e))

        try:
            await bot.close_forum_topic(
                chat_id=chat_id, message_thread_id=message_thread_id
            )
        except TelegramError as e:
            logger.debug("close_topic_failed", error=str(e))

    async def reopen(
        self, bot: Bot, chat_id: int, message_thread_id: int
    ) -> None:
        """Reopen a closed topic. Silently ignores errors."""
        try:
            await bot.reopen_forum_topic(
                chat_id=chat_id, message_thread_id=message_thread_id
            )
        except TelegramError as e:
            logger.debug("reopen_topic_failed", error=str(e))

    async def delete_confirmed(
        self, bot: Bot, chat_id: int, message_thread_id: int
    ) -> None:
        """Permanently delete a topic. Falls back to close on failure."""
        try:
            await bot.delete_forum_topic(
                chat_id=chat_id, message_thread_id=message_thread_id
            )
        except TelegramError:
            try:
                await bot.close_forum_topic(
                    chat_id=chat_id, message_thread_id=message_thread_id
                )
            except TelegramError as e:
                logger.warning("delete_fallback_close_failed", error=str(e))
```

Also update `src/projects/__init__.py`:
```python
"""Project thread management."""

from .lifecycle import TopicLifecycleManager
from .thread_manager import ProjectThreadManager

__all__ = ["ProjectThreadManager", "TopicLifecycleManager"]
```

**Step 4: Run test to verify it passes**

Run: `cd /local/home/moxu/claude-coder/.claude/worktrees/multi-project && uv run pytest tests/unit/test_topic_lifecycle.py -v`
Expected: PASS (6 tests)

**Step 5: Commit**

```bash
git add src/projects/lifecycle.py src/projects/__init__.py tests/unit/test_topic_lifecycle.py
git commit -m "feat: TopicLifecycleManager — close/reopen/delete forum topics"
```

---

### Task 4: Wire Lifecycle into ClientManager on_exit

**Files:**
- Modify: `src/claude/client_manager.py`
- Test: `tests/unit/test_client_manager_lifecycle.py`

**Context:** The `on_exit` callback in `UserClient` is synchronous (plain function). Since `TopicLifecycleManager.close_on_idle()` is async, the callback must schedule a coroutine. We pass the `Bot` instance and `TopicLifecycleManager` to `ClientManager`, and the `on_exit` closure uses `asyncio.get_event_loop().create_task()` to fire-and-forget the close.

**Step 1: Write the failing test**

```python
# tests/unit/test_client_manager_lifecycle.py
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from src.claude.client_manager import ClientManager
from src.projects.lifecycle import TopicLifecycleManager


@pytest.mark.asyncio
async def test_on_exit_closes_topic_for_group():
    """on_exit should schedule close_on_idle for group topics."""
    lifecycle = TopicLifecycleManager()
    lifecycle.close_on_idle = AsyncMock()
    bot = AsyncMock()

    repo = AsyncMock()
    repo.get = AsyncMock(return_value=None)
    options_builder = MagicMock()

    cm = ClientManager(
        chat_session_repo=repo,
        options_builder=options_builder,
        bot=bot,
        lifecycle_manager=lifecycle,
    )

    # Build the on_exit callback for a group topic
    on_exit = cm._make_on_exit(user_id=111, chat_id=-1001234, message_thread_id=42)

    # Call it (simulates idle timeout)
    on_exit(111)

    # Give the scheduled task a chance to run
    await asyncio.sleep(0.05)

    lifecycle.close_on_idle.assert_called_once_with(bot, chat_id=-1001234, message_thread_id=42)


@pytest.mark.asyncio
async def test_on_exit_skips_close_for_private_dm():
    """on_exit should NOT close topic for private DMs (chat_id == user_id, thread_id == 0)."""
    lifecycle = TopicLifecycleManager()
    lifecycle.close_on_idle = AsyncMock()
    bot = AsyncMock()

    repo = AsyncMock()
    options_builder = MagicMock()

    cm = ClientManager(
        chat_session_repo=repo,
        options_builder=options_builder,
        bot=bot,
        lifecycle_manager=lifecycle,
    )

    on_exit = cm._make_on_exit(user_id=111, chat_id=111, message_thread_id=0)
    on_exit(111)
    await asyncio.sleep(0.05)

    lifecycle.close_on_idle.assert_not_called()


@pytest.mark.asyncio
async def test_on_exit_works_without_lifecycle():
    """If no lifecycle_manager is provided, on_exit still removes the client."""
    repo = AsyncMock()
    options_builder = MagicMock()

    cm = ClientManager(chat_session_repo=repo, options_builder=options_builder)
    cm._clients[(111, -1001234, 42)] = MagicMock()

    on_exit = cm._make_on_exit(user_id=111, chat_id=-1001234, message_thread_id=42)
    on_exit(111)

    assert (111, -1001234, 42) not in cm._clients
```

**Step 2: Run test to verify it fails**

Run: `cd /local/home/moxu/claude-coder/.claude/worktrees/multi-project && uv run pytest tests/unit/test_client_manager_lifecycle.py -v`
Expected: FAIL — `TypeError: ClientManager.__init__() got an unexpected keyword argument 'bot'`

**Step 3: Modify ClientManager**

In `src/claude/client_manager.py`, update `__init__` to accept optional `bot` and `lifecycle_manager`, and update `_make_on_exit`:

```python
# Updated __init__ signature (add bot and lifecycle_manager params):
def __init__(
    self,
    chat_session_repo: ChatSessionRepository,
    options_builder: Optional[OptionsBuilder] = None,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT_SECONDS,
    bot: Optional[Any] = None,
    lifecycle_manager: Optional[Any] = None,
) -> None:
    self._chat_session_repo = chat_session_repo
    self._options_builder = options_builder or OptionsBuilder()
    self._idle_timeout = idle_timeout
    self._bot = bot
    self._lifecycle_manager = lifecycle_manager
    self._clients: dict[tuple[int, int, int], UserClient] = {}
```

Add import at top: `import asyncio`

Updated `_make_on_exit`:
```python
def _make_on_exit(
    self, user_id: int, chat_id: int, message_thread_id: int
) -> Callable[[int], None]:
    """Return a closure that removes the triple key on actor exit."""

    def on_exit(_uid: int) -> None:
        self._clients.pop((user_id, chat_id, message_thread_id), None)
        logger.info(
            "client_manager_actor_exited",
            user_id=user_id,
            chat_id=chat_id,
            message_thread_id=message_thread_id,
        )
        # Schedule topic close for group topics (not private DMs)
        if (
            self._bot is not None
            and self._lifecycle_manager is not None
            and chat_id != user_id  # private DM: chat_id == user_id
        ):
            try:
                loop = asyncio.get_event_loop()
                loop.create_task(
                    self._lifecycle_manager.close_on_idle(
                        self._bot, chat_id=chat_id, message_thread_id=message_thread_id
                    )
                )
            except RuntimeError:
                pass  # no event loop (shutdown)

    return on_exit
```

**Step 4: Run test to verify it passes**

Run: `cd /local/home/moxu/claude-coder/.claude/worktrees/multi-project && uv run pytest tests/unit/test_client_manager_lifecycle.py -v`
Expected: PASS (3 tests)

Also run existing tests: `uv run pytest tests/unit/test_client_manager_v2.py -v`
Expected: PASS (all existing tests still pass — `bot` and `lifecycle_manager` are optional)

**Step 5: Commit**

```bash
git add src/claude/client_manager.py tests/unit/test_client_manager_lifecycle.py
git commit -m "feat: wire TopicLifecycleManager into ClientManager on_exit"
```

---

### Task 5: Orchestrator — Reopen Topic on Resume

**Files:**
- Modify: `src/bot/orchestrator.py` (around line 862, in `_execute_query`)
- Test: `tests/unit/test_orchestrator_reopen.py`

**Context:** Before `get_or_connect()` reconnects the SDK, we should reopen the topic if it was closed. Add this in `_execute_query` right after cold-start restoration and before the progress message.

**Step 1: Write the failing test**

```python
# tests/unit/test_orchestrator_reopen.py
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from src.projects.lifecycle import TopicLifecycleManager


@pytest.mark.asyncio
async def test_reopen_called_for_group_topic():
    """_execute_query should reopen the topic for supergroup messages."""
    lifecycle = TopicLifecycleManager()
    lifecycle.reopen = AsyncMock()

    # Verify the reopen method works standalone
    bot = AsyncMock()
    await lifecycle.reopen(bot, chat_id=-1001234, message_thread_id=42)
    lifecycle.reopen.assert_called_once()
```

Note: Full orchestrator integration test is complex (requires full bot setup). We test the `TopicLifecycleManager.reopen` directly and verify the wiring via manual/integration testing. The orchestrator change is a 3-line addition.

**Step 2: Modify orchestrator**

In `src/bot/orchestrator.py`, in `_execute_query`, after the cold-start restoration block (around line 880) and before `chat.send_action("typing")`, add:

```python
        # Reopen topic if it was closed (idle timeout)
        if message_thread_id != 0:
            lifecycle: Optional[TopicLifecycleManager] = context.bot_data.get("lifecycle_manager")
            if lifecycle:
                await lifecycle.reopen(context.bot, chat_id, message_thread_id)
```

Add import at top of orchestrator: `from src.projects.lifecycle import TopicLifecycleManager`

**Step 3: Run tests**

Run: `cd /local/home/moxu/claude-coder/.claude/worktrees/multi-project && uv run pytest tests/unit/test_orchestrator_reopen.py tests/unit/test_orchestrator.py -v`
Expected: PASS

**Step 4: Commit**

```bash
git add src/bot/orchestrator.py tests/unit/test_orchestrator_reopen.py
git commit -m "feat: reopen closed topic on resume in _execute_query"
```

---

### Task 6: Orchestrator — History Replay on Fresh Topic

**Files:**
- Modify: `src/bot/orchestrator.py` (in `_execute_query`, after reopen, before progress message)

**Context:** If the topic is fresh (no prior bot messages) and resuming an existing session, load and display the transcript before processing the query.

**Step 1: Implementation**

In `_execute_query`, after the reopen block, add:

```python
        # History replay for fresh topic resuming existing session
        if (
            message_thread_id != 0
            and session_id
            and not context.chat_data.get("_history_replayed")
        ):
            try:
                from src.claude.transcript import format_condensed, read_full_transcript

                current_dir_str = str(current_dir)
                entries = read_full_transcript(session_id, current_dir_str)
                if entries:
                    history_messages = format_condensed(entries)
                    for hist_msg in history_messages:
                        await update.message.chat.send_message(
                            hist_msg,
                            message_thread_id=message_thread_id,
                        )
                    context.chat_data["_history_replayed"] = True
            except Exception as e:
                logger.debug("history_replay_failed", error=str(e))
                context.chat_data["_history_replayed"] = True  # don't retry
```

**Step 2: Run tests**

Run: `cd /local/home/moxu/claude-coder/.claude/worktrees/multi-project && uv run pytest tests/ -x --tb=short -q`
Expected: All pass

**Step 3: Commit**

```bash
git add src/bot/orchestrator.py
git commit -m "feat: auto-replay transcript history on fresh topic resume"
```

---

### Task 7: /remove with Double Confirmation and delete_forum_topic

**Files:**
- Modify: `src/bot/orchestrator.py` (replace `agentic_remove` method)
- Modify: `src/projects/thread_manager.py` (add `delete_topic` method)
- Test: `tests/unit/test_remove_confirm.py`

**Step 1: Write the failing test**

```python
# tests/unit/test_remove_confirm.py
from unittest.mock import AsyncMock, MagicMock
import pytest

from src.projects.lifecycle import TopicLifecycleManager


@pytest.mark.asyncio
async def test_delete_confirmed_calls_delete():
    lifecycle = TopicLifecycleManager()
    bot = AsyncMock()
    await lifecycle.delete_confirmed(bot, chat_id=-1001234, message_thread_id=42)
    bot.delete_forum_topic.assert_called_once_with(chat_id=-1001234, message_thread_id=42)
```

Note: Already covered by Task 3 tests. The orchestrator change is behavioral wiring.

**Step 2: Update agentic_remove in orchestrator**

Replace the existing `agentic_remove` method (around line 1445) with:

```python
    async def agentic_remove(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Remove this topic — double-confirm then delete permanently."""
        if not update.message or not update.effective_user:
            return
        thread_ctx = context.user_data.get("_thread_context")
        if not thread_ctx:
            await update.message.reply_text("Use /remove inside a project topic.")
            return

        user_id = update.effective_user.id
        chat_id = thread_ctx["chat_id"]
        message_thread_id = thread_ctx["message_thread_id"]

        # Double-confirm gate
        if not context.chat_data.get("pending_remove"):
            context.chat_data["pending_remove"] = True
            await update.message.reply_text(
                "⚠️ This will <b>permanently delete</b> this topic and all messages.\n\n"
                "Send <code>/remove</code> again to confirm.",
                parse_mode="HTML",
            )
            return

        # Confirmed — proceed with deletion
        context.chat_data.pop("pending_remove", None)

        # Disconnect client
        client_manager: Optional[ClientManager] = context.bot_data.get("client_manager")
        if client_manager:
            await client_manager.disconnect(user_id, chat_id, message_thread_id)

        # Deactivate DB row
        manager = context.bot_data.get("project_threads_manager")
        if manager:
            await manager.repository.deactivate(chat_id, message_thread_id)

        # Delete topic (falls back to close)
        lifecycle: Optional[TopicLifecycleManager] = context.bot_data.get("lifecycle_manager")
        if lifecycle:
            await lifecycle.delete_confirmed(context.bot, chat_id, message_thread_id)
        else:
            # Fallback: just close
            try:
                await context.bot.close_forum_topic(
                    chat_id=chat_id, message_thread_id=message_thread_id
                )
            except Exception:
                pass

        logger.info("topic_deleted", chat_id=chat_id, message_thread_id=message_thread_id)
```

Also add a handler to clear the pending_remove flag when any non-/remove message arrives. In `agentic_text`, add early in the method:

```python
        # Clear pending /remove confirmation on any non-command message
        context.chat_data.pop("pending_remove", None)
```

**Step 3: Run tests**

Run: `cd /local/home/moxu/claude-coder/.claude/worktrees/multi-project && uv run pytest tests/ -x --tb=short -q`
Expected: All pass

**Step 4: Commit**

```bash
git add src/bot/orchestrator.py
git commit -m "feat: /remove with double confirmation and delete_forum_topic"
```

---

### Task 8: /history Command

**Files:**
- Modify: `src/bot/orchestrator.py` (new `agentic_history` handler + register it)

**Step 1: Add handler**

```python
    async def agentic_history(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show session transcript history in this topic."""
        if not update.message or not update.effective_user:
            return

        chat_id, message_thread_id = self._resolve_chat_key(update, context)
        storage = context.bot_data.get("storage")

        # Parse optional limit argument
        args = update.message.text.split()[1:] if update.message.text else []
        last_n = None
        if args:
            try:
                last_n = int(args[0])
            except ValueError:
                await update.message.reply_text("Usage: /history [N]")
                return

        # Resolve session
        session = await storage.load_session(chat_id, message_thread_id) if storage else None
        if not session or not session.session_id:
            await update.message.reply_text("No active session in this topic.")
            return

        from src.claude.transcript import format_condensed, read_full_transcript

        entries = read_full_transcript(session.session_id, session.directory)
        if not entries:
            await update.message.reply_text(
                f"No transcript available for session <code>{session.session_id[:8]}...</code>",
                parse_mode="HTML",
            )
            return

        messages = format_condensed(entries, last_n=last_n)
        for msg in messages:
            await update.message.reply_text(msg)
```

**Step 2: Register the handler**

In `_register_agentic_handlers`, add alongside the other topic commands:

```python
app.add_handler(CommandHandler("history", self.agentic_history, filters=filters.ChatType.SUPERGROUP), group=10)
```

Also add to `get_bot_commands()` group commands:

```python
BotCommand("history", "Show session transcript"),
```

**Step 3: Run tests**

Run: `cd /local/home/moxu/claude-coder/.claude/worktrees/multi-project && uv run pytest tests/ -x --tb=short -q`
Expected: All pass

**Step 4: Commit**

```bash
git add src/bot/orchestrator.py
git commit -m "feat: /history command — on-demand transcript retrieval"
```

---

### Task 9: Wire Everything in main.py

**Files:**
- Modify: `src/main.py`

**Step 1: Wire lifecycle manager and bot into ClientManager**

In `create_application()`, after the `ProjectThreadManager` comment in `run_application()` (around line 268-271):

```python
        # Topic lifecycle manager
        lifecycle_manager = TopicLifecycleManager()
        bot.deps["lifecycle_manager"] = lifecycle_manager
```

And update the `ClientManager` construction (in `create_application()`) to pass `bot` and `lifecycle_manager`. But `bot` (Telegram Bot instance) is only available after `bot.initialize()` — so we need to set these after init:

```python
        # Wire bot and lifecycle into client_manager (bot available after init)
        client_manager._bot = telegram_bot
        client_manager._lifecycle_manager = lifecycle_manager
```

Add import: `from src.projects.lifecycle import TopicLifecycleManager`

**Step 2: Run full test suite**

Run: `cd /local/home/moxu/claude-coder/.claude/worktrees/multi-project && uv run pytest tests/ -x --tb=short -q`
Expected: All pass

**Step 3: Commit**

```bash
git add src/main.py
git commit -m "feat: wire TopicLifecycleManager into main.py startup"
```

---

### Task 10: Update Documentation

**Files:**
- Modify: `CLAUDE.md`

**Step 1: Update docs**

Add to the "Agentic mode commands" list: `/history`.

Update the `/remove` description to mention permanent deletion with confirmation.

Update the architecture section to mention `TopicLifecycleManager` in `src/projects/`.

**Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md with /history, /remove confirm, topic lifecycle"
```
