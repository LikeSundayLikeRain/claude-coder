"""Test ClaudeIntegration facade — thin client over SDK + history.jsonl."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.claude.facade import ClaudeIntegration
from src.claude.sdk_integration import ClaudeResponse
from src.config.settings import Settings


def _make_response(session_id: str = "sdk-session-123") -> ClaudeResponse:
    return ClaudeResponse(
        content="ok",
        session_id=session_id,
        cost=0.01,
        duration_ms=100,
        num_turns=1,
        tools_used=[],
    )


@pytest.fixture
def config(tmp_path):
    return Settings(
        telegram_bot_token="test:token",
        telegram_bot_username="testbot",
        approved_directory=tmp_path,
        session_timeout_hours=24,
    )


@pytest.fixture
def sdk_manager():
    mgr = MagicMock()
    mgr.execute_command = AsyncMock(return_value=_make_response())
    return mgr


@pytest.fixture
def facade(config, sdk_manager):
    return ClaudeIntegration(config=config, sdk_manager=sdk_manager)


class TestRunCommand:
    """Core run_command behavior."""

    async def test_new_session_no_resume(self, facade, sdk_manager):
        """Without session_id, SDK is called without resume."""
        with patch("src.claude.facade.read_claude_history", return_value=[]):
            resp = await facade.run_command(
                prompt="hello",
                working_directory=Path("/test"),
                user_id=123,
            )

        call_kwargs = sdk_manager.execute_command.call_args
        assert call_kwargs.kwargs.get("session_id") is None
        assert call_kwargs.kwargs.get("continue_session") is False
        assert resp.session_id == "sdk-session-123"

    async def test_resume_existing_session(self, facade, sdk_manager):
        """With session_id, SDK is called with resume."""
        resp = await facade.run_command(
            prompt="hello",
            working_directory=Path("/test"),
            user_id=123,
            session_id="existing-session-abc",
        )

        call_kwargs = sdk_manager.execute_command.call_args
        assert call_kwargs.kwargs.get("session_id") == "existing-session-abc"
        assert call_kwargs.kwargs.get("continue_session") is True

    async def test_force_new_ignores_session_id(self, facade, sdk_manager):
        """force_new=True overrides any provided session_id."""
        resp = await facade.run_command(
            prompt="hello",
            working_directory=Path("/test"),
            user_id=123,
            session_id="should-be-ignored",
            force_new=True,
        )

        call_kwargs = sdk_manager.execute_command.call_args
        assert call_kwargs.kwargs.get("session_id") is None
        assert call_kwargs.kwargs.get("continue_session") is False

    async def test_resume_failure_retries_fresh(self, facade, sdk_manager):
        """If resume fails, retries as fresh session."""
        fresh_response = _make_response("fresh-session-456")
        sdk_manager.execute_command = AsyncMock(
            side_effect=[RuntimeError("session gone"), fresh_response]
        )

        resp = await facade.run_command(
            prompt="hello",
            working_directory=Path("/test"),
            user_id=123,
            session_id="dead-session",
        )

        assert resp.session_id == "fresh-session-456"
        assert sdk_manager.execute_command.call_count == 2

        # Second call should be fresh (no resume)
        second_call = sdk_manager.execute_command.call_args_list[1]
        assert second_call.kwargs.get("session_id") is None
        assert second_call.kwargs.get("continue_session") is False


class TestAutoResume:
    """Auto-resume from history.jsonl."""

    async def test_auto_resume_picks_most_recent(self, facade, sdk_manager, tmp_path):
        """Without session_id, auto-resume reads history.jsonl."""
        history_entries = [
            MagicMock(session_id="recent-session", timestamp=2000),
            MagicMock(session_id="old-session", timestamp=1000),
        ]

        with patch(
            "src.claude.facade.read_claude_history", return_value=history_entries
        ):
            with patch(
                "src.claude.facade.filter_by_directory",
                return_value=history_entries,
            ):
                resp = await facade.run_command(
                    prompt="hello",
                    working_directory=tmp_path,
                    user_id=123,
                )

        call_kwargs = sdk_manager.execute_command.call_args
        assert call_kwargs.kwargs.get("session_id") == "recent-session"
        assert call_kwargs.kwargs.get("continue_session") is True

    async def test_auto_resume_skipped_when_force_new(
        self, facade, sdk_manager, tmp_path
    ):
        """force_new=True skips auto-resume entirely."""
        with patch(
            "src.claude.facade.read_claude_history"
        ) as mock_history:
            resp = await facade.run_command(
                prompt="hello",
                working_directory=tmp_path,
                user_id=123,
                force_new=True,
            )

        mock_history.assert_not_called()

    async def test_auto_resume_no_history(self, facade, sdk_manager, tmp_path):
        """When history.jsonl is empty, starts fresh session."""
        with patch("src.claude.facade.read_claude_history", return_value=[]):
            resp = await facade.run_command(
                prompt="hello",
                working_directory=tmp_path,
                user_id=123,
            )

        call_kwargs = sdk_manager.execute_command.call_args
        assert call_kwargs.kwargs.get("session_id") is None
        assert call_kwargs.kwargs.get("continue_session") is False
