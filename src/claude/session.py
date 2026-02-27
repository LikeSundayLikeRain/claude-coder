"""Session resolution using CLI's history.jsonl as source of truth."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from src.claude.history import (
    HistoryEntry,
    filter_by_directory,
    read_claude_history,
)

DEFAULT_HISTORY_PATH = Path.home() / ".claude" / "history.jsonl"


class SessionResolver:
    """Resolves session IDs from Claude CLI's history.jsonl."""

    def __init__(self, history_path: Optional[Path] = None) -> None:
        self._history_path = history_path or DEFAULT_HISTORY_PATH

    def get_latest_session(self, directory: str) -> Optional[str]:
        """Return the most recent session ID for a directory, or None."""
        entries = read_claude_history(self._history_path)
        filtered = filter_by_directory(entries, Path(directory))
        if not filtered:
            return None
        return filtered[0].session_id  # already sorted newest-first

    def list_sessions(
        self,
        directory: Optional[str] = None,
        limit: int = 10,
    ) -> list[HistoryEntry]:
        """List recent sessions, optionally filtered by directory."""
        entries = read_claude_history(self._history_path)
        if directory:
            entries = filter_by_directory(entries, Path(directory))
        return entries[:limit]
