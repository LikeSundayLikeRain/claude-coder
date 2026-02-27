"""Tests for SessionResolver."""

import json
from pathlib import Path

from src.claude.session import SessionResolver


def _write_history(path: Path, entries: list[dict]) -> None:
    """Write JSONL entries to a history file."""
    with path.open("w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


class TestGetLatestSession:
    """Tests for SessionResolver.get_latest_session()."""

    def test_returns_most_recent_session_for_directory(self, tmp_path: Path) -> None:
        """Returns the newest session_id for the given directory."""
        history_file = tmp_path / "history.jsonl"
        project_dir = str(tmp_path / "myproject")

        _write_history(
            history_file,
            [
                {
                    "sessionId": "old-session",
                    "display": "Older session",
                    "timestamp": 1000000,
                    "project": project_dir,
                },
                {
                    "sessionId": "new-session",
                    "display": "Newer session",
                    "timestamp": 2000000,
                    "project": project_dir,
                },
            ],
        )

        resolver = SessionResolver(history_path=history_file)
        result = resolver.get_latest_session(project_dir)

        assert result == "new-session"

    def test_filters_by_directory(self, tmp_path: Path) -> None:
        """Does not return sessions from other directories."""
        history_file = tmp_path / "history.jsonl"
        target_dir = str(tmp_path / "target")
        other_dir = str(tmp_path / "other")

        _write_history(
            history_file,
            [
                {
                    "sessionId": "other-session",
                    "display": "Other project",
                    "timestamp": 3000000,
                    "project": other_dir,
                },
                {
                    "sessionId": "target-session",
                    "display": "Target project",
                    "timestamp": 1000000,
                    "project": target_dir,
                },
            ],
        )

        resolver = SessionResolver(history_path=history_file)
        result = resolver.get_latest_session(target_dir)

        assert result == "target-session"

    def test_returns_none_when_no_sessions_for_directory(self, tmp_path: Path) -> None:
        """Returns None when no entries match the given directory."""
        history_file = tmp_path / "history.jsonl"

        _write_history(
            history_file,
            [
                {
                    "sessionId": "some-session",
                    "display": "Some project",
                    "timestamp": 1000000,
                    "project": "/completely/different/path",
                },
            ],
        )

        resolver = SessionResolver(history_path=history_file)
        result = resolver.get_latest_session(str(tmp_path / "absent"))

        assert result is None

    def test_returns_none_when_history_file_does_not_exist(
        self, tmp_path: Path
    ) -> None:
        """Returns None when history.jsonl is missing."""
        missing_file = tmp_path / "no_history.jsonl"
        resolver = SessionResolver(history_path=missing_file)

        result = resolver.get_latest_session("/any/directory")

        assert result is None


class TestListSessions:
    """Tests for SessionResolver.list_sessions()."""

    def test_returns_entries_sorted_newest_first_with_limit(
        self, tmp_path: Path
    ) -> None:
        """Entries are newest-first and limited to the requested count."""
        history_file = tmp_path / "history.jsonl"
        project_dir = str(tmp_path / "proj")

        _write_history(
            history_file,
            [
                {
                    "sessionId": "s1",
                    "display": "First",
                    "timestamp": 1000000,
                    "project": project_dir,
                },
                {
                    "sessionId": "s2",
                    "display": "Second",
                    "timestamp": 2000000,
                    "project": project_dir,
                },
                {
                    "sessionId": "s3",
                    "display": "Third",
                    "timestamp": 3000000,
                    "project": project_dir,
                },
            ],
        )

        resolver = SessionResolver(history_path=history_file)
        result = resolver.list_sessions(directory=project_dir, limit=2)

        assert len(result) == 2
        assert result[0].session_id == "s3"
        assert result[1].session_id == "s2"

    def test_returns_all_sessions_when_directory_is_none(self, tmp_path: Path) -> None:
        """With directory=None, returns sessions from all projects."""
        history_file = tmp_path / "history.jsonl"

        _write_history(
            history_file,
            [
                {
                    "sessionId": "alpha",
                    "display": "Alpha",
                    "timestamp": 1000000,
                    "project": "/proj/alpha",
                },
                {
                    "sessionId": "beta",
                    "display": "Beta",
                    "timestamp": 2000000,
                    "project": "/proj/beta",
                },
            ],
        )

        resolver = SessionResolver(history_path=history_file)
        result = resolver.list_sessions(directory=None, limit=10)

        session_ids = [e.session_id for e in result]
        assert "alpha" in session_ids
        assert "beta" in session_ids
        assert len(result) == 2
