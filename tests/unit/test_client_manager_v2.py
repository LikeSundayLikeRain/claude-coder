"""Tests for ClientManager with (user_id, chat_id, message_thread_id) key."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.claude.client_manager import ClientManager
from src.storage.repositories import ChatSessionRepository


@pytest.fixture
def chat_session_repo():
    repo = AsyncMock(spec=ChatSessionRepository)
    repo.get.return_value = None
    repo.upsert.return_value = None
    return repo


@pytest.fixture
def options_builder():
    builder = MagicMock()
    builder.build.return_value = MagicMock()
    return builder


@pytest.fixture
def manager(chat_session_repo, options_builder):
    return ClientManager(
        chat_session_repo=chat_session_repo,
        options_builder=options_builder,
        idle_timeout=60,
    )


def make_mock_client(session_id: str = "test-session") -> MagicMock:
    client = MagicMock()
    client.is_connected = True
    client.session_id = session_id
    client.start = AsyncMock()
    client.stop = AsyncMock()
    client.interrupt = AsyncMock()
    client.available_commands = []
    return client


class TestClientsDictUsesTripleKey:
    def test_clients_dict_is_empty_on_init(self, manager):
        assert manager._clients == {}

    def test_clients_dict_type_annotation(self, manager):
        # Verify by inserting a triple-keyed entry
        mock_client = make_mock_client()
        manager._clients[(1, 100, 0)] = mock_client
        assert (1, 100, 0) in manager._clients
        assert manager._clients[(1, 100, 0)] is mock_client


class TestGetOrConnect:
    @pytest.mark.asyncio
    async def test_creates_client_with_triple_key(self, manager, chat_session_repo):
        mock_client = make_mock_client()
        with patch("src.claude.client_manager.UserClient", return_value=mock_client):
            result = await manager.get_or_connect(
                user_id=1,
                chat_id=100,
                message_thread_id=0,
                directory="/proj",
            )

        assert result is mock_client
        assert (1, 100, 0) in manager._clients

    @pytest.mark.asyncio
    async def test_returns_existing_connected_client(self, manager):
        mock_client = make_mock_client()
        manager._clients[(1, 100, 0)] = mock_client

        with patch("src.claude.client_manager.UserClient") as MockUserClient:
            result = await manager.get_or_connect(
                user_id=1,
                chat_id=100,
                message_thread_id=0,
                directory="/proj",
            )
            MockUserClient.assert_not_called()

        assert result is mock_client

    @pytest.mark.asyncio
    async def test_force_new_replaces_existing_client(self, manager):
        old_client = make_mock_client("old-session")
        manager._clients[(1, 100, 0)] = old_client
        new_client = make_mock_client("new-session")

        with patch("src.claude.client_manager.UserClient", return_value=new_client):
            result = await manager.get_or_connect(
                user_id=1,
                chat_id=100,
                message_thread_id=0,
                directory="/proj",
                force_new=True,
            )

        old_client.stop.assert_awaited_once()
        assert result is new_client

    @pytest.mark.asyncio
    async def test_resolves_session_from_repo(self, manager, chat_session_repo):
        session_row = MagicMock()
        session_row.session_id = "persisted-session"
        chat_session_repo.get.return_value = session_row

        mock_client = make_mock_client("persisted-session")
        with patch("src.claude.client_manager.UserClient", return_value=mock_client):
            await manager.get_or_connect(
                user_id=1,
                chat_id=100,
                message_thread_id=42,
                directory="/proj",
            )

        chat_session_repo.get.assert_awaited_once_with(100, 42)

    @pytest.mark.asyncio
    async def test_skips_repo_lookup_when_force_new(self, manager, chat_session_repo):
        mock_client = make_mock_client()
        with patch("src.claude.client_manager.UserClient", return_value=mock_client):
            await manager.get_or_connect(
                user_id=1,
                chat_id=100,
                message_thread_id=0,
                directory="/proj",
                force_new=True,
            )

        chat_session_repo.get.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_persists_session_after_connect(self, manager, chat_session_repo):
        mock_client = make_mock_client("new-session")
        with patch("src.claude.client_manager.UserClient", return_value=mock_client):
            await manager.get_or_connect(
                user_id=1,
                chat_id=100,
                message_thread_id=0,
                directory="/proj",
            )

        chat_session_repo.upsert.assert_awaited_once_with(
            100, 0, 1, "/proj", "new-session"
        )

    @pytest.mark.asyncio
    async def test_different_threads_create_separate_clients(
        self, manager, chat_session_repo
    ):
        client_a = make_mock_client("session-a")
        client_b = make_mock_client("session-b")
        clients = [client_a, client_b]

        with patch("src.claude.client_manager.UserClient", side_effect=clients):
            result_a = await manager.get_or_connect(
                user_id=1, chat_id=100, message_thread_id=1, directory="/a"
            )
            result_b = await manager.get_or_connect(
                user_id=1, chat_id=100, message_thread_id=2, directory="/b"
            )

        assert result_a is client_a
        assert result_b is client_b
        assert (1, 100, 1) in manager._clients
        assert (1, 100, 2) in manager._clients


class TestDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_removes_triple_key(self, manager):
        mock_client = make_mock_client()
        manager._clients[(1, 100, 0)] = mock_client

        await manager.disconnect(user_id=1, chat_id=100, message_thread_id=0)

        assert (1, 100, 0) not in manager._clients
        mock_client.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_disconnect_noop_when_no_client(self, manager):
        # Should not raise
        await manager.disconnect(user_id=1, chat_id=100, message_thread_id=0)

    @pytest.mark.asyncio
    async def test_disconnect_only_removes_matching_key(self, manager):
        client_a = make_mock_client()
        client_b = make_mock_client()
        manager._clients[(1, 100, 1)] = client_a
        manager._clients[(1, 100, 2)] = client_b

        await manager.disconnect(user_id=1, chat_id=100, message_thread_id=1)

        assert (1, 100, 1) not in manager._clients
        assert (1, 100, 2) in manager._clients
        client_b.stop.assert_not_awaited()


class TestGetAllClientsForUser:
    def test_returns_chat_id_thread_id_client_tuples(self, manager):
        client_a = make_mock_client()
        client_b = make_mock_client()
        manager._clients[(1, 100, 1)] = client_a
        manager._clients[(1, 100, 2)] = client_b
        manager._clients[(2, 200, 0)] = make_mock_client()  # different user

        results = manager.get_all_clients_for_user(user_id=1)

        assert len(results) == 2
        chat_ids = {r[0] for r in results}
        thread_ids = {r[1] for r in results}
        clients = {r[2] for r in results}
        assert chat_ids == {100}
        assert thread_ids == {1, 2}
        assert clients == {client_a, client_b}

    def test_returns_empty_for_unknown_user(self, manager):
        manager._clients[(1, 100, 0)] = make_mock_client()
        assert manager.get_all_clients_for_user(user_id=99) == []

    def test_tuple_shape_is_chat_id_thread_id_client(self, manager):
        mock_client = make_mock_client()
        manager._clients[(1, 100, 42)] = mock_client

        results = manager.get_all_clients_for_user(user_id=1)

        assert len(results) == 1
        chat_id, thread_id, client = results[0]
        assert chat_id == 100
        assert thread_id == 42
        assert client is mock_client


class TestUpdateSessionId:
    @pytest.mark.asyncio
    async def test_calls_repo_upsert(self, manager, chat_session_repo):
        mock_client = make_mock_client("old")
        manager._clients[(1, 100, 0)] = mock_client

        await manager.update_session_id(
            user_id=1,
            chat_id=100,
            message_thread_id=0,
            directory="/proj",
            session_id="new-session",
        )

        chat_session_repo.upsert.assert_awaited_once_with(
            100, 0, 1, "/proj", "new-session"
        )

    @pytest.mark.asyncio
    async def test_updates_in_memory_session_id(self, manager, chat_session_repo):
        mock_client = make_mock_client("old")
        manager._clients[(1, 100, 0)] = mock_client

        await manager.update_session_id(
            user_id=1,
            chat_id=100,
            message_thread_id=0,
            directory="/proj",
            session_id="new-session",
        )

        assert mock_client.session_id == "new-session"

    @pytest.mark.asyncio
    async def test_still_calls_repo_when_no_in_memory_client(
        self, manager, chat_session_repo
    ):
        # No client in dict — repo upsert should still happen
        await manager.update_session_id(
            user_id=1,
            chat_id=100,
            message_thread_id=0,
            directory="/proj",
            session_id="new-session",
        )

        chat_session_repo.upsert.assert_awaited_once_with(
            100, 0, 1, "/proj", "new-session"
        )
