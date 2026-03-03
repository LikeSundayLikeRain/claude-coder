"""Tests for /compact command."""

import sys
import tempfile
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import Chat, Message, Update, User
from telegram.ext import ContextTypes

# handlers.message was removed in the classic-mode cleanup.
# Stub it so the lazy import inside _execute_query doesn't fail.
if "src.bot.handlers.message" not in sys.modules:
    _stub = ModuleType("src.bot.handlers.message")
    _stub._format_error_message = lambda response, default="": default  # type: ignore[attr-defined]
    _stub._update_working_directory_from_claude_response = lambda *a, **kw: None  # type: ignore[attr-defined]
    sys.modules["src.bot.handlers.message"] = _stub

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


def _make_client_manager(*submit_results: MagicMock) -> MagicMock:
    """Build a ClientManager mock that returns submit_results in sequence."""
    client = AsyncMock()
    client.submit = AsyncMock(side_effect=list(submit_results))
    client.session_id = None
    client.is_connected = False

    cm = AsyncMock()
    cm.get_or_connect = AsyncMock(return_value=client)
    cm.get_active_client = MagicMock(return_value=None)
    cm.update_session_id = AsyncMock()
    cm._client = client  # expose for assertions
    return cm


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
    )
    deps = {
        "claude_integration": MagicMock(),
        "storage": MagicMock(),
        "security_validator": MagicMock(),
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

        await orchestrator.handle_compact(mock_update, mock_context)

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

        # Build submit results for the two _run_claude_query calls
        summary_result = MagicMock()
        summary_result.response_text = "• Decision: Use FastAPI\n• State: API endpoints working\n• Pending: Add tests"
        summary_result.session_id = "old-session-123"
        summary_result.cost = None
        summary_result.duration_ms = None
        summary_result.num_turns = 1

        ack_result = MagicMock()
        ack_result.response_text = "Got it. Continuing from where we left off."
        ack_result.session_id = "new-session-456"
        ack_result.cost = None
        ack_result.duration_ms = None
        ack_result.num_turns = 1

        cm = _make_client_manager(summary_result, ack_result)
        mock_context.bot_data["client_manager"] = cm

        await orchestrator.handle_compact(mock_update, mock_context)

        # Verify two submit calls (summary + reseed)
        assert cm._client.submit.call_count == 2

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

        await orchestrator.handle_compact(mock_update, mock_context)

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

        # Build client_manager whose submit raises on first call
        client = AsyncMock()
        client.submit = AsyncMock(side_effect=Exception("Network timeout"))
        client.session_id = None
        client.is_connected = False

        cm = AsyncMock()
        cm.get_or_connect = AsyncMock(return_value=client)
        cm.get_active_client = MagicMock(return_value=None)
        cm.update_session_id = AsyncMock()
        mock_context.bot_data["client_manager"] = cm

        await orchestrator.handle_compact(mock_update, mock_context)

        # Only one submit attempted (failed during summary)
        assert client.submit.call_count == 1

        # Error message sent
        calls = [str(call) for call in mock_update.message.reply_text.call_args_list]
        error_call = [c for c in calls if "Failed to compact" in c]
        assert len(error_call) > 0
