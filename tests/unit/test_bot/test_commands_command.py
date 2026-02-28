"""Tests for skill commands — native pass-through via SDK."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import InlineKeyboardButton, Update
from telegram.ext import ContextTypes

from src.bot.orchestrator import MessageOrchestrator
from src.config.settings import Settings


@pytest.fixture
def mock_settings():
    settings = MagicMock(spec=Settings)
    settings.approved_directories = [Path("/test/workspace")]
    settings.approved_directory = Path("/test/workspace")
    settings.agentic_mode = True
    settings.enable_project_threads = False
    return settings


@pytest.fixture
def orchestrator(mock_settings):
    deps = {}
    return MessageOrchestrator(mock_settings, deps)


@pytest.fixture
def mock_update():
    update = MagicMock(spec=Update)
    update.effective_user.id = 12345
    update.message.text = "/commands"
    update.message.reply_text = AsyncMock()
    update.message.chat.send_action = AsyncMock()
    return update


@pytest.fixture
def mock_context():
    context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
    context.user_data = {"current_directory": Path("/test/workspace")}
    context.bot_data = {}
    return context


# --- /commands tests ---

@pytest.mark.asyncio
async def test_commands_shows_cached_commands(orchestrator, mock_update, mock_context):
    """Test /commands uses cached commands from get_server_info()."""
    mock_client_manager = MagicMock()
    mock_client_manager.get_available_commands.return_value = [
        {"name": "brainstorm", "description": "Brainstorm ideas", "argumentHint": "<topic>"},
        {"name": "commit", "description": "Commit changes", "argumentHint": ""},
        {"name": "deploy", "description": "Deploy app", "argumentHint": ""},
    ]
    mock_context.bot_data = {"client_manager": mock_client_manager}

    await orchestrator.agentic_commands(mock_update, mock_context)

    mock_update.message.reply_text.assert_called_once()
    call_args = mock_update.message.reply_text.call_args
    message_text = call_args[0][0]
    assert "Available Skills" in message_text
    assert "brainstorm" in message_text
    assert "commit" in message_text
    assert "deploy" in message_text

    reply_markup = call_args[1]["reply_markup"]
    assert reply_markup is not None


@pytest.mark.asyncio
async def test_commands_no_active_session(orchestrator, mock_update, mock_context):
    """Test /commands shows message when no active session."""
    mock_client_manager = MagicMock()
    mock_client_manager.get_available_commands.return_value = []
    mock_context.bot_data = {"client_manager": mock_client_manager}

    await orchestrator.agentic_commands(mock_update, mock_context)

    mock_update.message.reply_text.assert_called_once()
    message_text = mock_update.message.reply_text.call_args[0][0]
    assert "No Skills Available" in message_text


@pytest.mark.asyncio
async def test_commands_arg_hint_uses_switch_inline(orchestrator, mock_update, mock_context):
    """Test skills with argumentHint use switch_inline_query_current_chat."""
    mock_client_manager = MagicMock()
    mock_client_manager.get_available_commands.return_value = [
        {"name": "search", "description": "Search code", "argumentHint": "<query>"},
    ]
    mock_context.bot_data = {"client_manager": mock_client_manager}

    await orchestrator.agentic_commands(mock_update, mock_context)

    reply_markup = mock_update.message.reply_text.call_args[1]["reply_markup"]
    button = reply_markup.inline_keyboard[0][0]
    assert button.switch_inline_query_current_chat == "/search "


@pytest.mark.asyncio
async def test_commands_no_arg_hint_uses_callback(orchestrator, mock_update, mock_context):
    """Test skills without argumentHint use callback_data."""
    mock_client_manager = MagicMock()
    mock_client_manager.get_available_commands.return_value = [
        {"name": "deploy", "description": "Deploy app", "argumentHint": ""},
    ]
    mock_context.bot_data = {"client_manager": mock_client_manager}

    await orchestrator.agentic_commands(mock_update, mock_context)

    reply_markup = mock_update.message.reply_text.call_args[1]["reply_markup"]
    button = reply_markup.inline_keyboard[0][0]
    assert button.callback_data == "skill:deploy"


# --- Skill text invocation tests ---

@pytest.mark.asyncio
async def test_skill_text_passthrough(orchestrator, mock_update, mock_context):
    """Test that /skillname args passes verbatim to Claude without body injection."""
    mock_update.message.text = "/deploy production"

    mock_client_manager = MagicMock()
    mock_client = MagicMock()
    mock_client.has_command.return_value = True
    mock_client.is_connected = True
    mock_client.directory = "/test/workspace"
    mock_client.session_id = "sess"
    mock_client_manager.get_active_client.return_value = mock_client

    mock_claude_response = MagicMock()
    mock_claude_response.session_id = "test-session"
    mock_claude_response.content = "Deployed to production"

    mock_context.bot_data = {"client_manager": mock_client_manager}
    mock_context.user_data = {
        "current_directory": Path("/test/workspace"),
        "claude_session_id": "old-session",
    }

    with patch("src.bot.utils.formatting.ResponseFormatter") as mock_formatter_class:
        mock_formatter = MagicMock()
        mock_formatted_msg = MagicMock()
        mock_formatted_msg.text = "Deployed to production"
        mock_formatted_msg.parse_mode = "HTML"
        mock_formatter.format_claude_response.return_value = [mock_formatted_msg]
        mock_formatter_class.return_value = mock_formatter

        with patch.object(orchestrator, "_start_typing_heartbeat") as mock_heartbeat:
            mock_heartbeat.return_value = MagicMock()
            mock_update.message.reply_text.return_value = MagicMock(
                edit_text=AsyncMock()
            )
            with patch.object(orchestrator, "_run_claude_query") as mock_query:
                mock_query.return_value = mock_claude_response
                await orchestrator.agentic_text(mock_update, mock_context)

    # Verify prompt is passed verbatim — no <skill-invocation> wrapping
    mock_query.assert_called_once()
    prompt = mock_query.call_args.kwargs.get("prompt", mock_query.call_args[0][0] if mock_query.call_args[0] else "")
    assert prompt == "/deploy production"
    assert "<skill-invocation>" not in prompt
    assert "<skill-body>" not in prompt


@pytest.mark.asyncio
async def test_unknown_skill_shows_error(orchestrator, mock_update, mock_context):
    """Test that unknown /command with active client shows error."""
    mock_update.message.text = "/nonexistent"

    mock_client_manager = MagicMock()
    mock_client = MagicMock()
    mock_client.has_command.return_value = False
    mock_client_manager.get_active_client.return_value = mock_client

    mock_context.bot_data = {"client_manager": mock_client_manager}
    mock_context.user_data = {
        "current_directory": Path("/test/workspace"),
        "claude_session_id": "sess",
    }

    await orchestrator.agentic_text(mock_update, mock_context)

    mock_update.message.reply_text.assert_called()
    message_text = mock_update.message.reply_text.call_args[0][0]
    assert "not found" in message_text.lower() or "nonexistent" in message_text


# --- Skill callback tests ---

@pytest.mark.asyncio
async def test_skill_callback_passthrough(orchestrator, mock_context):
    """Test that skill: callback passes /skill_name verbatim to Claude."""
    mock_query = MagicMock()
    mock_query.data = "skill:deploy"
    mock_query.from_user.id = 12345
    mock_query.answer = AsyncMock()
    mock_query.edit_message_text = AsyncMock()
    mock_query.message.reply_text = AsyncMock()
    mock_query.message.chat.send_action = AsyncMock()

    mock_update = MagicMock(spec=Update)
    mock_update.callback_query = mock_query

    mock_claude_response = MagicMock()
    mock_claude_response.session_id = "test-session"
    mock_claude_response.content = "Deployment complete"

    mock_context.bot_data = {}
    mock_context.user_data = {
        "current_directory": Path("/test/workspace"),
        "claude_session_id": "old-session",
    }

    with patch("src.bot.utils.formatting.ResponseFormatter") as mock_formatter_class:
        mock_formatter = MagicMock()
        mock_formatted_msg = MagicMock()
        mock_formatted_msg.text = "Deployment complete"
        mock_formatted_msg.parse_mode = "HTML"
        mock_formatter.format_claude_response.return_value = [mock_formatted_msg]
        mock_formatter_class.return_value = mock_formatter

        with patch.object(orchestrator, "_start_typing_heartbeat") as mock_heartbeat:
            mock_heartbeat.return_value = MagicMock()
            mock_query.message.reply_text.return_value = MagicMock(
                edit_text=AsyncMock()
            )
            with patch.object(orchestrator, "_run_claude_query") as mock_run:
                mock_run.return_value = mock_claude_response
                await orchestrator._agentic_callback(mock_update, mock_context)

    # Verify prompt passed verbatim as /skill_name
    mock_run.assert_called_once()
    prompt = mock_run.call_args.kwargs.get("prompt", mock_run.call_args[0][0] if mock_run.call_args[0] else "")
    assert prompt == "/deploy"
    assert "<skill-invocation>" not in prompt
