"""Processes SDK message stream into structured events for Telegram output."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import structlog

logger = structlog.get_logger()


@dataclass(frozen=True)
class StreamEvent:
    """A structured event extracted from an SDK message.

    Types: "text", "thinking", "tool_use", "tool_result", "result", "user",
    "unknown".
    """

    type: str
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
        elif class_name == "StreamEvent":
            return self._handle_partial(message)
        elif class_name == "UserMessage":
            return StreamEvent(
                type="user", content=getattr(message, "content", "")
            )
        else:
            logger.debug(
                "stream_handler.unknown_message_type", class_name=class_name
            )
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

        # Single special block: thinking or tool_use
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

    def _handle_partial(self, message: Any) -> StreamEvent:
        """Handle SDK StreamEvent (partial/incremental messages).

        These carry raw Anthropic API stream events with types like:
        - content_block_start: new block starting (text, tool_use, thinking)
        - content_block_delta: incremental content (text_delta, input_json_delta)
        - content_block_stop: block finished
        """
        event = getattr(message, "event", {})
        event_type = event.get("type", "")

        if event_type == "content_block_start":
            block = event.get("content_block", {})
            block_type = block.get("type", "")
            if block_type == "tool_use":
                return StreamEvent(
                    type="tool_use",
                    tool_name=block.get("name", ""),
                )
            elif block_type == "thinking":
                return StreamEvent(type="thinking", content="")
            return StreamEvent(type="unknown")

        elif event_type == "content_block_delta":
            delta = event.get("delta", {})
            delta_type = delta.get("type", "")
            if delta_type == "text_delta":
                return StreamEvent(
                    type="text", content=delta.get("text", "")
                )
            elif delta_type == "thinking_delta":
                return StreamEvent(
                    type="thinking", content=delta.get("thinking", "")
                )
            elif delta_type == "input_json_delta":
                # Tool input streaming — skip, we get the full input later
                return StreamEvent(type="unknown")
            return StreamEvent(type="unknown")

        # message_start, message_delta, content_block_stop, etc.
        return StreamEvent(type="unknown")
