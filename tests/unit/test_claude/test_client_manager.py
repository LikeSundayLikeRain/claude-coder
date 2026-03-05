"""Tests for ClientManager: persistent per-(user_id, chat_id, message_thread_id) UserClient lifecycle management."""

from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.claude.client_manager import ClientManager


def _make_mock_chat_session_repo() -> MagicMock:
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    repo.upsert = AsyncMock()
    repo.delete = AsyncMock()
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


# Default chat/thread ids for tests
_UID = 1
_CID = 100
_TID = 0


class TestGetOrConnect:
    """Test get_or_connect creates or reuses UserClient instances."""

    @pytest.mark.asyncio
    async def test_get_or_connect_creates_new_client(self) -> None:
        """Creates UserClient, starts it, stores in _clients dict."""
        repo = _make_mock_chat_session_repo()
        builder = _make_mock_options_builder()
        mock_client = _make_mock_user_client()

        with patch("src.claude.client_manager.UserClient", return_value=mock_client):
            manager = ClientManager(
                chat_session_repo=repo,
                options_builder=builder,
            )
            result = await manager.get_or_connect(
                user_id=_UID,
                chat_id=_CID,
                message_thread_id=_TID,
                directory="/some/dir",
            )

        assert result is mock_client
        mock_client.start.assert_awaited_once()
        assert manager._clients[(_UID, _CID, _TID)] is mock_client

    @pytest.mark.asyncio
    async def test_get_or_connect_reuses_existing(self) -> None:
        """Returns existing connected client for same triple without reconnecting."""
        repo = _make_mock_chat_session_repo()
        builder = _make_mock_options_builder()
        mock_client = _make_mock_user_client(directory="/some/dir")
        mock_client.is_connected = True

        with patch(
            "src.claude.client_manager.UserClient", return_value=mock_client
        ) as mock_cls:
            manager = ClientManager(
                chat_session_repo=repo,
                options_builder=builder,
            )
            first = await manager.get_or_connect(
                user_id=_UID,
                chat_id=_CID,
                message_thread_id=_TID,
                directory="/some/dir",
            )
            second = await manager.get_or_connect(
                user_id=_UID,
                chat_id=_CID,
                message_thread_id=_TID,
                directory="/some/dir",
            )

        assert first is second
        assert mock_cls.call_count == 1
        assert mock_client.start.await_count == 1

    @pytest.mark.asyncio
    async def test_two_clients_same_user_different_threads(self) -> None:
        """Two clients for the same user in different threads can coexist."""
        repo = _make_mock_chat_session_repo()
        builder = _make_mock_options_builder()
        client_a = _make_mock_user_client(directory="/proj/a")
        client_b = _make_mock_user_client(directory="/proj/b")

        with patch(
            "src.claude.client_manager.UserClient", side_effect=[client_a, client_b]
        ):
            manager = ClientManager(
                chat_session_repo=repo,
                options_builder=builder,
            )
            result_a = await manager.get_or_connect(
                user_id=_UID, chat_id=_CID, message_thread_id=1, directory="/proj/a"
            )
            result_b = await manager.get_or_connect(
                user_id=_UID, chat_id=_CID, message_thread_id=2, directory="/proj/b"
            )

        assert result_a is client_a
        assert result_b is client_b
        assert manager._clients[(_UID, _CID, 1)] is client_a
        assert manager._clients[(_UID, _CID, 2)] is client_b
        client_a.stop.assert_not_awaited()
        client_b.stop.assert_not_awaited()


class TestInterrupt:
    """Test interrupt() delegates to user's client."""

    @pytest.mark.asyncio
    async def test_interrupt(self) -> None:
        """Calls interrupt() on the user's client."""
        repo = _make_mock_chat_session_repo()
        builder = _make_mock_options_builder()
        mock_client = _make_mock_user_client()

        with patch("src.claude.client_manager.UserClient", return_value=mock_client):
            manager = ClientManager(
                chat_session_repo=repo,
                options_builder=builder,
            )
            await manager.get_or_connect(
                user_id=_UID,
                chat_id=_CID,
                message_thread_id=_TID,
                directory="/some/dir",
            )
            await manager.interrupt(_UID, _CID, _TID)

        mock_client.interrupt.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_interrupt_noop_for_unknown_user(self) -> None:
        """Doesn't raise for unknown triple."""
        repo = _make_mock_chat_session_repo()
        manager = ClientManager(chat_session_repo=repo)
        await manager.interrupt(999, 999, 0)


class TestDisconnect:
    """Test disconnect() and disconnect_all()."""

    @pytest.mark.asyncio
    async def test_disconnect(self) -> None:
        """Stops and removes from _clients."""
        repo = _make_mock_chat_session_repo()
        builder = _make_mock_options_builder()
        mock_client = _make_mock_user_client()

        with patch("src.claude.client_manager.UserClient", return_value=mock_client):
            manager = ClientManager(
                chat_session_repo=repo,
                options_builder=builder,
            )
            await manager.get_or_connect(
                user_id=_UID,
                chat_id=_CID,
                message_thread_id=_TID,
                directory="/some/dir",
            )
            assert (_UID, _CID, _TID) in manager._clients

            await manager.disconnect(_UID, _CID, _TID)

        mock_client.stop.assert_awaited_once()
        assert (_UID, _CID, _TID) not in manager._clients

    @pytest.mark.asyncio
    async def test_disconnect_only_stops_target_thread(self) -> None:
        """disconnect only stops that thread's client."""
        repo = _make_mock_chat_session_repo()
        builder = _make_mock_options_builder()
        client_a = _make_mock_user_client(directory="/proj/a")
        client_b = _make_mock_user_client(directory="/proj/b")

        with patch(
            "src.claude.client_manager.UserClient", side_effect=[client_a, client_b]
        ):
            manager = ClientManager(
                chat_session_repo=repo,
                options_builder=builder,
            )
            await manager.get_or_connect(
                user_id=_UID, chat_id=_CID, message_thread_id=1, directory="/proj/a"
            )
            await manager.get_or_connect(
                user_id=_UID, chat_id=_CID, message_thread_id=2, directory="/proj/b"
            )
            await manager.disconnect(_UID, _CID, 1)

        client_a.stop.assert_awaited_once()
        client_b.stop.assert_not_awaited()
        assert (_UID, _CID, 1) not in manager._clients
        assert (_UID, _CID, 2) in manager._clients

    @pytest.mark.asyncio
    async def test_disconnect_all(self) -> None:
        """Stops all clients."""
        repo = _make_mock_chat_session_repo()
        builder = _make_mock_options_builder()
        client_a = _make_mock_user_client(directory="/dir/a")
        client_b = _make_mock_user_client(directory="/dir/b")

        with patch(
            "src.claude.client_manager.UserClient", side_effect=[client_a, client_b]
        ):
            manager = ClientManager(
                chat_session_repo=repo,
                options_builder=builder,
            )
            await manager.get_or_connect(
                user_id=1, chat_id=_CID, message_thread_id=1, directory="/dir/a"
            )
            await manager.get_or_connect(
                user_id=2, chat_id=_CID, message_thread_id=2, directory="/dir/b"
            )
            await manager.disconnect_all()

        client_a.stop.assert_awaited_once()
        client_b.stop.assert_awaited_once()
        assert len(manager._clients) == 0


class TestGetAllClientsForUser:
    """Test get_all_clients_for_user returns all (chat_id, thread_id, client) tuples."""

    @pytest.mark.asyncio
    async def test_get_all_clients_for_user(self) -> None:
        """Returns all active triples for a user."""
        repo = _make_mock_chat_session_repo()
        builder = _make_mock_options_builder()
        client_a = _make_mock_user_client(directory="/proj/a")
        client_b = _make_mock_user_client(directory="/proj/b")
        client_other = _make_mock_user_client(directory="/other/dir")

        with patch(
            "src.claude.client_manager.UserClient",
            side_effect=[client_a, client_b, client_other],
        ):
            manager = ClientManager(
                chat_session_repo=repo,
                options_builder=builder,
            )
            await manager.get_or_connect(
                user_id=1, chat_id=_CID, message_thread_id=1, directory="/proj/a"
            )
            await manager.get_or_connect(
                user_id=1, chat_id=_CID, message_thread_id=2, directory="/proj/b"
            )
            await manager.get_or_connect(
                user_id=2, chat_id=_CID, message_thread_id=3, directory="/other/dir"
            )

        result = manager.get_all_clients_for_user(1)
        thread_ids = {tid for _, tid, _ in result}
        assert thread_ids == {1, 2}
        assert all(c is not client_other for _, _, c in result)

    def test_get_all_clients_for_user_empty(self) -> None:
        """Returns empty list for unknown user."""
        repo = _make_mock_chat_session_repo()
        manager = ClientManager(chat_session_repo=repo)
        result = manager.get_all_clients_for_user(999)
        assert result == []


class TestDisconnectAllForUser:
    """Test disconnect_all_for_user stops only that user's clients."""

    @pytest.mark.asyncio
    async def test_disconnect_all_for_user(self) -> None:
        """Stops all clients for user 1 but not user 2."""
        repo = _make_mock_chat_session_repo()
        builder = _make_mock_options_builder()
        client_a = _make_mock_user_client(directory="/proj/a")
        client_b = _make_mock_user_client(directory="/proj/b")
        client_other = _make_mock_user_client(directory="/other/dir")

        with patch(
            "src.claude.client_manager.UserClient",
            side_effect=[client_a, client_b, client_other],
        ):
            manager = ClientManager(
                chat_session_repo=repo,
                options_builder=builder,
            )
            await manager.get_or_connect(
                user_id=1, chat_id=_CID, message_thread_id=1, directory="/proj/a"
            )
            await manager.get_or_connect(
                user_id=1, chat_id=_CID, message_thread_id=2, directory="/proj/b"
            )
            await manager.get_or_connect(
                user_id=2, chat_id=_CID, message_thread_id=3, directory="/other/dir"
            )
            await manager.disconnect_all_for_user(1)

        client_a.stop.assert_awaited_once()
        client_b.stop.assert_awaited_once()
        client_other.stop.assert_not_awaited()
        assert (2, _CID, 3) in manager._clients


class TestPersistence:
    """Test session persistence to/from ChatSessionRepository."""

    @pytest.mark.asyncio
    async def test_persists_session_on_connect(self) -> None:
        """Calls chat_session_repo.upsert() after connecting."""
        repo = _make_mock_chat_session_repo()
        builder = _make_mock_options_builder()
        mock_client = _make_mock_user_client(session_id="sess-abc")

        with patch("src.claude.client_manager.UserClient", return_value=mock_client):
            manager = ClientManager(
                chat_session_repo=repo,
                options_builder=builder,
            )
            await manager.get_or_connect(
                user_id=_UID,
                chat_id=_CID,
                message_thread_id=_TID,
                directory="/some/dir",
                session_id="sess-abc",
            )

        repo.upsert.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_restores_from_persisted_state(self) -> None:
        """Reads ChatSessionModel from repo and uses persisted session_id."""
        from src.storage.models import ChatSessionModel

        persisted = ChatSessionModel(
            chat_id=_CID,
            message_thread_id=_TID,
            user_id=_UID,
            directory="/some/dir",
            session_id="persisted-session",
        )
        repo = _make_mock_chat_session_repo()
        repo.get = AsyncMock(return_value=persisted)
        builder = _make_mock_options_builder()
        mock_client = _make_mock_user_client(session_id="persisted-session")

        with patch(
            "src.claude.client_manager.UserClient", return_value=mock_client
        ) as mock_cls:
            manager = ClientManager(
                chat_session_repo=repo,
                options_builder=builder,
            )
            result = await manager.get_or_connect(
                user_id=_UID,
                chat_id=_CID,
                message_thread_id=_TID,
                directory="/some/dir",
            )

        repo.get.assert_awaited_once_with(_CID, _TID)
        init_kwargs = mock_cls.call_args.kwargs
        assert init_kwargs.get("session_id") == "persisted-session"


class TestSwitchSession:
    """Test switch_session() disconnects current then connects new."""

    @pytest.mark.asyncio
    async def test_switch_session(self) -> None:
        """Stops current, starts new session."""
        repo = _make_mock_chat_session_repo()
        builder = _make_mock_options_builder()
        old_client = _make_mock_user_client(directory="/dir", session_id="old-sess")
        new_client = _make_mock_user_client(directory="/dir", session_id="new-sess")

        with patch(
            "src.claude.client_manager.UserClient", side_effect=[old_client, new_client]
        ):
            manager = ClientManager(
                chat_session_repo=repo,
                options_builder=builder,
            )
            await manager.get_or_connect(
                user_id=_UID, chat_id=_CID, message_thread_id=_TID, directory="/dir"
            )
            result = await manager.switch_session(
                user_id=_UID,
                chat_id=_CID,
                message_thread_id=_TID,
                session_id="new-sess",
                directory="/dir",
            )

        old_client.stop.assert_awaited_once()
        new_client.start.assert_awaited_once()
        assert result is new_client


class TestSetModel:
    """Test set_model() delegates to client and flags model change."""

    @pytest.mark.asyncio
    async def test_set_model(self) -> None:
        """Calls client.set_model() to flag reconnect; does not persist to DB."""
        repo = _make_mock_chat_session_repo()
        builder = _make_mock_options_builder()
        mock_client = _make_mock_user_client(session_id="some-session")

        with patch("src.claude.client_manager.UserClient", return_value=mock_client):
            manager = ClientManager(
                chat_session_repo=repo,
                options_builder=builder,
            )
            await manager.get_or_connect(
                user_id=_UID,
                chat_id=_CID,
                message_thread_id=_TID,
                directory="/some/dir",
            )
            repo.upsert.reset_mock()

            await manager.set_model(
                user_id=_UID,
                chat_id=_CID,
                message_thread_id=_TID,
                model="claude-opus-4-6",
            )

        mock_client.set_model.assert_called_once_with("claude-opus-4-6", None)
        repo.upsert.assert_not_awaited()


@pytest.fixture
def mock_repo() -> MagicMock:
    return _make_mock_chat_session_repo()


@pytest.fixture
def manager(mock_repo: MagicMock) -> ClientManager:
    return ClientManager(
        chat_session_repo=mock_repo,
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
        manager._clients[(_UID, _CID, _TID)] = mock_client

        await manager.update_session_id(_UID, _CID, _TID, "/tmp/project", "new-session")

        assert mock_client.session_id == "new-session"
        mock_repo.upsert.assert_awaited_once()


class TestMakeOnExit:
    """Test _make_on_exit closure cleans up the correct key."""

    def test_on_exit_removes_correct_key(self) -> None:
        """Closure removes (user_id, chat_id, message_thread_id) key, not others."""
        repo = _make_mock_chat_session_repo()
        manager = ClientManager(chat_session_repo=repo)
        client_a = _make_mock_user_client(directory="/proj/a")
        client_b = _make_mock_user_client(directory="/proj/b")
        manager._clients[(_UID, _CID, 1)] = client_a
        manager._clients[(_UID, _CID, 2)] = client_b

        on_exit = manager._make_on_exit(_UID, _CID, 1)
        on_exit(_UID)

        assert (_UID, _CID, 1) not in manager._clients
        assert (_UID, _CID, 2) in manager._clients

    def test_on_exit_noop_for_unknown_key(self) -> None:
        """Closure doesn't raise if key already removed."""
        repo = _make_mock_chat_session_repo()
        manager = ClientManager(chat_session_repo=repo)

        on_exit = manager._make_on_exit(999, 999, 0)
        on_exit(999)


class TestGetAvailableCommands:
    """Test get_available_commands() delegates to active client."""

    def test_returns_commands_from_active_client(self) -> None:
        repo = _make_mock_chat_session_repo()
        manager = ClientManager(chat_session_repo=repo)
        mock_client = _make_mock_user_client()
        mock_client.available_commands = [
            {"name": "brainstorm", "description": "Ideas", "argumentHint": ""},
        ]
        manager._clients[(_UID, _CID, _TID)] = mock_client
        result = manager.get_available_commands(
            user_id=_UID, chat_id=_CID, message_thread_id=_TID
        )
        assert len(result) == 1
        assert result[0]["name"] == "brainstorm"

    def test_returns_empty_for_unknown_user(self) -> None:
        repo = _make_mock_chat_session_repo()
        manager = ClientManager(chat_session_repo=repo)
        result = manager.get_available_commands(
            user_id=999, chat_id=999, message_thread_id=0
        )
        assert result == []
