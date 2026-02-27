"""Claude Code history.jsonl reader and session transcript utilities.

Reads and parses Claude Code's session history file at ~/.claude/history.jsonl.
Also reads session transcripts from ~/.claude/projects/<slug>/<session-id>.jsonl.
"""

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import structlog

logger = structlog.get_logger()

DEFAULT_HISTORY_PATH = Path.home() / ".claude" / "history.jsonl"
DEFAULT_PROJECTS_DIR = Path.home() / ".claude" / "projects"


@dataclass(frozen=True)
class HistoryEntry:
    """A single session history entry from history.jsonl."""

    session_id: str
    display: str
    timestamp: int  # milliseconds since epoch
    project: str


def read_claude_history(
    history_path: Path = DEFAULT_HISTORY_PATH,
) -> list[HistoryEntry]:
    """Read and parse history.jsonl.

    Returns entries sorted newest first. Skips malformed lines.

    Args:
        history_path: Path to history.jsonl file

    Returns:
        List of HistoryEntry objects, sorted by timestamp descending
    """
    if not history_path.exists():
        logger.debug("History file not found", path=str(history_path))
        return []

    entries: list[HistoryEntry] = []
    malformed_count = 0

    try:
        with history_path.open("r") as f:
            for line_num, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue

                try:
                    data = json.loads(line)

                    # Validate required fields
                    required_fields = ["sessionId", "display", "timestamp", "project"]
                    missing_fields = [
                        field for field in required_fields if field not in data
                    ]

                    if missing_fields:
                        logger.warning(
                            "Skipping history entry with missing fields",
                            line_num=line_num,
                            missing_fields=missing_fields,
                        )
                        malformed_count += 1
                        continue

                    entry = HistoryEntry(
                        session_id=data["sessionId"],
                        display=data["display"],
                        timestamp=data["timestamp"],
                        project=data["project"],
                    )
                    entries.append(entry)

                except json.JSONDecodeError as e:
                    logger.warning(
                        "Skipping malformed JSON line",
                        line_num=line_num,
                        error=str(e),
                    )
                    malformed_count += 1
                except (KeyError, TypeError, ValueError) as e:
                    logger.warning(
                        "Skipping invalid history entry",
                        line_num=line_num,
                        error=str(e),
                    )
                    malformed_count += 1

    except Exception as e:
        logger.error("Error reading history file", path=str(history_path), error=str(e))
        return []

    if malformed_count > 0:
        logger.info(
            "Skipped malformed history entries",
            count=malformed_count,
            total_entries=len(entries) + malformed_count,
        )

    # Sort by timestamp descending (newest first)
    entries.sort(key=lambda e: e.timestamp, reverse=True)

    logger.debug(
        "Loaded Claude history",
        entry_count=len(entries),
        path=str(history_path),
    )

    return entries


def filter_by_directory(
    entries: list[HistoryEntry], directory: Path
) -> list[HistoryEntry]:
    """Filter entries matching a specific project directory.

    Args:
        entries: List of history entries
        directory: Directory path to filter by

    Returns:
        Filtered list of entries matching the directory
    """
    # Resolve directory path for accurate comparison
    try:
        resolved_dir = directory.resolve()
        resolved_dir_str = str(resolved_dir)
    except (OSError, RuntimeError) as e:
        logger.warning(
            "Failed to resolve directory path",
            directory=str(directory),
            error=str(e),
        )
        resolved_dir_str = str(directory)

    filtered = [
        entry
        for entry in entries
        if Path(entry.project).resolve() == resolved_dir
        or entry.project == resolved_dir_str
    ]

    logger.debug(
        "Filtered history by directory",
        directory=resolved_dir_str,
        total_entries=len(entries),
        filtered_count=len(filtered),
    )

    return filtered


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


def check_history_format_health(history_path: Path) -> Optional[str]:
    """Check if more than 50% of lines are malformed.

    Args:
        history_path: Path to history.jsonl file

    Returns:
        Warning message if >50% malformed, None otherwise
    """
    if not history_path.exists():
        logger.debug("History file not found for health check", path=str(history_path))
        return None

    total_lines = 0
    malformed_count = 0

    try:
        with history_path.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                total_lines += 1

                try:
                    data = json.loads(line)

                    # Check for required fields
                    required_fields = ["sessionId", "display", "timestamp", "project"]
                    missing_fields = [
                        field for field in required_fields if field not in data
                    ]

                    if missing_fields:
                        malformed_count += 1

                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    malformed_count += 1

    except Exception as e:
        logger.error(
            "Error checking history file health",
            path=str(history_path),
            error=str(e),
        )
        return None

    if total_lines == 0:
        return None

    malformed_percentage = (malformed_count / total_lines) * 100

    if malformed_percentage > 50:
        warning = (
            f"History file has {malformed_percentage:.1f}% malformed entries "
            f"({malformed_count}/{total_lines}). "
            f"Consider backing up and recreating the file."
        )
        logger.warning("History file health check failed", warning=warning)
        return warning

    logger.debug(
        "History file health check passed",
        total_lines=total_lines,
        malformed_count=malformed_count,
        malformed_percentage=malformed_percentage,
    )

    return None


def _project_slug(directory: str) -> str:
    """Convert a project directory path to the slug format used by Claude Code.

    The slug replaces '/' with '-', e.g. '/local/home/moxu/claude-coder'
    becomes '-local-home-moxu-claude-coder'.
    """
    return directory.replace("/", "-")


@dataclass(frozen=True)
class TranscriptMessage:
    """A user or assistant message from a session transcript."""

    role: str  # 'user' or 'assistant'
    text: str


def read_session_transcript(
    session_id: str,
    project_dir: str,
    limit: int = 3,
    projects_dir: Path = DEFAULT_PROJECTS_DIR,
) -> List[TranscriptMessage]:
    """Read recent user/assistant messages from a session transcript.

    Claude Code stores full conversation transcripts at:
    ~/.claude/projects/<project-slug>/<session-id>.jsonl

    Args:
        session_id: The session UUID
        project_dir: The project directory path
        limit: Maximum number of user/assistant message pairs to return
        projects_dir: Base directory for project transcripts

    Returns:
        List of TranscriptMessage objects, chronological (oldest first),
        limited to the most recent messages.
    """
    slug = _project_slug(project_dir)
    transcript_path = projects_dir / slug / f"{session_id}.jsonl"

    if not transcript_path.exists():
        logger.debug(
            "Session transcript not found",
            session_id=session_id,
            path=str(transcript_path),
        )
        return []

    messages: List[TranscriptMessage] = []

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

                if isinstance(content, str):
                    text = content.strip()
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block["text"].strip()
                            break

                # Skip empty and system-injected messages
                if not text or text.startswith("<"):
                    continue

                messages.append(TranscriptMessage(role=msg_type, text=text))

    except Exception as e:
        logger.warning(
            "Failed to read session transcript",
            session_id=session_id,
            path=str(transcript_path),
            error=str(e),
        )
        return []

    # Return the most recent messages (limit applies to pairs loosely)
    return messages[-(limit * 2) :]


def append_history_entry(
    session_id: str,
    display: str,
    project: str,
    history_path: Path = DEFAULT_HISTORY_PATH,
) -> None:
    """Append an entry to history.jsonl so CLI can discover bot sessions.

    Args:
        session_id: The session UUID
        display: Display text (first user message snippet)
        project: The project directory path
        history_path: Path to history.jsonl file
    """
    entry = {
        "sessionId": session_id,
        "display": display,
        "timestamp": int(time.time() * 1000),
        "project": project,
    }

    try:
        history_path.parent.mkdir(parents=True, exist_ok=True)
        with history_path.open("a") as f:
            f.write(json.dumps(entry) + "\n")

        logger.debug(
            "Appended history entry",
            session_id=session_id,
            project=project,
        )
    except Exception as e:
        logger.warning(
            "Failed to append history entry",
            path=str(history_path),
            error=str(e),
        )
