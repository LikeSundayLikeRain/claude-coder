"""Rich progress display for Telegram — persistent activity log messages."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

from telegram import Message

# ---------------------------------------------------------------------------
# Task 1: ActivityEntry data model
# ---------------------------------------------------------------------------


@dataclass
class ActivityEntry:
    """One line in the activity log: text, tool call, or thinking indicator."""

    kind: Literal["text", "tool", "thinking"]
    content: str = ""
    tool_name: str = ""
    tool_detail: str = ""
    tool_result: str = ""
    is_running: bool = False


# ---------------------------------------------------------------------------
# Task 2: Secret patterns, redaction, tool icons, input summarizer
# ---------------------------------------------------------------------------

_SECRET_PATTERNS: List[re.Pattern[str]] = [
    # API keys / tokens (sk-ant-..., sk-..., ghp_..., gho_..., github_pat_..., xoxb-...)
    re.compile(
        r"(sk-ant-api\d*-[A-Za-z0-9_-]{10})[A-Za-z0-9_-]*"
        r"|(sk-[A-Za-z0-9_-]{20})[A-Za-z0-9_-]*"
        r"|(ghp_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(gho_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(github_pat_[A-Za-z0-9_]{5})[A-Za-z0-9_]*"
        r"|(xoxb-[A-Za-z0-9]{5})[A-Za-z0-9-]*"
    ),
    # AWS access keys
    re.compile(r"(AKIA[0-9A-Z]{4})[0-9A-Z]{12}"),
    # Generic long hex/base64 tokens after common flags/env patterns
    re.compile(
        r"((?:--token|--secret|--password|--api-key|--apikey|--auth)"
        r"[= ]+)['\"]?[A-Za-z0-9+/_.:-]{8,}['\"]?"
    ),
    # Inline env assignments like KEY=value
    re.compile(
        r"((?:TOKEN|SECRET|PASSWORD|API_KEY|APIKEY|AUTH_TOKEN|PRIVATE_KEY"
        r"|ACCESS_KEY|CLIENT_SECRET|WEBHOOK_SECRET)"
        r"=)['\"]?[^\s'\"]{8,}['\"]?"
    ),
    # Bearer / Basic auth headers
    re.compile(r"(Bearer )[A-Za-z0-9+/_.:-]{8,}" r"|(Basic )[A-Za-z0-9+/=]{8,}"),
    # Connection strings with credentials  user:pass@host
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


# Tool name -> friendly emoji mapping for verbose output
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
}


def tool_icon(name: str) -> str:
    """Return emoji for a tool, with a default wrench."""
    return TOOL_ICONS.get(name, "\U0001f527")


def summarize_tool_input(tool_name: str, tool_input: Dict[str, Any]) -> str:
    """Return a short summary of tool input for verbose level 2."""
    if not tool_input:
        return ""
    if tool_name in ("Read", "Write", "Edit", "MultiEdit"):
        path = tool_input.get("file_path") or tool_input.get("path", "")
        if path:
            # Show just the filename, not the full path
            return str(path).rsplit("/", 1)[-1]
    if tool_name in ("Glob", "Grep"):
        pattern = tool_input.get("pattern", "")
        if pattern:
            return str(pattern)[:60]
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if cmd:
            return redact_secrets(str(cmd)[:100])[:80]
    if tool_name in ("WebFetch", "WebSearch"):
        return str(tool_input.get("url", "") or tool_input.get("query", ""))[:60]
    if tool_name == "Task":
        desc = tool_input.get("description", "")
        if desc:
            return str(desc)[:60]
    # Generic: show first key's value
    for v in tool_input.values():
        if isinstance(v, str) and v:
            return v[:60]
    return ""


# ---------------------------------------------------------------------------
# Task 4: Tool result summarizer
# ---------------------------------------------------------------------------


def summarize_tool_result(tool_name: str, raw: str) -> str:
    """Extract a brief summary from raw tool result content."""
    if not raw:
        return ""
    first_line = ""
    for line in raw.split("\n"):
        stripped = line.strip()
        if stripped:
            first_line = stripped
            break
    if not first_line:
        return ""
    if len(first_line) > 100:
        return first_line[:100] + "..."
    return first_line


# ---------------------------------------------------------------------------
# Task 3: ProgressMessageManager
# ---------------------------------------------------------------------------

_ROLLOVER_THRESHOLD = 4000
_UPDATE_INTERVAL = 2.0  # seconds between Telegram edits


class ProgressMessageManager:
    """Manages a persistent Telegram message that shows live activity.

    Maintains an activity log of entries (text, tool calls, thinking) and
    renders them into a single Telegram message. Throttles edits to avoid
    hitting Telegram rate limits. Rolls over to a new message when the
    rendered text exceeds the threshold.
    """

    def __init__(
        self,
        initial_message: Message,
        start_time: float,
    ) -> None:
        self._message: Message = initial_message
        self._start_time: float = start_time
        self._last_update: float = 0.0
        self._dot_count: int = 0
        self.activity_log: List[ActivityEntry] = []
        self.messages: List[Message] = [initial_message]

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render(self, done: bool = False) -> str:
        """Build message text from the current activity log.

        When *done* is True the header changes to "Done (Xs)" and all
        running spinners are suppressed.
        """
        elapsed = int(time.time() - self._start_time)
        if done:
            header = f"Done ({elapsed}s)"
        else:
            header = f"Working... ({elapsed}s)"

        lines: List[str] = [header, ""]

        for entry in self.activity_log:
            if entry.kind == "text":
                # Text is delivered in the final response message;
                # skip it here to avoid showing the response twice.
                continue
            elif entry.kind == "tool":
                icon = tool_icon(entry.tool_name)
                is_running = entry.is_running and not done
                spinner = " \u23f3" if is_running else ""
                detail_part = f": {entry.tool_detail}" if entry.tool_detail else ""
                lines.append(f"{icon} {entry.tool_name}{detail_part}{spinner}")
                if entry.tool_result:
                    lines.append(f"  \u21b3 {entry.tool_result}")
            elif entry.kind == "thinking":
                if done or not entry.is_running:
                    lines.append("\U0001f4ad Thinking (done)")
                else:
                    self._dot_count = (self._dot_count % 3) + 1
                    dots = "." * self._dot_count
                    lines.append(f"\U0001f4ad Thinking{dots}")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Update (throttled)
    # ------------------------------------------------------------------

    async def update(self) -> None:
        """Edit the Telegram message if the throttle interval has passed.

        Checks rollover before editing.
        """
        now = time.time()
        if now - self._last_update < self.EDIT_INTERVAL:
            return
        text = self.render()
        if len(text) >= _ROLLOVER_THRESHOLD:
            await self._rollover()
            return
        try:
            await self._message.edit_text(text)
            self._last_update = now
        except Exception:
            # Silently ignore edit errors (e.g. MessageNotModified)
            self._last_update = now

    async def _rollover(self) -> None:
        """Finalize current message and send a fresh continuation message."""
        # Finalize current message as-is
        try:
            await self._message.edit_text(self.render(done=False))
        except Exception:
            pass
        # Send a new message in the same chat
        new_message = await self._message.chat.send_message(
            text="Working... (continued)",
        )
        self._message = new_message
        self.messages.append(new_message)
        self.activity_log = []
        self._last_update = time.time()

    # ------------------------------------------------------------------
    # Finalize
    # ------------------------------------------------------------------

    async def finalize(self) -> None:
        """Update header to 'Done', remove spinners, keep message in place."""
        text = self.render(done=True)
        try:
            await self._message.edit_text(text)
        except Exception:
            pass

    # Exposed for testing
    EDIT_INTERVAL: float = _UPDATE_INTERVAL


# ---------------------------------------------------------------------------
# Task 6: Stream callback helpers and factory
# ---------------------------------------------------------------------------


def _close_running_entry(activity_log: List[ActivityEntry]) -> None:
    """Find the last is_running=True entry and mark it done."""
    for entry in reversed(activity_log):
        if entry.is_running:
            entry.is_running = False
            if entry.kind == "thinking":
                entry.content = "Thinking (done)"
            return


def _attach_result_to_last_tool(
    activity_log: List[ActivityEntry], raw_content: str
) -> None:
    """Set tool_result on the most recent tool entry."""
    for entry in reversed(activity_log):
        if entry.kind == "tool":
            entry.tool_result = summarize_tool_result(entry.tool_name, raw_content)
            return


def _extract_tool_result_text(content: Any) -> str:
    """Extract plain text from SDK tool result content blocks.

    The SDK sends tool results as a list of content blocks (e.g.
    ``[ToolResultBlock(content='...')]``).  Each block may carry a
    ``content`` attribute that is either a plain string or a nested
    list of ``TextBlock`` objects.  This helper walks the structure and
    returns the concatenated text.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content

    parts: List[str] = []
    items = content if isinstance(content, list) else [content]
    for block in items:
        # block.content can be str or list[TextBlock]
        inner = getattr(block, "content", None)
        if inner is None:
            # Some blocks expose .text directly
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
            continue
        if isinstance(inner, str):
            parts.append(inner)
        elif isinstance(inner, list):
            for sub in inner:
                text = getattr(sub, "text", None)
                if isinstance(text, str):
                    parts.append(text)
    return "\n".join(parts) if parts else ""


def build_stream_callback(
    progress_manager: ProgressMessageManager,
) -> Any:
    """Return an async callback that feeds stream events into progress_manager."""

    async def _callback(event_type: str, content: Any) -> None:
        log = progress_manager.activity_log

        if event_type not in ("tool_result", "thinking"):
            _close_running_entry(log)

        if event_type == "tool_use":
            tool_name = (
                content.get("name", "") if isinstance(content, dict) else str(content)
            )
            tool_input = content.get("input", {}) if isinstance(content, dict) else {}
            detail = summarize_tool_input(tool_name, tool_input)
            log.append(
                ActivityEntry(
                    kind="tool",
                    tool_name=tool_name,
                    tool_detail=detail,
                    is_running=True,
                )
            )

        elif event_type == "text":
            text = str(content)
            if log and log[-1].kind == "text":
                log[-1].content += text
            else:
                log.append(ActivityEntry(kind="text", content=text))

        elif event_type == "thinking":
            if not (log and log[-1].kind == "thinking" and log[-1].is_running):
                log.append(
                    ActivityEntry(kind="thinking", content="Thinking", is_running=True)
                )

        elif event_type == "tool_result":
            raw = _extract_tool_result_text(content)
            _attach_result_to_last_tool(log, raw)

        await progress_manager.update()

    return _callback
