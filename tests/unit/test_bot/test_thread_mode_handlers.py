"""Tests for thread mode handler constraints."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.handlers import callback, command
from src.config import create_test_config


@pytest.fixture
def thread_settings(tmp_path: Path):
    approved = tmp_path / "projects"
    approved.mkdir()
    project_root = approved / "project_a"
    project_root.mkdir()

    config_file = tmp_path / "projects.yaml"
    config_file.write_text(
        "projects:\n"
        "  - slug: project_a\n"
        "    name: Project A\n"
        "    path: project_a\n",
        encoding="utf-8",
    )

    settings = create_test_config(
        approved_directory=str(approved),
    )
    return settings, project_root


async def test_command_cd_stays_within_project_root(thread_settings):
    """/cd .. at project root remains pinned to project root in thread mode."""
    settings, project_root = thread_settings

    update = MagicMock()
    update.effective_user.id = 1
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.args = [".."]
    context.bot_data = {
        "settings": settings,
        "security_validator": None,
        "audit_logger": None,
        "claude_integration": AsyncMock(
            _find_resumable_session=AsyncMock(return_value=None)
        ),
    }
    context.user_data = {
        "current_directory": project_root,
        "_thread_context": {"project_root": str(project_root)},
    }

    await command.change_directory(update, context)

    assert context.user_data["current_directory"] == project_root


async def test_callback_cd_stays_within_project_root(thread_settings):
    """cd callback keeps navigation constrained to thread project root."""
    settings, project_root = thread_settings

    query = MagicMock()
    query.from_user.id = 1
    query.edit_message_text = AsyncMock()

    context = MagicMock()
    context.bot_data = {
        "settings": settings,
        "security_validator": None,
        "audit_logger": None,
        "claude_integration": AsyncMock(
            _find_resumable_session=AsyncMock(return_value=None)
        ),
    }
    context.user_data = {
        "current_directory": project_root,
        "_thread_context": {"project_root": str(project_root)},
    }

    await callback.handle_cd_callback(query, "..", context)

    assert context.user_data["current_directory"] == project_root
    query.edit_message_text.assert_called_once()


async def test_start_sends_welcome_message(thread_settings):
    """/start sends a welcome message with inline keyboard."""
    settings, _ = thread_settings

    update = MagicMock()
    update.effective_user.id = 1
    update.effective_user.first_name = "User"
    update.effective_chat.type = "private"
    update.effective_chat.id = 42
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.bot = AsyncMock()
    context.bot_data = {
        "settings": settings,
        "audit_logger": None,
    }
    context.user_data = {}

    await command.start_command(update, context)

    update.message.reply_text.assert_called_once()
    text = update.message.reply_text.call_args[0][0]
    assert "Welcome" in text


async def test_sync_threads_shows_deprecation_message(thread_settings):
    """sync_threads sends a deprecation message pointing to /add."""
    settings, _ = thread_settings

    update = MagicMock()
    update.effective_user.id = 1
    update.effective_chat.type = "supergroup"
    update.effective_chat.id = -1001234567890
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.bot = AsyncMock()
    context.bot_data = {
        "settings": settings,
        "audit_logger": None,
    }
    context.user_data = {}

    await command.sync_threads(update, context)

    # Should send deprecation message directly via reply_text
    update.message.reply_text.assert_called_once()
    text = update.message.reply_text.call_args[0][0]
    assert "/add" in text


async def test_sync_threads_always_sends_deprecation(tmp_path: Path):
    """sync_threads always sends the deprecation message regardless of chat type."""
    approved = tmp_path / "projects"
    approved.mkdir()
    project_root = approved / "project_a"
    project_root.mkdir()

    settings = create_test_config(
        approved_directory=str(approved),
    )

    update = MagicMock()
    update.effective_user.id = 1
    update.effective_chat.id = -10099999
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.bot = AsyncMock()
    context.bot_data = {
        "settings": settings,
        "audit_logger": None,
    }
    context.user_data = {}

    await command.sync_threads(update, context)

    update.message.reply_text.assert_called_once()
    text = update.message.reply_text.call_args[0][0]
    assert "/add" in text
