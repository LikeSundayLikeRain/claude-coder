"""Tests for /compact command."""

import tempfile
from pathlib import Path
import pytest
from unittest.mock import AsyncMock, MagicMock

from telegram import Update, User, Message, Chat
from telegram.ext import ContextTypes

from src.bot.orchestrator import MessageOrchestrator
from src.claude.sdk_integration import ClaudeResponse
from src.config import create_test_config


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def mock_update():
    """Create a mock Telegram update."""
    update = MagicMock(spec=Update)
    update.effective_user = MagicMock(spec=User)
    update.effective_user.id = 12345
    update.message = MagicMock(spec=Message)
    update.message.chat = MagicMock(spec=Chat)
    update.message.reply_text = AsyncMock()
    update.message.chat.send_action = AsyncMock()
    return update


@pytest.fixture
def mock_context():
    """Create a mock context."""
    context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
    context.user_data = {}
    context.bot_data = {
        "claude_integration": MagicMock(),
        "storage": MagicMock(),
    }
    return context


@pytest.fixture
def orchestrator(tmp_dir):
    """Create orchestrator with test settings."""
    settings = create_test_config(
        approved_directory=str(tmp_dir),
        agentic_mode=True,
    )
    deps = {
        "claude_integration": MagicMock(),
        "storage": MagicMock(),
        "security_validator": MagicMock(),
        "rate_limiter": MagicMock(),
        "audit_logger": MagicMock(),
    }
    return MessageOrchestrator(settings, deps)


class TestCompactCommand:
    """Tests for /compact command."""

    @pytest.mark.asyncio
    async def test_compact_no_active_session(
        self, orchestrator, mock_update, mock_context
    ):
        """Test /compact with no active session."""
        # No session_id in context
        mock_context.user_data = {}

        await orchestrator.agentic_compact(mock_update, mock_context)

        mock_update.message.reply_text.assert_called_once_with(
            "No active session to compact. Start a conversation first."
        )

    @pytest.mark.asyncio
    async def test_compact_summarizes_and_reseeds(
        self, orchestrator, mock_update, mock_context
    ):
        """Test /compact summarizes then creates new session with summary."""
        # Setup: active session
        mock_context.user_data = {
            "claude_session_id": "old-session-123",
            "current_directory": "/tmp/test",
        }

        # Mock claude_integration
        claude_integration = mock_context.bot_data["claude_integration"]

        # First call returns summary
        summary_response = ClaudeResponse(
            content="• Decision: Use FastAPI\n• State: API endpoints working\n• Pending: Add tests",
            session_id="old-session-123",
            cost=0.05,
            num_turns=1,
            duration_ms=1500,
        )

        # Second call returns acknowledgment with new session
        ack_response = ClaudeResponse(
            content="Got it. Continuing from where we left off.",
            session_id="new-session-456",
            cost=0.02,
            num_turns=1,
            duration_ms=800,
        )

        claude_integration.run_command = AsyncMock(
            side_effect=[summary_response, ack_response]
        )

        await orchestrator.agentic_compact(mock_update, mock_context)

        # Verify two run_command calls
        assert claude_integration.run_command.call_count == 2

        # First call: summarize with existing session
        first_call = claude_integration.run_command.call_args_list[0]
        assert "Summarize our conversation" in first_call[1]["prompt"]
        assert first_call[1]["session_id"] == "old-session-123"
        assert first_call[1]["force_new"] is False

        # Second call: reseed with new session
        second_call = claude_integration.run_command.call_args_list[1]
        assert "This is a compacted session" in second_call[1]["prompt"]
        assert "Decision: Use FastAPI" in second_call[1]["prompt"]
        assert second_call[1]["force_new"] is True

        # Session ID updated to new session
        assert mock_context.user_data["claude_session_id"] == "new-session-456"

        # User notified
        calls = [str(call) for call in mock_update.message.reply_text.call_args_list]
        assert any("Compacting" in str(call) for call in calls)
        assert any("compacted" in str(call) for call in calls)

    @pytest.mark.asyncio
    async def test_compact_handles_claude_error(
        self, orchestrator, mock_update, mock_context
    ):
        """Test /compact handles errors gracefully."""
        # Setup: active session
        mock_context.user_data = {
            "claude_session_id": "session-123",
            "current_directory": "/tmp/test",
        }

        # Mock claude_integration to raise error
        claude_integration = mock_context.bot_data["claude_integration"]
        claude_integration.run_command = AsyncMock(
            side_effect=Exception("Claude API error")
        )

        await orchestrator.agentic_compact(mock_update, mock_context)

        # Error message sent
        calls = [str(call) for call in mock_update.message.reply_text.call_args_list]
        error_call = [c for c in calls if "Failed to compact" in c]
        assert len(error_call) > 0

    @pytest.mark.asyncio
    async def test_compact_handles_summary_error(
        self, orchestrator, mock_update, mock_context
    ):
        """Test /compact handles error during summary step."""
        # Setup: active session
        mock_context.user_data = {
            "claude_session_id": "session-123",
            "current_directory": "/tmp/test",
        }

        # Mock claude_integration to fail on first call only
        claude_integration = mock_context.bot_data["claude_integration"]
        claude_integration.run_command = AsyncMock(
            side_effect=Exception("Network timeout")
        )

        await orchestrator.agentic_compact(mock_update, mock_context)

        # Only one call attempted (failed during summary)
        assert claude_integration.run_command.call_count == 1

        # Error message sent
        calls = [str(call) for call in mock_update.message.reply_text.call_args_list]
        error_call = [c for c in calls if "Failed to compact" in c]
        assert len(error_call) > 0
