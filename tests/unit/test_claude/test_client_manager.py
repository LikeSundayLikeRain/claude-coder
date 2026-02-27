"""Tests for ClientManager: persistent per-user UserClient lifecycle management."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.claude.client_manager import ClientManager


def _make_mock_bot_session_repo() -> MagicMock:
    repo = MagicMock()
    repo.upsert = AsyncMock()
    repo.get_by_user = AsyncMock(return_value=None)
    repo.delete = AsyncMock()
    repo.cleanup_expired = AsyncMock(return_value=0)
    return repo


def _make_mock_options_builder() -> MagicMock:
    builder = MagicMock()
    builder.build = MagicMock(return_value=MagicMock())
    return builder


def _make_mock_user_client(
    directory: str = "/some/dir",
    session_id: Optional[str] = None,
    connected: bool = True,
) -> MagicMock:
    client = MagicMock()
    client.directory = directory
    client.session_id = session_id
    client.model = None
    client.betas = None
    client.is_connected = connected
    client.is_querying = False
    client.start = AsyncMock()
    client.stop = AsyncMock()
    client.submit = AsyncMock()
    client.interrupt = AsyncMock()
    return client


class TestGetOrConnect:
    """Test get_or_connect creates or reuses UserClient instances."""

    @pytest.mark.asyncio
    async def test_get_or_connect_creates_new_client(self) -> None:
        """Creates UserClient, starts it, stores in _clients dict."""
        repo = _make_mock_bot_session_repo()
        builder = _make_mock_options_builder()
        mock_client = _make_mock_user_client()

        with patch("src.claude.client_manager.UserClient", return_value=mock_client):
            manager = ClientManager(
                bot_session_repo=repo,
                options_builder=builder,
            )
            result = await manager.get_or_connect(user_id=1, directory="/some/dir")

        assert result is mock_client
        mock_client.start.assert_awaited_once()
        assert manager._clients[1] is mock_client

    @pytest.mark.asyncio
    async def test_get_or_connect_reuses_existing(self) -> None:
        """Returns existing connected client for same user+directory without reconnecting."""
        repo = _make_mock_bot_session_repo()
        builder = _make_mock_options_builder()
        mock_client = _make_mock_user_client(directory="/some/dir")
        mock_client.is_connected = True

        with patch(
            "src.claude.client_manager.UserClient", return_value=mock_client
        ) as mock_cls:
            manager = ClientManager(
                bot_session_repo=repo,
                options_builder=builder,
            )
            # First call creates
            first = await manager.get_or_connect(user_id=1, directory="/some/dir")
            # Second call reuses
            second = await manager.get_or_connect(user_id=1, directory="/some/dir")

        assert first is second
        # UserClient constructor only called once
        assert mock_cls.call_count == 1
        # start only called once
        assert mock_client.start.await_count == 1

    @pytest.mark.asyncio
    async def test_get_or_connect_reconnects_on_directory_change(self) -> None:
        """Stops old client and creates new one when directory changes."""
        repo = _make_mock_bot_session_repo()
        builder = _make_mock_options_builder()
        old_client = _make_mock_user_client(directory="/old/dir")
        new_client = _make_mock_user_client(directory="/new/dir")

        with patch(
            "src.claude.client_manager.UserClient", side_effect=[old_client, new_client]
        ):
            manager = ClientManager(
                bot_session_repo=repo,
                options_builder=builder,
            )
            await manager.get_or_connect(user_id=1, directory="/old/dir")
            result = await manager.get_or_connect(user_id=1, directory="/new/dir")

        old_client.stop.assert_awaited_once()
        assert result is new_client
        assert manager._clients[1] is new_client


class TestInterrupt:
    """Test interrupt() delegates to user's client."""

    @pytest.mark.asyncio
    async def test_interrupt(self) -> None:
        """Calls interrupt() on the user's client."""
        repo = _make_mock_bot_session_repo()
        builder = _make_mock_options_builder()
        mock_client = _make_mock_user_client()

        with patch("src.claude.client_manager.UserClient", return_value=mock_client):
            manager = ClientManager(
                bot_session_repo=repo,
                options_builder=builder,
            )
            await manager.get_or_connect(user_id=1, directory="/some/dir")
            await manager.interrupt(1)

        mock_client.interrupt.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_interrupt_noop_for_unknown_user(self) -> None:
        """Doesn't raise for unknown user."""
        repo = _make_mock_bot_session_repo()
        manager = ClientManager(bot_session_repo=repo)
        # Should not raise
        await manager.interrupt(999)


class TestDisconnect:
    """Test disconnect() and disconnect_all()."""

    @pytest.mark.asyncio
    async def test_disconnect(self) -> None:
        """Stops and removes from _clients."""
        repo = _make_mock_bot_session_repo()
        builder = _make_mock_options_builder()
        mock_client = _make_mock_user_client()

        with patch("src.claude.client_manager.UserClient", return_value=mock_client):
            manager = ClientManager(
                bot_session_repo=repo,
                options_builder=builder,
            )
            await manager.get_or_connect(user_id=1, directory="/some/dir")
            assert 1 in manager._clients

            await manager.disconnect(1)

        mock_client.stop.assert_awaited_once()
        assert 1 not in manager._clients

    @pytest.mark.asyncio
    async def test_disconnect_all(self) -> None:
        """Stops all clients."""
        repo = _make_mock_bot_session_repo()
        builder = _make_mock_options_builder()
        client_a = _make_mock_user_client(directory="/dir/a")
        client_b = _make_mock_user_client(directory="/dir/b")

        with patch(
            "src.claude.client_manager.UserClient", side_effect=[client_a, client_b]
        ):
            manager = ClientManager(
                bot_session_repo=repo,
                options_builder=builder,
            )
            await manager.get_or_connect(user_id=1, directory="/dir/a")
            await manager.get_or_connect(user_id=2, directory="/dir/b")
            await manager.disconnect_all()

        client_a.stop.assert_awaited_once()
        client_b.stop.assert_awaited_once()
        assert len(manager._clients) == 0


class TestPersistence:
    """Test session persistence to/from BotSessionRepository."""

    @pytest.mark.asyncio
    async def test_persists_session_on_connect(self) -> None:
        """Calls bot_session_repo.upsert() after connecting."""
        repo = _make_mock_bot_session_repo()
        builder = _make_mock_options_builder()
        mock_client = _make_mock_user_client(session_id="sess-abc")

        with patch("src.claude.client_manager.UserClient", return_value=mock_client):
            manager = ClientManager(
                bot_session_repo=repo,
                options_builder=builder,
            )
            await manager.get_or_connect(
                user_id=1,
                directory="/some/dir",
                session_id="sess-abc",
            )

        repo.upsert.assert_awaited_once()
        call_kwargs = repo.upsert.call_args
        assert call_kwargs.kwargs["user_id"] == 1 or call_kwargs.args[0] == 1

    @pytest.mark.asyncio
    async def test_restores_from_persisted_state(self) -> None:
        """Reads BotSessionModel from repo and uses persisted session_id."""
        from src.storage.models import BotSessionModel
        from datetime import datetime, UTC

        persisted = BotSessionModel(
            user_id=1,
            session_id="persisted-session",
            directory="/some/dir",
            model="claude-opus-4-6",
            betas=None,
            last_active=datetime.now(UTC),
        )
        repo = _make_mock_bot_session_repo()
        repo.get_by_user = AsyncMock(return_value=persisted)
        builder = _make_mock_options_builder()
        mock_client = _make_mock_user_client(session_id="persisted-session")

        with patch(
            "src.claude.client_manager.UserClient", return_value=mock_client
        ) as mock_cls:
            manager = ClientManager(
                bot_session_repo=repo,
                options_builder=builder,
            )
            result = await manager.get_or_connect(user_id=1, directory="/some/dir")

        # Should have looked up persisted state
        repo.get_by_user.assert_awaited_once_with(1)
        # UserClient created with persisted session_id
        init_kwargs = mock_cls.call_args.kwargs
        assert init_kwargs.get("session_id") == "persisted-session"


class TestSwitchSession:
    """Test switch_session() disconnects current then connects new."""

    @pytest.mark.asyncio
    async def test_switch_session(self) -> None:
        """Stops current, starts new session."""
        repo = _make_mock_bot_session_repo()
        builder = _make_mock_options_builder()
        old_client = _make_mock_user_client(directory="/dir", session_id="old-sess")
        new_client = _make_mock_user_client(directory="/dir", session_id="new-sess")

        with patch(
            "src.claude.client_manager.UserClient", side_effect=[old_client, new_client]
        ):
            manager = ClientManager(
                bot_session_repo=repo,
                options_builder=builder,
            )
            await manager.get_or_connect(user_id=1, directory="/dir")
            result = await manager.switch_session(
                user_id=1,
                session_id="new-sess",
                directory="/dir",
            )

        old_client.stop.assert_awaited_once()
        new_client.start.assert_awaited_once()
        assert result is new_client


class TestSetModel:
    """Test set_model() updates and persists."""

    @pytest.mark.asyncio
    async def test_set_model(self) -> None:
        """Updates client model and persists."""
        repo = _make_mock_bot_session_repo()
        builder = _make_mock_options_builder()
        mock_client = _make_mock_user_client(session_id="some-session")

        with patch("src.claude.client_manager.UserClient", return_value=mock_client):
            manager = ClientManager(
                bot_session_repo=repo,
                options_builder=builder,
            )
            await manager.get_or_connect(user_id=1, directory="/some/dir")
            # Reset upsert call count after initial connect
            repo.upsert.reset_mock()

            await manager.set_model(user_id=1, model="claude-opus-4-6")

        assert mock_client.model == "claude-opus-4-6"
        repo.upsert.assert_awaited_once()


class TestListSessions:
    """Test list_sessions() delegates to session_resolver."""

    def test_list_sessions(self, tmp_path: Path) -> None:
        """Delegates to session_resolver."""
        repo = _make_mock_bot_session_repo()
        history_file = tmp_path / "history.jsonl"
        history_file.write_text("")  # empty but valid

        manager = ClientManager(
            bot_session_repo=repo,
            history_path=history_file,
        )
        result = manager.list_sessions(directory="/some/dir", limit=5)
        assert isinstance(result, list)


@pytest.fixture
def mock_repo() -> MagicMock:
    return _make_mock_bot_session_repo()


@pytest.fixture
def manager(mock_repo: MagicMock) -> ClientManager:
    return ClientManager(
        bot_session_repo=mock_repo,
        options_builder=_make_mock_options_builder(),
    )


class TestUpdateSessionId:
    """Test update_session_id() updates client and persists."""

    @pytest.mark.asyncio
    async def test_update_session_id_persists(
        self, manager: ClientManager, mock_repo: MagicMock
    ) -> None:
        """update_session_id updates client and persists to repo."""
        mock_client = _make_mock_user_client()
        mock_client.session_id = "old-session"
        mock_client.directory = "/tmp/project"
        mock_client.model = "sonnet"
        manager._clients[123] = mock_client

        await manager.update_session_id(123, "new-session")

        assert mock_client.session_id == "new-session"
        mock_repo.upsert.assert_awaited_once()


class TestOnClientExit:
    """Test _on_client_exit removes client from dict."""

    def test_on_client_exit_removes_client(self) -> None:
        repo = _make_mock_bot_session_repo()
        manager = ClientManager(bot_session_repo=repo)
        mock_client = _make_mock_user_client()
        manager._clients[42] = mock_client

        manager._on_client_exit(42)

        assert 42 not in manager._clients

    def test_on_client_exit_noop_for_unknown_user(self) -> None:
        repo = _make_mock_bot_session_repo()
        manager = ClientManager(bot_session_repo=repo)
        # Should not raise
        manager._on_client_exit(999)
