"""Tests for the /resume agentic command."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bot.orchestrator import MessageOrchestrator
from src.claude.history import HistoryEntry, TranscriptMessage
from src.config import create_test_config


@pytest.fixture
def mock_settings(tmp_path: Path):
    """Create test settings with approved directory."""
    approved = tmp_path / "workspace"
    approved.mkdir()

    settings = create_test_config(
        approved_directory=str(approved),
        agentic_mode=True,
    )
    return settings


@pytest.fixture
def orchestrator(mock_settings):
    """Create MessageOrchestrator with test settings."""
    deps = {}
    return MessageOrchestrator(mock_settings, deps)


@pytest.fixture
def mock_update():
    """Create mock Telegram update."""
    update = MagicMock()
    update.effective_user.id = 123
    update.message.reply_text = AsyncMock()
    update.message.delete = AsyncMock()
    update.message.chat_id = 123
    update.message.text = "/resume"
    return update


@pytest.fixture
def mock_context():
    """Create mock context with dependencies."""
    context = MagicMock()
    context.user_data = {}
    context.bot_data = {}
    context.bot.send_message = AsyncMock()
    return context


class TestAgenticResume:
    async def test_missing_session_id_shows_usage(
        self,
        orchestrator: MessageOrchestrator,
        mock_update: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        """Shows usage hint when no session_id argument is provided."""
        mock_update.message.text = "/resume"
        await orchestrator.agentic_resume(mock_update, mock_context)
        mock_context.bot.send_message.assert_called_once()
        msg = mock_context.bot.send_message.call_args.kwargs["text"]
        assert "Usage" in msg

    async def test_missing_session_id_with_trailing_space(
        self,
        orchestrator: MessageOrchestrator,
        mock_update: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        """Shows usage hint when /resume is followed by only whitespace."""
        mock_update.message.text = "/resume   "
        await orchestrator.agentic_resume(mock_update, mock_context)
        mock_context.bot.send_message.assert_called_once()
        msg = mock_context.bot.send_message.call_args.kwargs["text"]
        assert "Usage" in msg

    async def test_session_not_found(
        self,
        orchestrator: MessageOrchestrator,
        mock_update: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        """Shows 'not found' message when session_id doesn't match any entry."""
        mock_update.message.text = "/resume nonexistent-id"
        with patch(
            "src.bot.orchestrator.read_claude_history", return_value=[]
        ):
            with patch(
                "src.bot.orchestrator.find_session_by_id", return_value=None
            ):
                await orchestrator.agentic_resume(mock_update, mock_context)
        msg = mock_context.bot.send_message.call_args.kwargs["text"]
        assert "not found" in msg

    async def test_session_not_found_calls_audit_logger(
        self,
        orchestrator: MessageOrchestrator,
        mock_update: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        """Audit logger is called with success=False when session not found."""
        mock_update.message.text = "/resume nonexistent-id"
        audit_logger = MagicMock()
        audit_logger.log_command = AsyncMock()
        mock_context.bot_data["audit_logger"] = audit_logger

        with patch(
            "src.bot.orchestrator.read_claude_history", return_value=[]
        ):
            with patch(
                "src.bot.orchestrator.find_session_by_id", return_value=None
            ):
                await orchestrator.agentic_resume(mock_update, mock_context)

        audit_logger.log_command.assert_called_once_with(
            user_id=123,
            command="resume",
            args=["nonexistent-id"],
            success=False,
        )

    async def test_successful_resume_sets_context(
        self,
        orchestrator: MessageOrchestrator,
        mock_update: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        """Sets claude_session_id and current_directory in user_data on success."""
        mock_update.message.text = "/resume sess-abc123"
        entry = HistoryEntry(
            session_id="sess-abc123",
            display="Fix auth bug",
            timestamp=1700000000000,
            project="/test/project",
        )
        with patch(
            "src.bot.orchestrator.read_claude_history",
            return_value=[entry],
        ):
            with patch(
                "src.bot.orchestrator.find_session_by_id",
                return_value=entry,
            ):
                with patch(
                    "src.bot.orchestrator.read_session_transcript",
                    return_value=[],
                ):
                    await orchestrator.agentic_resume(
                        mock_update, mock_context
                    )
        assert mock_context.user_data["claude_session_id"] == "sess-abc123"
        assert mock_context.user_data["current_directory"] == Path(
            "/test/project"
        )

    async def test_successful_resume_shows_transcript(
        self,
        orchestrator: MessageOrchestrator,
        mock_update: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        """Shows transcript preview with recent messages on success."""
        mock_update.message.text = "/resume sess-abc123"
        entry = HistoryEntry(
            session_id="sess-abc123",
            display="Fix auth bug",
            timestamp=1700000000000,
            project="/test/project",
        )
        transcript = [
            TranscriptMessage(role="user", text="Fix the auth middleware"),
            TranscriptMessage(
                role="assistant", text="I updated src/middleware/auth.py"
            ),
        ]
        with patch(
            "src.bot.orchestrator.read_claude_history",
            return_value=[entry],
        ):
            with patch(
                "src.bot.orchestrator.find_session_by_id",
                return_value=entry,
            ):
                with patch(
                    "src.bot.orchestrator.read_session_transcript",
                    return_value=transcript,
                ):
                    await orchestrator.agentic_resume(
                        mock_update, mock_context
                    )
        msg = mock_context.bot.send_message.call_args.kwargs["text"]
        assert "Session resumed" in msg
        assert "Fix the auth middleware" in msg
        assert "I updated src/middleware/auth.py" in msg

    async def test_successful_resume_calls_audit_logger(
        self,
        orchestrator: MessageOrchestrator,
        mock_update: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        """Audit logger is called with success=True on successful resume."""
        mock_update.message.text = "/resume sess-abc123"
        audit_logger = MagicMock()
        audit_logger.log_command = AsyncMock()
        mock_context.bot_data["audit_logger"] = audit_logger

        entry = HistoryEntry(
            session_id="sess-abc123",
            display="Fix auth bug",
            timestamp=1700000000000,
            project="/test/project",
        )
        with patch(
            "src.bot.orchestrator.read_claude_history",
            return_value=[entry],
        ):
            with patch(
                "src.bot.orchestrator.find_session_by_id",
                return_value=entry,
            ):
                with patch(
                    "src.bot.orchestrator.read_session_transcript",
                    return_value=[],
                ):
                    await orchestrator.agentic_resume(
                        mock_update, mock_context
                    )

        audit_logger.log_command.assert_called_once_with(
            user_id=123,
            command="resume",
            args=["sess-abc123"],
            success=True,
        )

    async def test_resume_with_no_message(
        self,
        orchestrator: MessageOrchestrator,
        mock_update: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        """Returns silently when update.message is None."""
        mock_update.message = None
        await orchestrator.agentic_resume(mock_update, mock_context)
        # Should return silently

    async def test_resume_with_no_message_text(
        self,
        orchestrator: MessageOrchestrator,
        mock_update: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        """Returns silently when update.message.text is None."""
        mock_update.message.text = None
        await orchestrator.agentic_resume(mock_update, mock_context)
        mock_context.bot.send_message.assert_not_called()

    async def test_transcript_read_error_handled_gracefully(
        self,
        orchestrator: MessageOrchestrator,
        mock_update: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        """Transcript read errors are caught; response still sent without crash."""
        mock_update.message.text = "/resume sess-abc123"
        entry = HistoryEntry(
            session_id="sess-abc123",
            display="Fix auth bug",
            timestamp=1700000000000,
            project="/test/project",
        )
        with patch(
            "src.bot.orchestrator.read_claude_history",
            return_value=[entry],
        ):
            with patch(
                "src.bot.orchestrator.find_session_by_id",
                return_value=entry,
            ):
                with patch(
                    "src.bot.orchestrator.read_session_transcript",
                    side_effect=OSError("disk error"),
                ):
                    await orchestrator.agentic_resume(
                        mock_update, mock_context
                    )

        # Should still send a response despite transcript error
        mock_context.bot.send_message.assert_called_once()
        msg = mock_context.bot.send_message.call_args.kwargs["text"]
        assert "Session resumed" in msg
        # Session context should still be set
        assert mock_context.user_data["claude_session_id"] == "sess-abc123"

    async def test_resume_no_audit_logger_does_not_crash(
        self,
        orchestrator: MessageOrchestrator,
        mock_update: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        """Works without audit_logger in bot_data (no KeyError)."""
        mock_update.message.text = "/resume sess-abc123"
        # Ensure no audit_logger in bot_data
        mock_context.bot_data = {}

        entry = HistoryEntry(
            session_id="sess-abc123",
            display="Fix auth bug",
            timestamp=1700000000000,
            project="/test/project",
        )
        with patch(
            "src.bot.orchestrator.read_claude_history",
            return_value=[entry],
        ):
            with patch(
                "src.bot.orchestrator.find_session_by_id",
                return_value=entry,
            ):
                with patch(
                    "src.bot.orchestrator.read_session_transcript",
                    return_value=[],
                ):
                    await orchestrator.agentic_resume(
                        mock_update, mock_context
                    )

        mock_context.bot.send_message.assert_called_once()
        msg = mock_context.bot.send_message.call_args.kwargs["text"]
        assert "Session resumed" in msg

    async def test_resume_deletes_command_message(
        self,
        orchestrator: MessageOrchestrator,
        mock_update: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        """The /resume command message is deleted for clean chat experience."""
        mock_update.message.text = "/resume sess-abc123"
        entry = HistoryEntry(
            session_id="sess-abc123",
            display="Fix auth bug",
            timestamp=1700000000000,
            project="/test/project",
        )
        with patch(
            "src.bot.orchestrator.read_claude_history",
            return_value=[entry],
        ):
            with patch(
                "src.bot.orchestrator.find_session_by_id",
                return_value=entry,
            ):
                with patch(
                    "src.bot.orchestrator.read_session_transcript",
                    return_value=[],
                ):
                    await orchestrator.agentic_resume(
                        mock_update, mock_context
                    )

        mock_update.message.delete.assert_called_once()
