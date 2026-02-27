"""Tests for Claude Code history.jsonl reader."""

import json
from pathlib import Path

from src.claude.history import (
    HistoryEntry,
    append_history_entry,
    check_history_format_health,
    filter_by_directory,
    read_claude_history,
    read_session_transcript,
)


class TestReadClaudeHistory:
    """Tests for reading Claude history.jsonl."""

    def test_reads_valid_entries(self, tmp_path: Path) -> None:
        """Parses well-formed lines and returns HistoryEntry objects."""
        history_file = tmp_path / "history.jsonl"
        entries_data = [
            {
                "display": "Session 1",
                "timestamp": 1740000000000,
                "project": "/path/to/project1",
                "sessionId": "session-id-1",
            },
            {
                "display": "Session 2",
                "timestamp": 1740000001000,
                "project": "/path/to/project2",
                "sessionId": "session-id-2",
            },
        ]

        with history_file.open("w") as f:
            for entry in entries_data:
                f.write(json.dumps(entry) + "\n")

        result = read_claude_history(history_file)

        assert len(result) == 2
        assert result[0].session_id == "session-id-2"
        assert result[0].display == "Session 2"
        assert result[0].timestamp == 1740000001000
        assert result[0].project == "/path/to/project2"
        assert result[1].session_id == "session-id-1"

    def test_skips_malformed_lines(self, tmp_path: Path) -> None:
        """Bad JSON and missing required fields are skipped."""
        history_file = tmp_path / "history.jsonl"
        with history_file.open("w") as f:
            # Valid entry
            f.write(
                json.dumps(
                    {
                        "display": "Valid",
                        "timestamp": 1740000000000,
                        "project": "/valid",
                        "sessionId": "valid-id",
                    }
                )
                + "\n"
            )
            # Bad JSON
            f.write("{ invalid json\n")
            # Missing sessionId
            f.write(
                json.dumps(
                    {
                        "display": "Missing session",
                        "timestamp": 1740000001000,
                        "project": "/missing",
                    }
                )
                + "\n"
            )
            # Missing project
            f.write(
                json.dumps(
                    {
                        "display": "Missing project",
                        "timestamp": 1740000002000,
                        "sessionId": "missing-project-id",
                    }
                )
                + "\n"
            )
            # Another valid entry
            f.write(
                json.dumps(
                    {
                        "display": "Valid 2",
                        "timestamp": 1740000003000,
                        "project": "/valid2",
                        "sessionId": "valid-id-2",
                    }
                )
                + "\n"
            )

        result = read_claude_history(history_file)

        assert len(result) == 2
        assert result[0].session_id == "valid-id-2"
        assert result[1].session_id == "valid-id"

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        """Nonexistent path returns empty list."""
        nonexistent = tmp_path / "does_not_exist.jsonl"
        result = read_claude_history(nonexistent)
        assert result == []

    def test_entries_sorted_by_timestamp_descending(self, tmp_path: Path) -> None:
        """Newest entries first."""
        history_file = tmp_path / "history.jsonl"
        entries_data = [
            {
                "display": "Old",
                "timestamp": 1000,
                "project": "/old",
                "sessionId": "old-id",
            },
            {
                "display": "Newest",
                "timestamp": 3000,
                "project": "/newest",
                "sessionId": "newest-id",
            },
            {
                "display": "Middle",
                "timestamp": 2000,
                "project": "/middle",
                "sessionId": "middle-id",
            },
        ]

        with history_file.open("w") as f:
            for entry in entries_data:
                f.write(json.dumps(entry) + "\n")

        result = read_claude_history(history_file)

        assert len(result) == 3
        assert result[0].session_id == "newest-id"
        assert result[1].session_id == "middle-id"
        assert result[2].session_id == "old-id"


class TestFilterByDirectory:
    """Tests for filtering entries by directory."""

    def test_filter_by_directory(self, tmp_path: Path) -> None:
        """Filters entries to matching project path only."""
        target_dir = tmp_path / "target"
        target_dir.mkdir()

        entries = [
            HistoryEntry(
                session_id="id-1",
                display="Target 1",
                timestamp=1000,
                project=str(target_dir),
            ),
            HistoryEntry(
                session_id="id-2",
                display="Other",
                timestamp=2000,
                project="/other/path",
            ),
            HistoryEntry(
                session_id="id-3",
                display="Target 2",
                timestamp=3000,
                project=str(target_dir),
            ),
        ]

        result = filter_by_directory(entries, target_dir)

        assert len(result) == 2
        assert result[0].session_id == "id-1"
        assert result[1].session_id == "id-3"

    def test_filter_no_matches(self, tmp_path: Path) -> None:
        """Returns empty list if no entries match."""
        target_dir = tmp_path / "target"
        target_dir.mkdir()

        entries = [
            HistoryEntry(
                session_id="id-1",
                display="Other 1",
                timestamp=1000,
                project="/other/path1",
            ),
            HistoryEntry(
                session_id="id-2",
                display="Other 2",
                timestamp=2000,
                project="/other/path2",
            ),
        ]

        result = filter_by_directory(entries, target_dir)
        assert result == []


class TestCheckHistoryFormatHealth:
    """Tests for checking history file format health."""

    def test_healthy_file_returns_none(self, tmp_path: Path) -> None:
        """All lines valid returns None."""
        history_file = tmp_path / "history.jsonl"
        entries_data = [
            {
                "display": "Session 1",
                "timestamp": 1740000000000,
                "project": "/path1",
                "sessionId": "id-1",
            },
            {
                "display": "Session 2",
                "timestamp": 1740000001000,
                "project": "/path2",
                "sessionId": "id-2",
            },
        ]

        with history_file.open("w") as f:
            for entry in entries_data:
                f.write(json.dumps(entry) + "\n")

        result = check_history_format_health(history_file)
        assert result is None

    def test_majority_malformed_returns_warning(self, tmp_path: Path) -> None:
        """More than 50% bad lines returns warning string."""
        history_file = tmp_path / "history.jsonl"
        with history_file.open("w") as f:
            # 1 valid entry
            f.write(
                json.dumps(
                    {
                        "display": "Valid",
                        "timestamp": 1740000000000,
                        "project": "/valid",
                        "sessionId": "valid-id",
                    }
                )
                + "\n"
            )
            # 3 bad entries (75% malformed)
            f.write("{ bad json 1\n")
            f.write("{ bad json 2\n")
            f.write(json.dumps({"display": "Missing fields"}) + "\n")

        result = check_history_format_health(history_file)
        assert result is not None
        assert "75.0%" in result
        assert "malformed" in result.lower()

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        """Nonexistent path returns None."""
        nonexistent = tmp_path / "does_not_exist.jsonl"
        result = check_history_format_health(nonexistent)
        assert result is None


class TestReadSessionTranscript:
    """Tests for reading session transcript JSONL files."""

    def test_reads_user_and_assistant_messages(self, tmp_path: Path) -> None:
        """Extracts user and assistant text messages from transcript."""
        projects_dir = tmp_path / "projects"
        slug_dir = projects_dir / "-test-project"
        slug_dir.mkdir(parents=True)

        transcript = slug_dir / "session-123.jsonl"
        lines = [
            json.dumps({"type": "user", "message": {"role": "user", "content": "Hello"}}),
            json.dumps({
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Hi there!"}],
                },
            }),
            json.dumps({"type": "user", "message": {"role": "user", "content": "How are you?"}}),
            json.dumps({
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "I'm doing well!"}],
                },
            }),
        ]
        transcript.write_text("\n".join(lines) + "\n")

        msgs = read_session_transcript(
            session_id="session-123",
            project_dir="/test/project",
            limit=3,
            projects_dir=projects_dir,
        )

        assert len(msgs) == 4
        assert msgs[0].role == "user"
        assert msgs[0].text == "Hello"
        assert msgs[1].role == "assistant"
        assert msgs[1].text == "Hi there!"

    def test_skips_system_messages(self, tmp_path: Path) -> None:
        """Messages starting with < are filtered out."""
        projects_dir = tmp_path / "projects"
        slug_dir = projects_dir / "-test-project"
        slug_dir.mkdir(parents=True)

        transcript = slug_dir / "session-456.jsonl"
        lines = [
            json.dumps({"type": "user", "message": {"role": "user", "content": "<system>ignore</system>"}}),
            json.dumps({"type": "user", "message": {"role": "user", "content": "Real message"}}),
            json.dumps({"type": "progress", "data": "something"}),
        ]
        transcript.write_text("\n".join(lines) + "\n")

        msgs = read_session_transcript(
            session_id="session-456",
            project_dir="/test/project",
            limit=5,
            projects_dir=projects_dir,
        )

        assert len(msgs) == 1
        assert msgs[0].text == "Real message"

    def test_limits_recent_messages(self, tmp_path: Path) -> None:
        """Only the most recent messages are returned based on limit."""
        projects_dir = tmp_path / "projects"
        slug_dir = projects_dir / "-test-project"
        slug_dir.mkdir(parents=True)

        transcript = slug_dir / "session-789.jsonl"
        lines = []
        for i in range(10):
            lines.append(json.dumps({
                "type": "user",
                "message": {"role": "user", "content": f"Message {i}"},
            }))
        transcript.write_text("\n".join(lines) + "\n")

        msgs = read_session_transcript(
            session_id="session-789",
            project_dir="/test/project",
            limit=2,
            projects_dir=projects_dir,
        )

        # limit=2 → last 4 messages (2*2)
        assert len(msgs) == 4
        assert msgs[0].text == "Message 6"
        assert msgs[-1].text == "Message 9"

    def test_missing_transcript_returns_empty(self, tmp_path: Path) -> None:
        """Non-existent session transcript returns empty list."""
        msgs = read_session_transcript(
            session_id="nonexistent",
            project_dir="/test/project",
            projects_dir=tmp_path,
        )
        assert msgs == []


class TestAppendHistoryEntry:
    """Tests for appending to history.jsonl."""

    def test_appends_entry(self, tmp_path: Path) -> None:
        """Appends a valid JSONL entry to history file."""
        history_path = tmp_path / "history.jsonl"

        append_history_entry(
            session_id="sess-abc",
            display="Test message",
            project="/test/project",
            history_path=history_path,
        )

        assert history_path.exists()
        lines = history_path.read_text().strip().split("\n")
        assert len(lines) == 1

        entry = json.loads(lines[0])
        assert entry["sessionId"] == "sess-abc"
        assert entry["display"] == "Test message"
        assert entry["project"] == "/test/project"
        assert "timestamp" in entry

    def test_appends_multiple_entries(self, tmp_path: Path) -> None:
        """Multiple appends create multiple lines."""
        history_path = tmp_path / "history.jsonl"

        append_history_entry("s1", "First", "/proj", history_path)
        append_history_entry("s2", "Second", "/proj", history_path)

        lines = history_path.read_text().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["sessionId"] == "s1"
        assert json.loads(lines[1])["sessionId"] == "s2"

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        """Creates parent directories if they don't exist."""
        history_path = tmp_path / "nested" / "dir" / "history.jsonl"

        append_history_entry("s1", "Test", "/proj", history_path)

        assert history_path.exists()
