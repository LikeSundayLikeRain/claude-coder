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
                            tool_file = tool_input.get("file_path") or tool_input.get(
                                "command", ""
                            )

                # Skip system-injected messages (start with <)
                if text and text.startswith("<"):
                    text = ""

                # Skip meta-only user messages
                if data.get("isMeta"):
                    continue

                # Keep entries that have text OR tool usage
                if not text and not tool_name:
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


USER_TEXT_LIMIT = 200
ASSISTANT_TEXT_LIMIT = 500
SEPARATOR = "━━━━━━━━━━━━━━━━━━━━━━"


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _format_entry(entry: TranscriptEntry) -> str:
    if entry.role == "user":
        if not entry.text:
            return ""  # skip empty user entries in display
        return f"👤 {_truncate(entry.text, USER_TEXT_LIMIT)}"
    else:
        parts: list[str] = []
        if entry.text:
            parts.append(f"🤖 {_truncate(entry.text, ASSISTANT_TEXT_LIMIT)}")
        if entry.tool_name:
            target = entry.tool_file or ""
            if target:
                tool_desc = f"🔧 {entry.tool_name} → {_truncate(target, 80)}"
            else:
                tool_desc = f"🔧 {entry.tool_name}"
            if parts:
                parts.append(tool_desc)
            else:
                parts.append(tool_desc)
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
    formatted_blocks = [b for b in (_format_entry(e) for e in entries) if b]

    # Split into messages respecting max_chars
    messages: List[str] = []
    current_blocks: List[str] = []
    current_len = 0
    header_template = (
        f"📜 Session history ({exchange_count} exchanges):\n{SEPARATOR}\n\n"
    )
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
