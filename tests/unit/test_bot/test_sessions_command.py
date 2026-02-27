"""Tests for /sessions command and session callbacks."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bot.orchestrator import MessageOrchestrator
from src.claude.history import HistoryEntry
from src.config import create_test_config


@pytest.fixture
def mock_settings(tmp_path: Path):
    """Create test settings with approved directory."""
    approved = tmp_path / "workspace"
    approved.mkdir()
    project_dir = approved / "test-project"
    project_dir.mkdir()

    settings = create_test_config(
        approved_directory=str(approved),
        agentic_mode=True,
    )
    return settings, project_dir


@pytest.fixture
def orchestrator(mock_settings):
    """Create MessageOrchestrator with test settings."""
    settings, _ = mock_settings
    deps = {}  # Empty deps dict for testing
    return MessageOrchestrator(settings, deps)


@pytest.fixture
def mock_update():
    """Create mock Telegram update."""
    update = MagicMock()
    update.effective_user.id = 123
    update.message.reply_text = AsyncMock()
    update.message.text = "/sessions"
    return update


@pytest.fixture
def mock_context(mock_settings):
    """Create mock context with dependencies."""
    _, project_dir = mock_settings

    context = MagicMock()
    context.effective_user.id = 123
    context.user_data = {"current_directory": project_dir}
    context.bot_data = {}
    return context


async def test_sessions_shows_picker_with_sessions(
    orchestrator, mock_update, mock_context, mock_settings
):
    """Shows session picker with 2 sessions + New Session button."""
    _, project_dir = mock_settings

    # Create mock history entries
    entry1 = HistoryEntry(
        session_id="sess-1",
        display="First session",
        timestamp=int(datetime(2026, 2, 25, 10, 0, 0, tzinfo=UTC).timestamp() * 1000),
        project=str(project_dir),
    )
    entry2 = HistoryEntry(
        session_id="sess-2",
        display="Second session with a very long display name that needs truncation",
        timestamp=int(datetime(2026, 2, 24, 10, 0, 0, tzinfo=UTC).timestamp() * 1000),
        project=str(project_dir),
    )

    with patch(
        "src.bot.orchestrator.read_claude_history", return_value=[entry1, entry2]
    ):
        with patch(
            "src.bot.orchestrator.filter_by_directory",
            return_value=[entry1, entry2],
        ):
            with patch(
                "src.bot.orchestrator.check_history_format_health", return_value=None
            ):
                await orchestrator.agentic_sessions(mock_update, mock_context)

    # Verify reply was called
    mock_update.message.reply_text.assert_called_once()
    call_kwargs = mock_update.message.reply_text.call_args.kwargs

    # Check message content
    message_text = mock_update.message.reply_text.call_args.args[0]
    assert "Sessions in" in message_text
    assert "test-project" in message_text

    # Check inline keyboard
    reply_markup = call_kwargs["reply_markup"]
    assert reply_markup is not None
    buttons = reply_markup.inline_keyboard

    # Should have 3 buttons: 2 sessions + New Session
    assert len(buttons) == 3

    # Check first session button (newest first)
    assert "02/25" in buttons[0][0].text
    assert "First session" in buttons[0][0].text
    assert buttons[0][0].callback_data == "session:sess-1"

    # Check second session button (truncated display name)
    assert "02/24" in buttons[1][0].text
    assert len(buttons[1][0].text) < 80  # Should be truncated
    assert buttons[1][0].callback_data == "session:sess-2"

    # Check New Session button
    assert "+ New Session" in buttons[2][0].text
    assert buttons[2][0].callback_data == "session:new"


async def test_sessions_empty_shows_new_only(
    orchestrator, mock_update, mock_context, mock_settings
):
    """No sessions shows just New Session button."""
    with patch("src.bot.orchestrator.read_claude_history", return_value=[]):
        with patch("src.bot.orchestrator.filter_by_directory", return_value=[]):
            with patch(
                "src.bot.orchestrator.check_history_format_health", return_value=None
            ):
                await orchestrator.agentic_sessions(mock_update, mock_context)

    mock_update.message.reply_text.assert_called_once()
    call_kwargs = mock_update.message.reply_text.call_args.kwargs

    # Check message indicates no sessions
    message_text = mock_update.message.reply_text.call_args.args[0]
    assert "No sessions found" in message_text

    # Check inline keyboard has only New Session button
    reply_markup = call_kwargs["reply_markup"]
    buttons = reply_markup.inline_keyboard
    assert len(buttons) == 1
    assert "+ New Session" in buttons[0][0].text
    assert buttons[0][0].callback_data == "session:new"


async def test_session_callback_resumes(orchestrator, mock_settings):
    """Tapping session button sets session_id in context."""
    settings, project_dir = mock_settings

    query = MagicMock()
    query.from_user.id = 123
    query.data = "session:sess-abc123"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    update = MagicMock()
    update.callback_query = query

    context = MagicMock()
    context.user_data = {}
    context.bot_data = {
        "settings": settings,
        "audit_logger": None,
    }

    await orchestrator._agentic_callback(update, context)

    # Check session_id was set
    assert context.user_data.get("claude_session_id") == "sess-abc123"

    # Check response message
    query.edit_message_text.assert_called_once()
    message = query.edit_message_text.call_args.args[0]
    assert "Resumed session" in message


async def test_session_callback_new(orchestrator, mock_settings):
    """Tapping New Session button sets force_new_session flag."""
    settings, _ = mock_settings

    query = MagicMock()
    query.from_user.id = 123
    query.data = "session:new"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    update = MagicMock()
    update.callback_query = query

    context = MagicMock()
    context.user_data = {}
    context.bot_data = {
        "settings": settings,
        "audit_logger": None,
    }

    await orchestrator._agentic_callback(update, context)

    # Check force_new_session flag was set
    assert context.user_data.get("force_new_session") is True

    # Check response message
    query.edit_message_text.assert_called_once()
    message = query.edit_message_text.call_args.args[0]
    assert "Starting new session" in message


async def test_sessions_warns_on_malformed_history(
    orchestrator, mock_update, mock_context
):
    """Sessions command shows warning if history has >50% malformed entries."""
    warning_message = "History file has 75.0% malformed entries (3/4). Consider backing up and recreating the file."

    with patch("src.bot.orchestrator.read_claude_history", return_value=[]):
        with patch("src.bot.orchestrator.filter_by_directory", return_value=[]):
            with patch(
                "src.bot.orchestrator.check_history_format_health",
                return_value=warning_message,
            ):
                await orchestrator.agentic_sessions(mock_update, mock_context)

    # Should have been called twice: once for warning, once for session list
    assert mock_update.message.reply_text.call_count == 2

    # First call should be the warning
    first_call = mock_update.message.reply_text.call_args_list[0]
    warning_text = first_call[0][0]
    assert "⚠️" in warning_text
    assert "malformed entries" in warning_text


async def test_sessions_caps_at_10_sessions(
    orchestrator, mock_update, mock_context, mock_settings
):
    """Sessions list shows max 10 sessions even if more exist."""
    _, project_dir = mock_settings

    # Create 15 mock history entries
    entries = [
        HistoryEntry(
            session_id=f"sess-{i}",
            display=f"Session {i}",
            timestamp=int(
                datetime(2026, 2, 25, 10, i, 0, tzinfo=UTC).timestamp() * 1000
            ),
            project=str(project_dir),
        )
        for i in range(15)
    ]

    with patch("src.bot.orchestrator.read_claude_history", return_value=entries):
        with patch(
            "src.bot.orchestrator.filter_by_directory", return_value=entries
        ):
            with patch(
                "src.bot.orchestrator.check_history_format_health", return_value=None
            ):
                await orchestrator.agentic_sessions(mock_update, mock_context)

    call_kwargs = mock_update.message.reply_text.call_args.kwargs
    reply_markup = call_kwargs["reply_markup"]
    buttons = reply_markup.inline_keyboard

    # Should have 11 buttons: 10 sessions + New Session
    assert len(buttons) == 11
    assert buttons[-1][0].callback_data == "session:new"


async def test_sessions_falls_back_to_first_approved_dir(
    orchestrator, mock_update, mock_settings
):
    """Sessions uses first approved directory when current_directory not set."""
    settings, project_dir = mock_settings

    context = MagicMock()
    context.user_data = {}  # No current_directory set
    context.bot_data = {}

    with patch("src.bot.orchestrator.read_claude_history", return_value=[]):
        with patch(
            "src.bot.orchestrator.filter_by_directory", return_value=[]
        ) as mock_filter:
            with patch(
                "src.bot.orchestrator.check_history_format_health", return_value=None
            ):
                await orchestrator.agentic_sessions(mock_update, context)

    # filter_by_directory should have been called with first approved directory
    mock_filter.assert_called_once()
    call_args = mock_filter.call_args[0]
    assert call_args[1] == settings.approved_directories[0]
