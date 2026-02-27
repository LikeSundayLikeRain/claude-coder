"""Tests for StreamHandler: SDK message stream -> structured events."""

from unittest.mock import MagicMock

from src.claude.stream_handler import StreamEvent, StreamHandler


class TestStreamHandlerText:
    """Tests for text extraction from AssistantMessage."""

    def setup_method(self) -> None:
        self.handler = StreamHandler()

    def _make_assistant(self, blocks: list) -> MagicMock:
        msg = MagicMock()
        msg.__class__.__name__ = "AssistantMessage"
        msg.content = blocks
        return msg

    def _make_text_block(self, text: str) -> MagicMock:
        block = MagicMock()
        block.type = "text"
        block.text = text
        return block

    def test_single_text_block(self) -> None:
        block = self._make_text_block("Hello")
        msg = self._make_assistant([block])
        event = self.handler.extract_content(msg)
        assert event.type == "text"
        assert event.content == "Hello"

    def test_multiple_text_blocks_joined(self) -> None:
        blocks = [
            self._make_text_block("Hello"),
            self._make_text_block(" world"),
        ]
        msg = self._make_assistant(blocks)
        event = self.handler.extract_content(msg)
        assert event.type == "text"
        assert event.content == "Hello world"

    def test_empty_content_list(self) -> None:
        msg = self._make_assistant([])
        event = self.handler.extract_content(msg)
        assert event.type == "text"
        assert event.content == ""


class TestStreamHandlerThinking:
    """Tests for thinking extraction from AssistantMessage."""

    def setup_method(self) -> None:
        self.handler = StreamHandler()

    def test_thinking_block(self) -> None:
        block = MagicMock()
        block.type = "thinking"
        block.thinking = "Let me think..."

        msg = MagicMock()
        msg.__class__.__name__ = "AssistantMessage"
        msg.content = [block]

        event = self.handler.extract_content(msg)
        assert event.type == "thinking"
        assert event.content == "Let me think..."

    def test_thinking_block_not_dropped(self) -> None:
        """ThinkingBlock must not be silently dropped (regression guard)."""
        block = MagicMock()
        block.type = "thinking"
        block.thinking = "Deep reasoning here"

        msg = MagicMock()
        msg.__class__.__name__ = "AssistantMessage"
        msg.content = [block]

        event = self.handler.extract_content(msg)
        assert event.type == "thinking"
        assert event.content == "Deep reasoning here"
        assert event.tool_name is None


class TestStreamHandlerToolUse:
    """Tests for tool_use extraction from AssistantMessage."""

    def setup_method(self) -> None:
        self.handler = StreamHandler()

    def test_tool_use_block(self) -> None:
        block = MagicMock()
        block.type = "tool_use"
        block.name = "Read"
        block.input = {"file_path": "/tmp/test.py"}

        msg = MagicMock()
        msg.__class__.__name__ = "AssistantMessage"
        msg.content = [block]

        event = self.handler.extract_content(msg)
        assert event.type == "tool_use"
        assert event.tool_name == "Read"
        assert event.tool_input == {"file_path": "/tmp/test.py"}
        assert event.content is None

    def test_tool_use_with_complex_input(self) -> None:
        block = MagicMock()
        block.type = "tool_use"
        block.name = "Bash"
        block.input = {"command": "ls -la", "timeout": 30}

        msg = MagicMock()
        msg.__class__.__name__ = "AssistantMessage"
        msg.content = [block]

        event = self.handler.extract_content(msg)
        assert event.type == "tool_use"
        assert event.tool_name == "Bash"
        assert event.tool_input == {"command": "ls -la", "timeout": 30}


class TestStreamHandlerResult:
    """Tests for result extraction from ResultMessage."""

    def setup_method(self) -> None:
        self.handler = StreamHandler()

    def test_result_message(self) -> None:
        msg = MagicMock()
        msg.__class__.__name__ = "ResultMessage"
        msg.result = "Task completed"
        msg.session_id = "abc-123"
        msg.total_cost_usd = 0.005

        event = self.handler.extract_content(msg)
        assert event.type == "result"
        assert event.content == "Task completed"
        assert event.session_id == "abc-123"
        assert event.cost == 0.005

    def test_result_message_no_cost(self) -> None:
        msg = MagicMock()
        msg.__class__.__name__ = "ResultMessage"
        msg.result = "Done"
        msg.session_id = "xyz-999"
        msg.total_cost_usd = None

        event = self.handler.extract_content(msg)
        assert event.type == "result"
        assert event.cost is None


class TestStreamHandlerMixedContent:
    """Tests for mixed content blocks in AssistantMessage."""

    def setup_method(self) -> None:
        self.handler = StreamHandler()

    def test_multiple_text_blocks_concatenated(self) -> None:
        blocks = []
        for text in ["Part one. ", "Part two. ", "Part three."]:
            b = MagicMock()
            b.type = "text"
            b.text = text
            blocks.append(b)

        msg = MagicMock()
        msg.__class__.__name__ = "AssistantMessage"
        msg.content = blocks

        event = self.handler.extract_content(msg)
        assert event.type == "text"
        assert event.content == "Part one. Part two. Part three."

    def test_mixed_text_and_unknown_blocks_extracts_text(self) -> None:
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Hello"

        other_block = MagicMock()
        other_block.type = "something_else"

        msg = MagicMock()
        msg.__class__.__name__ = "AssistantMessage"
        msg.content = [text_block, other_block]

        event = self.handler.extract_content(msg)
        assert event.type == "text"
        assert event.content == "Hello"


class TestStreamHandlerUnknown:
    """Tests for unknown message types."""

    def setup_method(self) -> None:
        self.handler = StreamHandler()

    def test_unknown_message_type(self) -> None:
        msg = MagicMock()
        msg.__class__.__name__ = "SomeOtherMessage"

        event = self.handler.extract_content(msg)
        assert event.type == "unknown"
        assert event.content is None

    def test_user_message(self) -> None:
        msg = MagicMock()
        msg.__class__.__name__ = "UserMessage"
        msg.content = "ping"

        event = self.handler.extract_content(msg)
        assert event.type == "user"
        assert event.content == "ping"


class TestStreamHandlerPartialMessages:
    """Tests for SDK StreamEvent (partial/incremental messages)."""

    def setup_method(self) -> None:
        self.handler = StreamHandler()

    def _make_sdk_stream_event(self, event_dict: dict) -> MagicMock:
        msg = MagicMock()
        msg.__class__.__name__ = "StreamEvent"
        msg.event = event_dict
        return msg

    def test_content_block_start_tool_use(self) -> None:
        msg = self._make_sdk_stream_event({
            "type": "content_block_start",
            "content_block": {"type": "tool_use", "name": "Read"},
        })
        event = self.handler.extract_content(msg)
        assert event.type == "tool_use"
        assert event.tool_name == "Read"

    def test_content_block_start_thinking(self) -> None:
        msg = self._make_sdk_stream_event({
            "type": "content_block_start",
            "content_block": {"type": "thinking"},
        })
        event = self.handler.extract_content(msg)
        assert event.type == "thinking"

    def test_content_block_delta_text(self) -> None:
        msg = self._make_sdk_stream_event({
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "Hello world"},
        })
        event = self.handler.extract_content(msg)
        assert event.type == "text"
        assert event.content == "Hello world"

    def test_content_block_delta_thinking(self) -> None:
        msg = self._make_sdk_stream_event({
            "type": "content_block_delta",
            "delta": {"type": "thinking_delta", "thinking": "Let me consider..."},
        })
        event = self.handler.extract_content(msg)
        assert event.type == "thinking"
        assert event.content == "Let me consider..."

    def test_content_block_delta_json_ignored(self) -> None:
        msg = self._make_sdk_stream_event({
            "type": "content_block_delta",
            "delta": {"type": "input_json_delta", "partial_json": '{"file'},
        })
        event = self.handler.extract_content(msg)
        assert event.type == "unknown"

    def test_content_block_stop_ignored(self) -> None:
        msg = self._make_sdk_stream_event({"type": "content_block_stop"})
        event = self.handler.extract_content(msg)
        assert event.type == "unknown"

    def test_message_start_ignored(self) -> None:
        msg = self._make_sdk_stream_event({"type": "message_start"})
        event = self.handler.extract_content(msg)
        assert event.type == "unknown"


class TestStreamEventDataclass:
    """Tests for StreamEvent immutability and defaults."""

    def test_frozen_dataclass(self) -> None:
        event = StreamEvent(type="text", content="hello")
        try:
            event.content = "mutated"  # type: ignore[misc]
            assert False, "Should have raised FrozenInstanceError"
        except Exception:
            pass

    def test_defaults_are_none(self) -> None:
        event = StreamEvent(type="unknown")
        assert event.content is None
        assert event.tool_name is None
        assert event.tool_input is None
        assert event.session_id is None
        assert event.cost is None
        assert event.tools_used == []
