"""Tests for /commands command."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import InlineKeyboardButton, Update
from telegram.ext import ContextTypes

from src.bot.orchestrator import MessageOrchestrator
from src.config.settings import Settings
from src.skills.loader import SkillMetadata


@pytest.fixture
def mock_settings():
    """Create mock settings."""
    settings = MagicMock(spec=Settings)
    settings.approved_directories = [Path("/test/workspace")]
    settings.agentic_mode = True
    settings.enable_project_threads = False
    settings.verbose_level = 1
    return settings


@pytest.fixture
def orchestrator(mock_settings):
    """Create orchestrator instance."""
    deps = {}
    return MessageOrchestrator(mock_settings, deps)


@pytest.fixture
def mock_update():
    """Create mock Telegram update."""
    update = MagicMock(spec=Update)
    update.effective_user.id = 12345
    update.message.text = "/commands"
    update.message.reply_text = AsyncMock()
    update.message.chat.send_action = AsyncMock()
    return update


@pytest.fixture
def mock_context():
    """Create mock context."""
    context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
    context.user_data = {"current_directory": Path("/test/workspace")}
    context.bot_data = {}
    return context


@pytest.mark.asyncio
async def test_commands_shows_project_and_personal_skills(
    orchestrator, mock_update, mock_context
):
    """Test that /commands shows both project and personal skills grouped."""
    project_skill = SkillMetadata(
        name="deploy",
        description="Deploy the app",
        argument_hint=None,
        user_invocable=True,
        source="project",
        file_path=Path("/test/.claude/skills/deploy/SKILL.md"),
    )
    personal_skill = SkillMetadata(
        name="review",
        description="Review code",
        argument_hint=None,
        user_invocable=True,
        source="personal",
        file_path=Path("/home/user/.claude/skills/review/SKILL.md"),
    )

    with patch(
        "src.bot.orchestrator.discover_skills", return_value=[project_skill, personal_skill]
    ):
        await orchestrator.agentic_commands(mock_update, mock_context)

    # Verify reply was sent
    mock_update.message.reply_text.assert_called_once()
    call_args = mock_update.message.reply_text.call_args

    # Check message text contains both sections
    message_text = call_args[0][0]
    assert "Available Skills" in message_text
    assert "Project" in message_text
    assert "Personal" in message_text
    assert "deploy" in message_text
    assert "review" in message_text

    # Check inline keyboard
    reply_markup = call_args[1]["reply_markup"]
    assert reply_markup is not None
    buttons = reply_markup.inline_keyboard
    assert len(buttons) == 2  # Two skills = two rows


@pytest.mark.asyncio
async def test_commands_no_skills_shows_message(orchestrator, mock_update, mock_context):
    """Test that /commands shows helpful message when no skills found."""
    with patch("src.bot.orchestrator.discover_skills", return_value=[]):
        await orchestrator.agentic_commands(mock_update, mock_context)

    mock_update.message.reply_text.assert_called_once()
    call_args = mock_update.message.reply_text.call_args
    message_text = call_args[0][0]

    assert "No Skills Found" in message_text
    assert ".claude/skills" in message_text
    assert "~/.claude/skills" in message_text


@pytest.mark.asyncio
async def test_commands_no_arg_skill_uses_callback(
    orchestrator, mock_update, mock_context
):
    """Test that skills without argument_hint use callback_data."""
    skill = SkillMetadata(
        name="test-skill",
        description="Test skill",
        argument_hint=None,
        user_invocable=True,
        source="project",
        file_path=Path("/test/.claude/skills/test-skill/SKILL.md"),
    )

    with patch("src.bot.orchestrator.discover_skills", return_value=[skill]):
        await orchestrator.agentic_commands(mock_update, mock_context)

    reply_markup = mock_update.message.reply_text.call_args[1]["reply_markup"]
    button = reply_markup.inline_keyboard[0][0]

    assert isinstance(button, InlineKeyboardButton)
    assert button.text == "test-skill"
    assert button.callback_data == "skill:test-skill"
    assert button.switch_inline_query_current_chat is None


@pytest.mark.asyncio
async def test_commands_arg_skill_uses_switch_inline(
    orchestrator, mock_update, mock_context
):
    """Test that skills with argument_hint use switch_inline_query_current_chat."""
    skill = SkillMetadata(
        name="search",
        description="Search code",
        argument_hint="query",
        user_invocable=True,
        source="project",
        file_path=Path("/test/.claude/skills/search/SKILL.md"),
    )

    with patch("src.bot.orchestrator.discover_skills", return_value=[skill]):
        await orchestrator.agentic_commands(mock_update, mock_context)

    reply_markup = mock_update.message.reply_text.call_args[1]["reply_markup"]
    button = reply_markup.inline_keyboard[0][0]

    assert isinstance(button, InlineKeyboardButton)
    assert button.text == "search ..."
    assert button.switch_inline_query_current_chat == "/search "
    assert button.callback_data is None


@pytest.mark.asyncio
async def test_skill_callback_executes(orchestrator, mock_context):
    """Test that tapping no-arg skill button triggers Claude execution."""
    mock_query = MagicMock()
    mock_query.data = "skill:deploy"
    mock_query.from_user.id = 12345
    mock_query.answer = AsyncMock()
    mock_query.edit_message_text = AsyncMock()
    mock_query.message.reply_text = AsyncMock()
    mock_query.message.chat.send_action = AsyncMock()

    mock_update = MagicMock(spec=Update)
    mock_update.callback_query = mock_query

    skill = SkillMetadata(
        name="deploy",
        description="Deploy app",
        argument_hint=None,
        user_invocable=True,
        source="project",
        file_path=Path("/test/.claude/skills/deploy/SKILL.md"),
    )

    mock_claude_integration = MagicMock()
    mock_claude_integration.run_command = AsyncMock()
    mock_claude_response = MagicMock()
    mock_claude_response.session_id = "test-session"
    mock_claude_response.content = "Deployment complete"
    mock_claude_integration.run_command.return_value = mock_claude_response

    mock_context.bot_data = {"claude_integration": mock_claude_integration}
    mock_context.user_data = {
        "current_directory": Path("/test/workspace"),
        "claude_session_id": "old-session",
    }

    with patch("src.bot.orchestrator.discover_skills", return_value=[skill]):
        with patch("src.bot.orchestrator.load_skill_body", return_value="Deploy the app"):
            with patch(
                "src.bot.orchestrator.resolve_skill_prompt",
                return_value="Deploy the app",
            ):
                with patch(
                    "src.bot.utils.formatting.ResponseFormatter"
                ) as mock_formatter_class:
                    mock_formatter = MagicMock()
                    mock_formatted_msg = MagicMock()
                    mock_formatted_msg.text = "Deployment complete"
                    mock_formatted_msg.parse_mode = "HTML"
                    mock_formatter.format_claude_response.return_value = [
                        mock_formatted_msg
                    ]
                    mock_formatter_class.return_value = mock_formatter

                    # Also mock _make_stream_callback, _start_typing_heartbeat, and progress_msg
                    with patch.object(orchestrator, "_make_stream_callback", return_value=AsyncMock()):
                        with patch.object(orchestrator, "_start_typing_heartbeat") as mock_heartbeat:
                            mock_heartbeat.return_value = MagicMock()
                            with patch.object(orchestrator, "_get_verbose_level", return_value=1):
                                mock_query.message.reply_text.return_value = MagicMock(delete=AsyncMock())
                                await orchestrator._agentic_callback(mock_update, mock_context)

    # Verify Claude was called
    mock_claude_integration.run_command.assert_called_once()
    call_kwargs = mock_claude_integration.run_command.call_args[1]
    assert call_kwargs["prompt"] == "Deploy the app"


@pytest.mark.asyncio
async def test_skill_text_invocation(orchestrator, mock_update, mock_context):
    """Test that sending /skillname args resolves and sends to Claude."""
    mock_update.message.text = "/deploy production"

    skill = SkillMetadata(
        name="deploy",
        description="Deploy app",
        argument_hint="environment",
        user_invocable=True,
        source="project",
        file_path=Path("/test/.claude/skills/deploy/SKILL.md"),
    )

    mock_claude_integration = MagicMock()
    mock_claude_integration.run_command = AsyncMock()
    mock_claude_response = MagicMock()
    mock_claude_response.session_id = "test-session"
    mock_claude_response.content = "Deployed to production"
    mock_claude_integration.run_command.return_value = mock_claude_response

    mock_context.bot_data = {"claude_integration": mock_claude_integration}
    mock_context.user_data = {
        "current_directory": Path("/test/workspace"),
        "claude_session_id": "old-session",
    }

    with patch("src.bot.orchestrator.discover_skills", return_value=[skill]):
        with patch(
            "src.bot.orchestrator.load_skill_body",
            return_value="Deploy to $ARGUMENTS environment",
        ):
            with patch(
                "src.bot.orchestrator.resolve_skill_prompt",
                return_value="Deploy to production environment",
            ) as mock_resolve:
                with patch(
                    "src.bot.utils.formatting.ResponseFormatter"
                ) as mock_formatter_class:
                    mock_formatter = MagicMock()
                    mock_formatted_msg = MagicMock()
                    mock_formatted_msg.text = "Deployed to production"
                    mock_formatted_msg.parse_mode = "HTML"
                    mock_formatter.format_claude_response.return_value = [
                        mock_formatted_msg
                    ]
                    mock_formatter_class.return_value = mock_formatter

                    # Mock stream callback, typing heartbeat, and progress msg
                    with patch.object(orchestrator, "_make_stream_callback", return_value=AsyncMock()):
                        with patch.object(orchestrator, "_start_typing_heartbeat") as mock_heartbeat:
                            mock_heartbeat.return_value = MagicMock()
                            with patch.object(orchestrator, "_get_verbose_level", return_value=1):
                                mock_update.message.reply_text.return_value = MagicMock(delete=AsyncMock())
                                await orchestrator.agentic_text(mock_update, mock_context)

    # Verify resolve was called with the arguments
    mock_resolve.assert_called_once()
    args = mock_resolve.call_args[0]
    assert args[1] == "production"  # arguments

    # Verify Claude was called with skill-framed prompt
    mock_claude_integration.run_command.assert_called_once()
    call_kwargs = mock_claude_integration.run_command.call_args[1]
    assert "<skill-invocation>" in call_kwargs["prompt"]
    assert "/deploy skill" in call_kwargs["prompt"]
    assert "Deploy to production environment" in call_kwargs["prompt"]
    assert "<skill-body>" in call_kwargs["prompt"]
