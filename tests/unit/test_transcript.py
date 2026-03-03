import json
from pathlib import Path

import pytest

from src.claude.transcript import (
    TranscriptEntry,
    format_condensed,
    read_full_transcript,
)


@pytest.fixture
def transcript_dir(tmp_path):
    """Create a mock transcript file."""
    slug_dir = tmp_path / "-tmp-myproject"
    slug_dir.mkdir()
    transcript = slug_dir / "sess-123.jsonl"
    lines = [
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "Fix the bug"}],
            },
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Found the issue in auth.py"},
                    {
                        "type": "tool_use",
                        "name": "Edit",
                        "input": {"file_path": "src/auth.py"},
                    },
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "Add a test"}],
            },
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Added test_auth.py. All passing."},
                ],
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(l) for l in lines))
    return tmp_path


def test_read_full_transcript(transcript_dir):
    entries = read_full_transcript(
        "sess-123", "/tmp/myproject", projects_dir=transcript_dir
    )
    assert len(entries) == 4
    assert entries[0].role == "user"
    assert entries[0].text == "Fix the bug"
    assert entries[0].tool_name is None
    assert entries[1].role == "assistant"
    assert entries[1].text == "Found the issue in auth.py"
    assert entries[1].tool_name == "Edit"
    assert entries[1].tool_file == "src/auth.py"


def test_read_full_transcript_missing_file(tmp_path):
    entries = read_full_transcript(
        "nonexistent", "/tmp/myproject", projects_dir=tmp_path
    )
    assert entries == []


def test_read_full_transcript_skips_thinking(transcript_dir):
    """Thinking blocks should be excluded."""
    slug_dir = transcript_dir / "-tmp-thinkproject"
    slug_dir.mkdir()
    transcript = slug_dir / "sess-456.jsonl"
    lines = [
        {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": "Hello"}]},
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "Let me think..."},
                    {"type": "text", "text": "Hi there!"},
                ],
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(l) for l in lines))
    entries = read_full_transcript(
        "sess-456", "/tmp/thinkproject", projects_dir=transcript_dir
    )
    assert len(entries) == 2
    assert entries[1].text == "Hi there!"


def test_format_condensed_basic(transcript_dir):
    entries = read_full_transcript(
        "sess-123", "/tmp/myproject", projects_dir=transcript_dir
    )
    messages = format_condensed(entries)
    assert len(messages) == 1
    assert "Fix the bug" in messages[0]
    assert "Found the issue" in messages[0]
    assert "Edit" in messages[0]  # tool usage shown as 🔧 Edit → path
    assert "Session history (2 exchanges)" in messages[0]


def test_format_condensed_empty():
    messages = format_condensed([])
    assert messages == []


def test_format_condensed_truncates_long_text():
    entries = [
        TranscriptEntry(role="user", text="x" * 300),
        TranscriptEntry(role="assistant", text="y" * 600),
    ]
    messages = format_condensed(entries)
    assert len(messages) == 1
    # User truncated at 200, assistant at 500
    assert "..." in messages[0]


def test_format_condensed_splits_at_limit():
    """Very long history should split into multiple messages."""
    entries = []
    for i in range(50):
        entries.append(TranscriptEntry(role="user", text=f"Question {i} " + "x" * 50))
        entries.append(
            TranscriptEntry(role="assistant", text=f"Answer {i} " + "y" * 100)
        )
    messages = format_condensed(entries, max_chars=4000)
    assert len(messages) > 1
    for msg in messages:
        assert len(msg) <= 4000


def test_format_condensed_last_n():
    entries = [
        TranscriptEntry(role="user", text="First"),
        TranscriptEntry(role="assistant", text="First reply"),
        TranscriptEntry(role="user", text="Second"),
        TranscriptEntry(role="assistant", text="Second reply"),
        TranscriptEntry(role="user", text="Third"),
        TranscriptEntry(role="assistant", text="Third reply"),
    ]
    messages = format_condensed(entries, last_n=2)
    assert len(messages) == 1
    assert "Second" in messages[0]
    assert "Third" in messages[0]
    assert "First" not in messages[0]
    assert "Session history (2 exchanges)" in messages[0]
