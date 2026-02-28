"""Integration tests for repo directory browser callbacks."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.orchestrator import MessageOrchestrator
from src.config import create_test_config


@pytest.fixture
def workspace(tmp_path):
    """Workspace with branch and leaf dirs."""
    (tmp_path / "projectA" / "src").mkdir(parents=True)
    (tmp_path / "projectA" / ".git").mkdir()
    (tmp_path / "projectB").mkdir()
    (tmp_path / "projectB" / ".git").mkdir()
    return tmp_path


@pytest.fixture
def settings(workspace):
    return create_test_config(
        approved_directory=str(workspace), agentic_mode=True
    )


@pytest.fixture
def orchestrator(settings):
    deps = {
        "storage": MagicMock(),
        "audit_logger": MagicMock(),
        "client_manager": MagicMock(),
    }
    return MessageOrchestrator(settings, deps)


def _make_context(user_data=None, bot_data=None):
    ctx = MagicMock()
    ctx.user_data = user_data or {}
    ctx.bot_data = bot_data or {}
    return ctx


async def test_old_cd_callback_still_switches_directory(orchestrator, workspace):
    """Old cd:{abs_path} callbacks from existing messages should still work."""
    query = MagicMock()
    query.answer = AsyncMock()
    query.data = f"cd:{workspace / 'projectB'}"
    query.from_user.id = 123
    query.edit_message_text = AsyncMock()

    update = MagicMock()
    update.callback_query = query

    storage_mock = MagicMock()
    storage_mock.save_user_directory = AsyncMock()
    client_mgr = MagicMock()
    client_mgr.disconnect = AsyncMock()

    ctx = _make_context(
        user_data={},
        bot_data={
            "storage": storage_mock,
            "client_manager": client_mgr,
            "audit_logger": MagicMock(log_command=AsyncMock()),
        },
    )

    await orchestrator._agentic_callback(update, ctx)

    assert ctx.user_data["current_directory"] == workspace / "projectB"
    # Session should be cleared (no auto-resume)
    assert ctx.user_data["claude_session_id"] is None
    # Should disconnect active client
    client_mgr.disconnect.assert_called_once_with(123)
    query.edit_message_text.assert_called_once()
    text = query.edit_message_text.call_args[0][0]
    assert "Switched to" in text
    assert "session resumed" not in text


async def test_old_cd_callback_not_found(orchestrator, workspace):
    """Old cd: callback with nonexistent path shows error."""
    query = MagicMock()
    query.answer = AsyncMock()
    query.data = f"cd:{workspace / 'nonexistent'}"
    query.from_user.id = 123
    query.edit_message_text = AsyncMock()

    update = MagicMock()
    update.callback_query = query

    ctx = _make_context(
        user_data={},
        bot_data={"storage": MagicMock()},
    )

    await orchestrator._agentic_callback(update, ctx)

    query.edit_message_text.assert_called_once()
    text = query.edit_message_text.call_args[0][0]
    assert "not found" in text.lower()


async def test_old_cd_callback_outside_roots_rejected(orchestrator, workspace):
    """Old cd: callback with path outside approved directories is rejected."""
    query = MagicMock()
    query.answer = AsyncMock()
    query.data = "cd:/tmp/evil"
    query.from_user.id = 123
    query.edit_message_text = AsyncMock()

    update = MagicMock()
    update.callback_query = query

    ctx = _make_context(
        user_data={},
        bot_data={"storage": MagicMock()},
    )

    await orchestrator._agentic_callback(update, ctx)

    query.edit_message_text.assert_called_once()
    text = query.edit_message_text.call_args[0][0]
    assert "not found" in text.lower()
