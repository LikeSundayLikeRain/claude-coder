# Rich Progress Display Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the ephemeral, truncated "Working..." progress message with a persistent, rich activity log that shows full intermediate text, tool call summaries with results, and thinking indicators — matching the CLI experience.

**Architecture:** New `ProgressMessageManager` class in `src/bot/progress.py` manages one or more persistent Telegram messages that accumulate activity. The stream callback in the orchestrator is rewritten to populate `ActivityEntry` objects without truncation. `UserClient` forwards tool result events. Verbose level system removed entirely.

**Tech Stack:** Python 3.10+, python-telegram-bot, dataclasses

---

### Task 1: Create ActivityEntry Data Model

**Files:**
- Create: `src/bot/progress.py`
- Test: `tests/unit/test_bot/test_progress.py`

**Step 1: Write the failing test**

Create `tests/unit/test_bot/__init__.py` if it doesn't exist, then create the test file:

```python
# tests/unit/test_bot/__init__.py
# (empty)
```

```python
# tests/unit/test_bot/test_progress.py
"""Tests for ProgressMessageManager and ActivityEntry."""

from src.bot.progress import ActivityEntry


class TestActivityEntry:
    """Tests for the ActivityEntry dataclass."""

    def test_create_text_entry(self) -> None:
        entry = ActivityEntry(kind="text", content="Let me check that.")
        assert entry.kind == "text"
        assert entry.content == "Let me check that."
        assert entry.tool_name == ""
        assert entry.is_running is False

    def test_create_tool_entry(self) -> None:
        entry = ActivityEntry(
            kind="tool",
            content="",
            tool_name="Read",
            tool_detail="orchestrator.py",
            is_running=True,
        )
        assert entry.kind == "tool"
        assert entry.tool_name == "Read"
        assert entry.tool_detail == "orchestrator.py"
        assert entry.is_running is True
        assert entry.tool_result == ""

    def test_create_thinking_entry(self) -> None:
        entry = ActivityEntry(kind="thinking", content="Thinking", is_running=True)
        assert entry.kind == "thinking"
        assert entry.is_running is True

    def test_tool_result_default_empty(self) -> None:
        entry = ActivityEntry(kind="tool", content="", tool_name="Bash")
        assert entry.tool_result == ""
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_bot/test_progress.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.bot.progress'`

**Step 3: Write minimal implementation**

```python
# src/bot/progress.py
"""Rich progress display for Telegram — persistent activity log messages."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class ActivityEntry:
    """One line in the activity log: text, tool call, or thinking indicator."""

    kind: Literal["text", "tool", "thinking"]
    content: str = ""
    tool_name: str = ""
    tool_detail: str = ""
    tool_result: str = ""
    is_running: bool = False
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_bot/test_progress.py -v`
Expected: PASS (4 tests)

**Step 5: Commit**

```bash
git add src/bot/progress.py tests/unit/test_bot/__init__.py tests/unit/test_bot/test_progress.py
git commit -m "feat: add ActivityEntry data model for rich progress display"
```

---

### Task 2: Add Tool Icons and Summarizer to progress.py

Move `_TOOL_ICONS`, `_tool_icon()`, `_redact_secrets()`, `_SECRET_PATTERNS`, and `_summarize_tool_input()` from `orchestrator.py` into `progress.py`. The orchestrator will import them from the new location.

**Files:**
- Modify: `src/bot/progress.py`
- Test: `tests/unit/test_bot/test_progress.py`

**Step 1: Write the failing tests**

Append to `tests/unit/test_bot/test_progress.py`:

```python
from src.bot.progress import summarize_tool_input, tool_icon


class TestToolIcon:
    """Tests for tool_icon()."""

    def test_known_tool(self) -> None:
        assert tool_icon("Read") == "\U0001f4d6"  # 📖
        assert tool_icon("Bash") == "\U0001f4bb"  # 💻

    def test_unknown_tool_returns_wrench(self) -> None:
        assert tool_icon("SomeNewTool") == "\U0001f527"  # 🔧


class TestSummarizeToolInput:
    """Tests for summarize_tool_input()."""

    def test_read_shows_filename(self) -> None:
        result = summarize_tool_input("Read", {"file_path": "/home/user/src/foo.py"})
        assert result == "foo.py"

    def test_bash_shows_command(self) -> None:
        result = summarize_tool_input("Bash", {"command": "git status"})
        assert result == "git status"

    def test_grep_shows_pattern(self) -> None:
        result = summarize_tool_input("Grep", {"pattern": "def main"})
        assert result == "def main"

    def test_empty_input(self) -> None:
        result = summarize_tool_input("Read", {})
        assert result == ""
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_bot/test_progress.py::TestToolIcon -v`
Expected: FAIL with `ImportError: cannot import name 'tool_icon'`

**Step 3: Write minimal implementation**

Add to `src/bot/progress.py` — copy the `_SECRET_PATTERNS`, `_redact_secrets()`, `_TOOL_ICONS`, `_tool_icon()`, and `_summarize_tool_input()` from `src/bot/orchestrator.py` lines 49-942. Rename to public functions (drop leading `_`):

```python
import re
from typing import Any, Dict, List

# --- Secret redaction (moved from orchestrator.py) ---

_SECRET_PATTERNS: List[re.Pattern[str]] = [
    re.compile(
        r"(sk-ant-api\d*-[A-Za-z0-9_-]{10})[A-Za-z0-9_-]*"
        r"|(sk-[A-Za-z0-9_-]{20})[A-Za-z0-9_-]*"
    ),
    re.compile(r"(ghp_[A-Za-z0-9]{4})[A-Za-z0-9_-]*"),
    re.compile(r"(gho_[A-Za-z0-9]{4})[A-Za-z0-9_-]*"),
    re.compile(r"(github_pat_[A-Za-z0-9]{4})[A-Za-z0-9_-]*"),
    re.compile(r"(xoxb-[0-9]{4})[0-9A-Za-z-]*"),
    re.compile(r"(AKIA[0-9A-Z]{4})[0-9A-Z]{12}"),
    re.compile(
        r"((?:--token|--secret|--password|--api-key|--apikey|--auth)"
        r"[= ]+)['\"]?[A-Za-z0-9+/_.:-]{8,}['\"]?"
    ),
    re.compile(
        r"((?:TOKEN|SECRET|PASSWORD|API_KEY|APIKEY|AUTH_TOKEN|PRIVATE_KEY"
        r"|ACCESS_KEY|CLIENT_SECRET|WEBHOOK_SECRET)"
        r"=)['\"]?[^\s'\"]{8,}['\"]?"
    ),
    re.compile(r"(Bearer )[A-Za-z0-9+/_.:-]{8,}" r"|(Basic )[A-Za-z0-9+/=]{8,}"),
    re.compile(r"://([^:]+:)[^@]{4,}(@)"),
]


def redact_secrets(text: str) -> str:
    """Replace likely secrets/credentials with redacted placeholders."""
    result = text
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(
            lambda m: next((g + "***" for g in m.groups() if g is not None), "***"),
            result,
        )
    return result


# --- Tool icons ---

TOOL_ICONS: Dict[str, str] = {
    "Read": "\U0001f4d6",
    "Write": "\u270f\ufe0f",
    "Edit": "\u270f\ufe0f",
    "MultiEdit": "\u270f\ufe0f",
    "Bash": "\U0001f4bb",
    "Glob": "\U0001f50d",
    "Grep": "\U0001f50d",
    "LS": "\U0001f4c2",
    "Task": "\U0001f9e0",
    "TaskOutput": "\U0001f9e0",
    "WebFetch": "\U0001f310",
    "WebSearch": "\U0001f310",
    "NotebookRead": "\U0001f4d3",
    "NotebookEdit": "\U0001f4d3",
    "TodoRead": "\u2611\ufe0f",
    "TodoWrite": "\u2611\ufe0f",
    "Skill": "\U0001f527",
}


def tool_icon(name: str) -> str:
    """Return emoji for a tool, with a default wrench."""
    return TOOL_ICONS.get(name, "\U0001f527")


def summarize_tool_input(tool_name: str, tool_input: Dict[str, Any]) -> str:
    """Return a short summary of tool input for progress display."""
    if not tool_input:
        return ""
    if tool_name in ("Read", "Write", "Edit", "MultiEdit"):
        path = tool_input.get("file_path") or tool_input.get("path", "")
        if path:
            return path.rsplit("/", 1)[-1]
    if tool_name in ("Glob", "Grep"):
        pattern = tool_input.get("pattern", "")
        if pattern:
            return pattern[:60]
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if cmd:
            return redact_secrets(cmd[:100])[:80]
    if tool_name in ("WebFetch", "WebSearch"):
        return (tool_input.get("url", "") or tool_input.get("query", ""))[:60]
    if tool_name == "Task":
        desc = tool_input.get("description", "")
        if desc:
            return desc[:60]
    for v in tool_input.values():
        if isinstance(v, str) and v:
            return v[:60]
    return ""
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_bot/test_progress.py -v`
Expected: PASS (all tests)

**Step 5: Commit**

```bash
git add src/bot/progress.py tests/unit/test_bot/test_progress.py
git commit -m "feat: move tool icons and summarizer to progress module"
```

---

### Task 3: Implement ProgressMessageManager Core

**Files:**
- Modify: `src/bot/progress.py`
- Test: `tests/unit/test_bot/test_progress.py`

**Step 1: Write the failing tests**

Append to `tests/unit/test_bot/test_progress.py`:

```python
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.progress import ProgressMessageManager


class TestProgressMessageManagerRender:
    """Tests for rendering the activity log into message text."""

    def test_empty_log_shows_working(self) -> None:
        msg = AsyncMock()
        pm = ProgressMessageManager(initial_message=msg, start_time=time.time())
        text = pm.render()
        assert text.startswith("Working...")

    def test_text_entry_rendered_full(self) -> None:
        msg = AsyncMock()
        pm = ProgressMessageManager(initial_message=msg, start_time=0.0)
        pm.activity_log.append(ActivityEntry(kind="text", content="Let me check that file."))
        text = pm.render()
        assert "Let me check that file." in text

    def test_tool_entry_with_detail(self) -> None:
        msg = AsyncMock()
        pm = ProgressMessageManager(initial_message=msg, start_time=0.0)
        pm.activity_log.append(
            ActivityEntry(kind="tool", tool_name="Read", tool_detail="foo.py")
        )
        text = pm.render()
        assert "Read" in text
        assert "foo.py" in text

    def test_running_tool_shows_spinner(self) -> None:
        msg = AsyncMock()
        pm = ProgressMessageManager(initial_message=msg, start_time=0.0)
        pm.activity_log.append(
            ActivityEntry(kind="tool", tool_name="Bash", tool_detail="git status", is_running=True)
        )
        text = pm.render()
        assert "\u23f3" in text  # ⏳

    def test_tool_result_indented(self) -> None:
        msg = AsyncMock()
        pm = ProgressMessageManager(initial_message=msg, start_time=0.0)
        pm.activity_log.append(
            ActivityEntry(
                kind="tool", tool_name="Bash", tool_detail="git commit",
                tool_result="[main abc1234] docs: add design",
            )
        )
        text = pm.render()
        assert "  \u23bf" in text or "\u23bf" in text  # ⎿ character
        assert "abc1234" in text

    def test_thinking_indicator(self) -> None:
        msg = AsyncMock()
        pm = ProgressMessageManager(initial_message=msg, start_time=0.0)
        pm.activity_log.append(
            ActivityEntry(kind="thinking", content="Thinking", is_running=True)
        )
        text = pm.render()
        assert "Thinking" in text

    def test_no_entry_cap(self) -> None:
        """All entries are rendered, no 15-entry cap."""
        msg = AsyncMock()
        pm = ProgressMessageManager(initial_message=msg, start_time=0.0)
        for i in range(25):
            pm.activity_log.append(
                ActivityEntry(kind="tool", tool_name="Read", tool_detail=f"file{i}.py")
            )
        text = pm.render()
        assert "file0.py" in text
        assert "file24.py" in text


class TestProgressMessageManagerFinalize:
    """Tests for finalization (completion)."""

    def test_finalize_changes_header(self) -> None:
        msg = AsyncMock()
        pm = ProgressMessageManager(initial_message=msg, start_time=time.time() - 42)
        text_before = pm.render()
        assert "Working..." in text_before

        finalized = pm.render(done=True)
        assert "Done" in finalized
        assert "Working..." not in finalized

    def test_finalize_removes_spinner(self) -> None:
        msg = AsyncMock()
        pm = ProgressMessageManager(initial_message=msg, start_time=0.0)
        pm.activity_log.append(
            ActivityEntry(kind="tool", tool_name="Bash", is_running=True)
        )
        text = pm.render(done=True)
        assert "\u23f3" not in text  # no ⏳
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_bot/test_progress.py::TestProgressMessageManagerRender -v`
Expected: FAIL with `ImportError: cannot import name 'ProgressMessageManager'`

**Step 3: Write minimal implementation**

Add to `src/bot/progress.py`:

```python
import time as _time
from typing import Any


class ProgressMessageManager:
    """Manages persistent Telegram progress message(s) with activity log.

    Accumulates ActivityEntry items and renders them into one or more
    Telegram messages, rolling over at MAX_MSG_LENGTH.
    """

    MAX_MSG_LENGTH = 4000  # Leave margin under Telegram's 4096 limit
    EDIT_INTERVAL = 2.0  # Minimum seconds between message edits

    def __init__(
        self,
        initial_message: Any,
        start_time: float,
    ) -> None:
        self.messages: list[Any] = [initial_message]
        self.activity_log: list[ActivityEntry] = []
        self.start_time = start_time
        self._rendered_up_to: int = 0  # entries frozen in earlier messages
        self._last_edit_time: float = 0.0
        self._thinking_tick: int = 0  # for dot animation

    def render(self, done: bool = False) -> str:
        """Build message text from activity log entries."""
        elapsed = _time.time() - self.start_time
        if done:
            header = f"Done ({elapsed:.0f}s)"
        else:
            header = f"Working... ({elapsed:.0f}s)"

        if not self.activity_log:
            return header

        lines: list[str] = [header, ""]
        prev_kind = ""

        for entry in self.activity_log[self._rendered_up_to :]:
            # Blank line between different kinds for readability
            if prev_kind and prev_kind != entry.kind and entry.kind != "thinking":
                lines.append("")

            if entry.kind == "text":
                lines.append(entry.content)
            elif entry.kind == "tool":
                icon = tool_icon(entry.tool_name)
                detail_suffix = f": {entry.tool_detail}" if entry.tool_detail else ""
                spinner = " \u23f3" if entry.is_running and not done else ""
                lines.append(f"{icon} {entry.tool_name}{detail_suffix}{spinner}")
                if entry.tool_result:
                    lines.append(f"  \u23bf {entry.tool_result}")
            elif entry.kind == "thinking":
                if entry.is_running and not done:
                    dots = "." * (1 + self._thinking_tick % 3)
                    lines.append(f"\U0001f4ad Thinking{dots}")
                else:
                    lines.append(f"\U0001f4ad Thinking (done)")

            prev_kind = entry.kind

        return "\n".join(lines)

    async def update(self) -> None:
        """Edit the current progress message if throttle interval has passed."""
        now = _time.time()
        if (now - self._last_edit_time) < self.EDIT_INTERVAL:
            return

        self._last_edit_time = now
        self._thinking_tick += 1

        text = self.render()

        # Check if we need to roll over to a new message
        if len(text) > self.MAX_MSG_LENGTH:
            await self._rollover(text)
            return

        try:
            await self.messages[-1].edit_text(text)
        except Exception:
            pass

    async def _rollover(self, full_text: str) -> None:
        """Finalize current message and start a new one for overflow."""
        # Freeze current entries
        freeze_text = self.render()
        # Truncate to fit
        if len(freeze_text) > self.MAX_MSG_LENGTH:
            freeze_text = freeze_text[: self.MAX_MSG_LENGTH - 20] + "\n\n(continued...)"
        try:
            await self.messages[-1].edit_text(freeze_text)
        except Exception:
            pass

        self._rendered_up_to = len(self.activity_log)

        # Send new message
        try:
            chat = self.messages[-1].chat
            new_msg = await chat.send_message(
                f"Working... ({_time.time() - self.start_time:.0f}s) (continued)"
            )
            self.messages.append(new_msg)
        except Exception:
            pass

    async def finalize(self) -> None:
        """Mark progress as done — update header, remove spinners, keep message."""
        for entry in self.activity_log:
            entry.is_running = False
        text = self.render(done=True)
        if len(text) > self.MAX_MSG_LENGTH:
            text = text[: self.MAX_MSG_LENGTH - 10] + "\n..."
        try:
            await self.messages[-1].edit_text(text)
        except Exception:
            pass
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_bot/test_progress.py -v`
Expected: PASS (all tests)

**Step 5: Commit**

```bash
git add src/bot/progress.py tests/unit/test_bot/test_progress.py
git commit -m "feat: implement ProgressMessageManager with rendering and rollover"
```

---

### Task 4: Add Tool Result Summarizer

**Files:**
- Modify: `src/bot/progress.py`
- Test: `tests/unit/test_bot/test_progress.py`

**Step 1: Write the failing test**

Append to `tests/unit/test_bot/test_progress.py`:

```python
from src.bot.progress import summarize_tool_result


class TestSummarizeToolResult:
    """Tests for summarize_tool_result()."""

    def test_short_result_unchanged(self) -> None:
        result = summarize_tool_result("Bash", "[main abc1234] docs: add design")
        assert result == "[main abc1234] docs: add design"

    def test_long_result_truncated(self) -> None:
        long_text = "x" * 200
        result = summarize_tool_result("Bash", long_text)
        assert len(result) <= 120

    def test_multiline_takes_first_line(self) -> None:
        text = "line one\nline two\nline three"
        result = summarize_tool_result("Bash", text)
        assert result == "line one"

    def test_empty_result(self) -> None:
        result = summarize_tool_result("Read", "")
        assert result == ""

    def test_write_extracts_line_count(self) -> None:
        """If tool result mentions 'Wrote N lines', extract that."""
        result = summarize_tool_result(
            "Write", "Wrote 94 lines to docs/plans/design.md"
        )
        assert "94 lines" in result
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_bot/test_progress.py::TestSummarizeToolResult -v`
Expected: FAIL with `ImportError: cannot import name 'summarize_tool_result'`

**Step 3: Write minimal implementation**

Add to `src/bot/progress.py`:

```python
def summarize_tool_result(tool_name: str, raw: str) -> str:
    """Extract a brief summary from raw tool result content."""
    if not raw:
        return ""
    # Take first non-empty line
    first_line = ""
    for line in raw.split("\n"):
        stripped = line.strip()
        if stripped:
            first_line = stripped
            break
    if not first_line:
        return ""
    # Truncate to reasonable length
    if len(first_line) > 100:
        return first_line[:100] + "..."
    return first_line
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_bot/test_progress.py::TestSummarizeToolResult -v`
Expected: PASS (5 tests)

**Step 5: Commit**

```bash
git add src/bot/progress.py tests/unit/test_bot/test_progress.py
git commit -m "feat: add tool result summarizer for progress display"
```

---

### Task 5: Forward Tool Results in UserClient

**Files:**
- Modify: `src/claude/user_client.py:213-276`
- Test: `tests/unit/test_claude/test_user_client.py`

**Step 1: Write the failing test**

Find the existing test file. Add a test that verifies `"user"` events are forwarded as `"tool_result"`:

```python
# Append to tests/unit/test_claude/test_user_client.py

async def test_tool_result_events_forwarded(self):
    """UserClient forwards 'user' SDK events as 'tool_result' to on_stream."""
    # This test verifies that UserMessage events (containing tool results)
    # are forwarded to the on_stream callback with type "tool_result".
    received_events = []

    async def on_stream(event_type: str, content: Any) -> None:
        received_events.append((event_type, content))

    # ... (mock setup to emit a UserMessage event through the stream)
    # After processing, verify:
    tool_results = [(t, c) for t, c in received_events if t == "tool_result"]
    assert len(tool_results) >= 1
```

Note: The exact test setup depends on how existing `test_user_client.py` mocks the SDK. Read the existing test patterns and follow them. The key assertion is that a `UserMessage` with content goes through as `("tool_result", content_string)`.

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_claude/test_user_client.py::test_tool_result_events_forwarded -v`
Expected: FAIL — the event is currently silently dropped

**Step 3: Write minimal implementation**

In `src/claude/user_client.py`, in `_process_item()` method, after the `elif event.type == "thinking"` block (line 251-252), add:

```python
                elif event.type == "user" and event.content and item.on_stream:
                    await item.on_stream("tool_result", event.content)
```

The full block (lines 234-253) becomes:

```python
                if event.type == "result":
                    response_text = event.content or ""
                    result_session_id = event.session_id
                    cost = event.cost or 0.0
                elif event.type == "text" and event.content and item.on_stream:
                    await item.on_stream(event.type, event.content)
                elif event.type == "tool_use":
                    if not is_partial:
                        num_turns += 1
                    if item.on_stream:
                        await item.on_stream(
                            event.type,
                            {
                                "name": event.tool_name or "",
                                "input": event.tool_input or {},
                            },
                        )
                elif event.type == "thinking" and event.content and item.on_stream:
                    await item.on_stream(event.type, event.content)
                elif event.type == "user" and event.content and item.on_stream:
                    await item.on_stream("tool_result", event.content)
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_claude/test_user_client.py -v`
Expected: PASS (all tests including new one)

**Step 5: Commit**

```bash
git add src/claude/user_client.py tests/unit/test_claude/test_user_client.py
git commit -m "feat: forward tool result events from UserClient to stream callback"
```

---

### Task 6: Rewrite Stream Callback

Replace `_make_stream_callback()` in the orchestrator to use `ProgressMessageManager` and `ActivityEntry`.

**Files:**
- Modify: `src/bot/orchestrator.py:1030-1081`
- Test: `tests/unit/test_bot/test_progress.py`

**Step 1: Write the failing test**

Append to `tests/unit/test_bot/test_progress.py`:

```python
from src.bot.progress import build_stream_callback, ActivityEntry, ProgressMessageManager


class TestBuildStreamCallback:
    """Tests for the stream callback factory."""

    @pytest.fixture
    def pm(self) -> ProgressMessageManager:
        msg = AsyncMock()
        return ProgressMessageManager(initial_message=msg, start_time=0.0)

    async def test_tool_use_appends_entry(self, pm: ProgressMessageManager) -> None:
        cb = build_stream_callback(pm)
        await cb("tool_use", {"name": "Read", "input": {"file_path": "/src/foo.py"}})
        assert len(pm.activity_log) == 1
        assert pm.activity_log[0].kind == "tool"
        assert pm.activity_log[0].tool_name == "Read"
        assert pm.activity_log[0].is_running is True

    async def test_text_appends_full_content(self, pm: ProgressMessageManager) -> None:
        cb = build_stream_callback(pm)
        await cb("text", "Hello ")
        await cb("text", "world, this is a long message.")
        # Consecutive text should merge into one entry
        assert len(pm.activity_log) == 1
        assert pm.activity_log[0].content == "Hello world, this is a long message."

    async def test_text_not_truncated(self, pm: ProgressMessageManager) -> None:
        cb = build_stream_callback(pm)
        long_text = "x" * 500
        await cb("text", long_text)
        assert len(pm.activity_log[0].content) == 500  # No truncation!

    async def test_thinking_creates_indicator(self, pm: ProgressMessageManager) -> None:
        cb = build_stream_callback(pm)
        await cb("thinking", "Let me think about this...")
        assert len(pm.activity_log) == 1
        assert pm.activity_log[0].kind == "thinking"
        assert pm.activity_log[0].is_running is True

    async def test_tool_result_attaches_to_last_tool(self, pm: ProgressMessageManager) -> None:
        cb = build_stream_callback(pm)
        await cb("tool_use", {"name": "Bash", "input": {"command": "git status"}})
        await cb("tool_result", "On branch main\nnothing to commit")
        assert pm.activity_log[0].tool_result == "On branch main"

    async def test_new_event_closes_running(self, pm: ProgressMessageManager) -> None:
        cb = build_stream_callback(pm)
        await cb("tool_use", {"name": "Read", "input": {}})
        assert pm.activity_log[0].is_running is True
        await cb("tool_use", {"name": "Write", "input": {}})
        assert pm.activity_log[0].is_running is False
        assert pm.activity_log[1].is_running is True

    async def test_text_after_tool_creates_new_entry(self, pm: ProgressMessageManager) -> None:
        cb = build_stream_callback(pm)
        await cb("text", "first")
        await cb("tool_use", {"name": "Read", "input": {}})
        await cb("text", "second")
        assert len(pm.activity_log) == 3
        assert pm.activity_log[0].content == "first"
        assert pm.activity_log[2].content == "second"
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_bot/test_progress.py::TestBuildStreamCallback -v`
Expected: FAIL with `ImportError: cannot import name 'build_stream_callback'`

**Step 3: Write minimal implementation**

Add to `src/bot/progress.py`:

```python
from typing import Callable, Awaitable


def _close_running_entry(activity_log: list[ActivityEntry]) -> None:
    """Mark the last running entry as done."""
    for entry in reversed(activity_log):
        if entry.is_running:
            entry.is_running = False
            if entry.kind == "thinking":
                entry.content = "Thinking (done)"
            break


def _attach_result_to_last_tool(
    activity_log: list[ActivityEntry], raw_content: str
) -> None:
    """Attach a brief result summary to the most recent tool entry."""
    for entry in reversed(activity_log):
        if entry.kind == "tool":
            entry.tool_result = summarize_tool_result(entry.tool_name, raw_content)
            break


def build_stream_callback(
    progress_manager: ProgressMessageManager,
) -> Callable[[str, Any], Awaitable[None]]:
    """Create a stream callback that populates the progress manager's activity log.

    The callback signature matches UserClient._process_item:
    on_stream(event_type: str, content: Any)
    """
    activity_log = progress_manager.activity_log

    async def _on_stream(event_type: str, content: Any) -> None:
        # 1. Close any running entry before processing new event
        if event_type != "tool_result":
            _close_running_entry(activity_log)

        # 2. Handle the new event
        if event_type == "tool_use" and isinstance(content, dict):
            name = content.get("name", "unknown")
            detail = summarize_tool_input(name, content.get("input", {}))
            activity_log.append(
                ActivityEntry(
                    kind="tool",
                    tool_name=name,
                    tool_detail=detail,
                    is_running=True,
                )
            )

        elif event_type == "text" and content:
            text = str(content)
            # Merge consecutive text deltas into one entry
            if activity_log and activity_log[-1].kind == "text":
                activity_log[-1].content += text
            else:
                activity_log.append(ActivityEntry(kind="text", content=text))

        elif event_type == "thinking":
            # Only create one thinking entry; merge consecutive
            if not (
                activity_log
                and activity_log[-1].kind == "thinking"
                and activity_log[-1].is_running
            ):
                activity_log.append(
                    ActivityEntry(kind="thinking", content="Thinking", is_running=True)
                )

        elif event_type == "tool_result" and content:
            _attach_result_to_last_tool(activity_log, str(content))

        # 3. Update progress display (throttled internally)
        await progress_manager.update()

    return _on_stream
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_bot/test_progress.py -v`
Expected: PASS (all tests)

**Step 5: Commit**

```bash
git add src/bot/progress.py tests/unit/test_bot/test_progress.py
git commit -m "feat: implement stream callback with full text and tool result support"
```

---

### Task 7: Integrate into Orchestrator — Replace Progress Logic

Wire `ProgressMessageManager` and `build_stream_callback` into `agentic_text()`. Remove old methods.

**Files:**
- Modify: `src/bot/orchestrator.py`

**Step 1: Update imports at top of orchestrator.py**

Add import (after line 36):

```python
from .progress import ProgressMessageManager, build_stream_callback
```

**Step 2: Replace `agentic_text()` progress logic (lines 1175-1259)**

Change this section:

```python
# OLD (lines 1175-1195):
        verbose_level = self._get_verbose_level(context)
        progress_msg = await update.message.reply_text("Working...")
        ...
        tool_log: List[Dict[str, Any]] = []
        start_time = time.time()
        on_stream = self._make_stream_callback(
            verbose_level, progress_msg, tool_log, start_time
        )
```

To:

```python
# NEW:
        progress_msg = await update.message.reply_text("Working...")
        start_time = time.time()
        progress_manager = ProgressMessageManager(
            initial_message=progress_msg, start_time=start_time
        )
        on_stream = build_stream_callback(progress_manager)
```

**Step 3: Replace `progress_msg.delete()` with `progress_manager.finalize()` (line 1259)**

Change:

```python
        await progress_msg.delete()
```

To:

```python
        await progress_manager.finalize()
```

**Step 4: Run tests to verify nothing is broken**

Run: `uv run pytest tests/unit/test_orchestrator.py -v`

Some existing tests reference `_make_stream_callback` — those will need updating in the next task. For now, verify `agentic_text`-related tests still pass by checking the overall test suite:

Run: `uv run pytest tests/ -x -q`
Expected: Some tests may fail if they reference old methods. Note which ones.

**Step 5: Commit**

```bash
git add src/bot/orchestrator.py
git commit -m "feat: wire ProgressMessageManager into agentic_text"
```

---

### Task 8: Remove Verbose Level System

Remove `/verbose` command, `_get_verbose_level()`, `_format_verbose_progress()`, `_make_stream_callback()`, and `_summarize_tool_input()` from orchestrator. Remove `verbose_level` from settings. Update `registered_commands` set, `get_bot_commands()`, and `/start` message.

**Files:**
- Modify: `src/bot/orchestrator.py`
- Modify: `src/config/settings.py:150-160`
- Modify: `tests/unit/test_orchestrator.py`

**Step 1: Remove from orchestrator.py**

1. Delete `_TOOL_ICONS` dict and `_tool_icon()` function (lines 90-113) — now in `progress.py`
2. Delete `_SECRET_PATTERNS` list and `_redact_secrets()` function (lines 49-87) — now in `progress.py`
3. Delete `_get_verbose_level()` method (lines 725-730)
4. Delete `agentic_verbose()` method (lines 732-765)
5. Delete `_format_verbose_progress()` method (lines 870-912)
6. Delete `_summarize_tool_input()` static method (lines 914-942)
7. Delete `_make_stream_callback()` method (lines 1030-1081)
8. Remove `("verbose", self.agentic_verbose)` from `_register_agentic_handlers` (line 308)
9. Remove `"verbose"` from `registered_commands` set in `agentic_text` (line 1109)
10. Remove `BotCommand("verbose", ...)` from `get_bot_commands` (line 420)
11. Remove `/verbose` line from `agentic_start` help text (line 580)

**Step 2: Remove from settings.py**

Delete the `verbose_level` field (lines 150-160):

```python
    # DELETE THIS BLOCK:
    # Output verbosity (0=quiet, 1=normal, 2=detailed)
    verbose_level: int = Field(
        1,
        description=(...),
        ge=0,
        le=2,
    )
```

**Step 3: Update tests**

In `tests/unit/test_orchestrator.py`:

1. Remove `"verbose"` from command registration assertion (line 107)
2. Remove `"verbose"` from the registered commands list (line 173)
3. Update `test_stream_callback_independent_of_typing` test — it references `_make_stream_callback`. Replace with a test that verifies `build_stream_callback` is used instead, or delete the test (the functionality is now tested in `test_progress.py`).

**Step 4: Run all tests**

Run: `uv run pytest tests/ -x -q`
Expected: PASS (all tests)

**Step 5: Run linter**

Run: `uv run mypy src/bot/orchestrator.py src/bot/progress.py src/config/settings.py`
Expected: PASS (no type errors)

**Step 6: Commit**

```bash
git add src/bot/orchestrator.py src/config/settings.py tests/unit/test_orchestrator.py
git commit -m "refactor: remove verbose level system, old progress methods"
```

---

### Task 9: Update Orchestrator Imports — Remove Stale References

After removing the old functions, clean up any remaining references to old imports or functions across the codebase.

**Files:**
- Check: all files in `src/` and `tests/` that might reference verbose, `_tool_icon`, `_redact_secrets`, `_TOOL_ICONS`

**Step 1: Search for stale references**

Run:
```bash
grep -rn "_tool_icon\|_TOOL_ICONS\|_redact_secrets\|_SECRET_PATTERNS\|verbose_level\|_format_verbose\|_summarize_tool_input\|_make_stream_callback\|_get_verbose_level\|agentic_verbose" src/ tests/ --include="*.py"
```

Fix any remaining references:
- If `_redact_secrets` is used elsewhere, import from `src.bot.progress` instead
- If `verbose_level` appears in test fixtures, remove it

**Step 2: Run full test suite**

Run: `uv run pytest tests/ -x -q`
Expected: PASS (all 552+ tests)

**Step 3: Run full linter**

Run: `uv run make lint`
Expected: PASS

**Step 4: Commit (if changes needed)**

```bash
git add -A
git commit -m "chore: clean up stale verbose and progress references"
```

---

### Task 10: Update CLAUDE.md Documentation

Remove verbose level documentation from CLAUDE.md since the feature is removed.

**Files:**
- Modify: `CLAUDE.md`

**Step 1: Remove verbose documentation**

In `CLAUDE.md`, find and remove:

1. The `Output verbosity: VERBOSE_LEVEL ...` paragraph in the Configuration section
2. Remove `/verbose` from the "Agentic mode commands" list in "Adding a New Bot Command" section

**Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: remove verbose level references from CLAUDE.md"
```

---

### Task 11: Integration Test — End-to-End Progress Flow

Write an integration-level test that exercises the full flow: callback receives events, progress manager accumulates them, render produces correct output.

**Files:**
- Test: `tests/unit/test_bot/test_progress.py`

**Step 1: Write the integration test**

Append to `tests/unit/test_bot/test_progress.py`:

```python
class TestEndToEndProgressFlow:
    """Integration test: full callback -> progress -> render flow."""

    async def test_realistic_session(self) -> None:
        """Simulate a realistic Claude session and verify rendered output."""
        msg = AsyncMock()
        # Make edit_text a no-op (we just check render output)
        msg.edit_text = AsyncMock()

        pm = ProgressMessageManager(initial_message=msg, start_time=0.0)
        # Disable throttle for testing
        pm.EDIT_INTERVAL = 0.0
        cb = build_stream_callback(pm)

        # Claude says something
        await cb("text", "Let me check that file.")

        # Claude reads a file
        await cb("tool_use", {"name": "Read", "input": {"file_path": "/src/foo.py"}})
        await cb("tool_result", "def main():\n    pass\n")

        # Claude thinks
        await cb("thinking", "I see the issue...")

        # Claude edits
        await cb("tool_use", {"name": "Edit", "input": {"file_path": "/src/foo.py"}})
        await cb("tool_result", "Applied 1 edit")

        # Claude explains
        await cb("text", "I've fixed the bug in foo.py.")

        # Verify the rendered output
        text = pm.render(done=True)

        # All intermediate text present (not truncated)
        assert "Let me check that file." in text
        assert "I've fixed the bug in foo.py." in text

        # Tool calls present
        assert "Read" in text
        assert "Edit" in text

        # Tool results present
        assert "def main():" in text
        assert "Applied 1 edit" in text

        # Thinking indicator present
        assert "Thinking" in text

        # Header shows done
        assert "Done" in text

        # No spinners in final output
        assert "\u23f3" not in text

    async def test_message_rollover(self) -> None:
        """Progress rolls over to new message when hitting char limit."""
        msg = AsyncMock()
        msg.edit_text = AsyncMock()
        msg.chat = AsyncMock()
        new_msg = AsyncMock()
        new_msg.edit_text = AsyncMock()
        msg.chat.send_message = AsyncMock(return_value=new_msg)

        pm = ProgressMessageManager(initial_message=msg, start_time=0.0)
        pm.EDIT_INTERVAL = 0.0

        cb = build_stream_callback(pm)

        # Add enough content to exceed MAX_MSG_LENGTH
        for i in range(100):
            await cb("text", f"This is line number {i} with some extra text to fill space. ")
            await cb("tool_use", {"name": "Read", "input": {"file_path": f"/src/file{i}.py"}})
            await cb("tool_result", f"Content of file {i}")

        # Should have rolled over to at least 2 messages
        assert len(pm.messages) >= 2
```

**Step 2: Run tests**

Run: `uv run pytest tests/unit/test_bot/test_progress.py::TestEndToEndProgressFlow -v`
Expected: PASS

**Step 3: Run full test suite one final time**

Run: `uv run pytest tests/ -x -q`
Expected: PASS (all tests)

**Step 4: Commit**

```bash
git add tests/unit/test_bot/test_progress.py
git commit -m "test: add end-to-end integration tests for rich progress flow"
```

---

### Task 12: Final Verification

**Step 1: Run full test suite with coverage**

Run: `uv run pytest tests/ --cov=src -q`
Expected: PASS, coverage for `src/bot/progress.py` should be >80%

**Step 2: Run linter**

Run: `make lint`
Expected: PASS

**Step 3: Run type checker**

Run: `uv run mypy src`
Expected: PASS (or only pre-existing issues)

**Step 4: Verify no regressions**

Run: `uv run pytest tests/ -x -q`
Expected: Same number of tests passing as baseline (552+)

**Step 5: Final commit if any fixups needed**

```bash
git add -A
git commit -m "chore: final cleanup for rich progress display"
```
