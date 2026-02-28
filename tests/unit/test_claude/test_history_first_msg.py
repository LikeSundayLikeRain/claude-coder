"""Tests for read_first_message from session transcript."""

import pytest

from src.claude.history import read_first_message


class TestReadFirstMessage:
    def test_returns_first_user_message(self, tmp_path) -> None:
        projects_dir = tmp_path / "projects"
        slug_dir = projects_dir / "-test-project"
        slug_dir.mkdir(parents=True)
        transcript = slug_dir / "sess-1.jsonl"
        transcript.write_text(
            '{"type":"user","message":{"content":"Fix the login bug"}}\n'
            '{"type":"assistant","message":{"content":"I will fix it"}}\n'
            '{"type":"user","message":{"content":"Thanks"}}\n'
        )

        result = read_first_message(
            session_id="sess-1",
            project_dir="/test/project",
            projects_dir=projects_dir,
        )
        assert result == "Fix the login bug"

    def test_returns_none_for_missing_transcript(self, tmp_path) -> None:
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        result = read_first_message(
            session_id="nonexistent",
            project_dir="/test/project",
            projects_dir=projects_dir,
        )
        assert result is None

    def test_skips_system_messages(self, tmp_path) -> None:
        projects_dir = tmp_path / "projects"
        slug_dir = projects_dir / "-test-project"
        slug_dir.mkdir(parents=True)
        transcript = slug_dir / "sess-1.jsonl"
        transcript.write_text(
            '{"type":"user","message":{"content":"<system>init</system>"}}\n'
            '{"type":"user","message":{"content":"Real first message"}}\n'
        )

        result = read_first_message(
            session_id="sess-1",
            project_dir="/test/project",
            projects_dir=projects_dir,
        )
        assert result == "Real first message"

    def test_handles_content_list_format(self, tmp_path) -> None:
        projects_dir = tmp_path / "projects"
        slug_dir = projects_dir / "-test-project"
        slug_dir.mkdir(parents=True)
        transcript = slug_dir / "sess-1.jsonl"
        transcript.write_text(
            '{"type":"user","message":{"content":[{"type":"text","text":"Hello world"}]}}\n'
        )

        result = read_first_message(
            session_id="sess-1",
            project_dir="/test/project",
            projects_dir=projects_dir,
        )
        assert result == "Hello world"
