"""Tests for ProgressMessageManager and ActivityEntry."""

import time
from unittest.mock import AsyncMock

import pytest

from src.bot.progress import (
    ActivityEntry,
    ProgressMessageManager,
    _extract_tool_result_text,
    build_stream_callback,
    summarize_tool_input,
    summarize_tool_result,
    tool_icon,
)


# ---------------------------------------------------------------------------
# Task 1: ActivityEntry
# ---------------------------------------------------------------------------


class TestActivityEntry:
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


# ---------------------------------------------------------------------------
# Task 2: tool_icon and summarize_tool_input
# ---------------------------------------------------------------------------


class TestToolIcon:
    def test_known_tool(self) -> None:
        assert tool_icon("Read") == "\U0001f4d6"
        assert tool_icon("Bash") == "\U0001f4bb"

    def test_unknown_tool_returns_wrench(self) -> None:
        assert tool_icon("SomeNewTool") == "\U0001f527"


class TestSummarizeToolInput:
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


# ---------------------------------------------------------------------------
# Task 3: ProgressMessageManager
# ---------------------------------------------------------------------------


class TestProgressMessageManagerRender:
    def test_empty_log_shows_working(self) -> None:
        msg = AsyncMock()
        pm = ProgressMessageManager(initial_message=msg, start_time=time.time())
        text = pm.render()
        assert text.startswith("Working...")

    def test_text_entry_skipped_in_render(self) -> None:
        """Text entries are not rendered — response is sent separately."""
        msg = AsyncMock()
        pm = ProgressMessageManager(initial_message=msg, start_time=0.0)
        pm.activity_log.append(ActivityEntry(kind="text", content="Let me check that file."))
        text = pm.render()
        assert "Let me check that file." not in text

    def test_tool_entry_with_detail(self) -> None:
        msg = AsyncMock()
        pm = ProgressMessageManager(initial_message=msg, start_time=0.0)
        pm.activity_log.append(ActivityEntry(kind="tool", tool_name="Read", tool_detail="foo.py"))
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
        assert "\u23f3" in text

    def test_tool_result_indented(self) -> None:
        msg = AsyncMock()
        pm = ProgressMessageManager(initial_message=msg, start_time=0.0)
        pm.activity_log.append(
            ActivityEntry(
                kind="tool",
                tool_name="Bash",
                tool_detail="git commit",
                tool_result="[main abc1234] docs: add design",
            )
        )
        text = pm.render()
        assert "abc1234" in text

    def test_thinking_indicator(self) -> None:
        msg = AsyncMock()
        pm = ProgressMessageManager(initial_message=msg, start_time=0.0)
        pm.activity_log.append(ActivityEntry(kind="thinking", content="Thinking", is_running=True))
        text = pm.render()
        assert "Thinking" in text

    def test_no_entry_cap(self) -> None:
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
        pm.activity_log.append(ActivityEntry(kind="tool", tool_name="Bash", is_running=True))
        text = pm.render(done=True)
        assert "\u23f3" not in text


# ---------------------------------------------------------------------------
# Task 4: summarize_tool_result
# ---------------------------------------------------------------------------


class TestSummarizeToolResult:
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
        result = summarize_tool_result("Write", "Wrote 94 lines to docs/plans/design.md")
        assert "94 lines" in result


# ---------------------------------------------------------------------------
# _extract_tool_result_text
# ---------------------------------------------------------------------------


class TestExtractToolResultText:
    def test_none_returns_empty(self) -> None:
        assert _extract_tool_result_text(None) == ""

    def test_plain_string_passthrough(self) -> None:
        assert _extract_tool_result_text("hello") == "hello"

    def test_block_with_string_content(self) -> None:
        """Simulate ToolResultBlock(content='some text')."""

        class FakeBlock:
            content = "file contents here"

        assert _extract_tool_result_text([FakeBlock()]) == "file contents here"

    def test_block_with_nested_text_blocks(self) -> None:
        """Simulate ToolResultBlock(content=[TextBlock(text='...')])."""

        class FakeTextBlock:
            def __init__(self, text: str) -> None:
                self.text = text

        class FakeBlock:
            content = [FakeTextBlock("line one"), FakeTextBlock("line two")]

        result = _extract_tool_result_text([FakeBlock()])
        assert "line one" in result
        assert "line two" in result

    def test_block_with_text_attr(self) -> None:
        """Block that has .text but no .content."""

        class TextOnlyBlock:
            """Block with only a text attribute, no content."""

            def __init__(self) -> None:
                self.text = "direct text"

        block = TextOnlyBlock()
        assert not hasattr(block, "content")
        assert _extract_tool_result_text([block]) == "direct text"

    def test_multiple_blocks_concatenated(self) -> None:
        class FakeBlock:
            def __init__(self, content: str) -> None:
                self.content = content

        blocks = [FakeBlock("first"), FakeBlock("second")]
        result = _extract_tool_result_text(blocks)
        assert result == "first\nsecond"


# ---------------------------------------------------------------------------
# Task 6: build_stream_callback
# ---------------------------------------------------------------------------


class TestBuildStreamCallback:
    @pytest.fixture
    def pm(self) -> ProgressMessageManager:
        msg = AsyncMock()
        msg.edit_text = AsyncMock()
        pm = ProgressMessageManager(initial_message=msg, start_time=0.0)
        pm.EDIT_INTERVAL = 0.0  # disable throttle for tests
        return pm

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
        assert len(pm.activity_log) == 1
        assert pm.activity_log[0].content == "Hello world, this is a long message."

    async def test_text_not_truncated(self, pm: ProgressMessageManager) -> None:
        cb = build_stream_callback(pm)
        long_text = "x" * 500
        await cb("text", long_text)
        assert len(pm.activity_log[0].content) == 500

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


# ---------------------------------------------------------------------------
# Task 11: End-to-end integration tests
# ---------------------------------------------------------------------------


class TestEndToEndProgressFlow:
    """Integration test: full callback -> progress -> render flow."""

    async def test_realistic_session(self) -> None:
        """Simulate a realistic Claude session and verify rendered output."""
        msg = AsyncMock()
        msg.edit_text = AsyncMock()

        pm = ProgressMessageManager(initial_message=msg, start_time=0.0)
        pm.EDIT_INTERVAL = 0.0  # disable throttle for testing
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

        # Text entries are NOT in progress (delivered separately as final response)
        assert "Let me check that file." not in text
        assert "I've fixed the bug in foo.py." not in text

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
