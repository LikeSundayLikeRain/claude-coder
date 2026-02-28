"""Tests for UserClient actor pattern."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.claude.user_client import QueryResult, UserClient, WorkItem


class TestUserClientInitialState:
    """Test initial state of a newly created UserClient."""

    def test_initial_state(self) -> None:
        client = UserClient(user_id=42, directory="/some/dir")
        assert client.user_id == 42
        assert client.directory == "/some/dir"
        assert client.session_id is None
        assert client.is_connected is False
        assert client.is_querying is False

    def test_initial_state_with_optional_params(self) -> None:
        client = UserClient(
            user_id=1,
            directory="/dir",
            session_id="abc",
            model="claude-sonnet-4-5",
            idle_timeout=120,
        )
        assert client.session_id == "abc"
        assert client.model == "claude-sonnet-4-5"
        assert client.idle_timeout == 120


class TestUserClientStart:
    """Test start() spawns worker and connects."""

    @pytest.mark.asyncio
    async def test_start_sets_connected(self) -> None:
        mock_sdk = AsyncMock()
        mock_options = MagicMock()

        with patch("src.claude.user_client.ClaudeSDKClient", return_value=mock_sdk):
            client = UserClient(user_id=1, directory="/dir")
            await client.start(mock_options)
            assert client.is_connected is True
            mock_sdk.connect.assert_awaited_once()
            await client.stop()

    @pytest.mark.asyncio
    async def test_start_when_already_running_stops_first(self) -> None:
        mock_sdk_1 = AsyncMock()
        mock_sdk_2 = AsyncMock()
        mock_options = MagicMock()

        with patch(
            "src.claude.user_client.ClaudeSDKClient",
            side_effect=[mock_sdk_1, mock_sdk_2],
        ):
            client = UserClient(user_id=1, directory="/dir")
            await client.start(mock_options)
            await client.start(mock_options)
            assert client.is_connected is True
            mock_sdk_1.disconnect.assert_awaited_once()
            await client.stop()


class TestUserClientStartFailure:
    """Test start() propagates errors when connect fails."""

    @pytest.mark.asyncio
    async def test_start_raises_on_connect_failure(self) -> None:
        mock_sdk = AsyncMock()
        mock_sdk.connect.side_effect = RuntimeError("connection refused")
        mock_options = MagicMock()

        with patch(
            "src.claude.user_client.ClaudeSDKClient", return_value=mock_sdk
        ):
            client = UserClient(user_id=1, directory="/dir")
            with pytest.raises(RuntimeError, match="connection refused"):
                await client.start(mock_options)
            assert client.is_connected is False


class TestUserClientStop:
    """Test stop() cleanly shuts down the worker."""

    @pytest.mark.asyncio
    async def test_stop_disconnects(self) -> None:
        mock_sdk = AsyncMock()
        mock_options = MagicMock()

        with patch("src.claude.user_client.ClaudeSDKClient", return_value=mock_sdk):
            client = UserClient(user_id=1, directory="/dir")
            await client.start(mock_options)
            await client.stop()
            assert client.is_connected is False
            mock_sdk.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_noop_when_not_running(self) -> None:
        client = UserClient(user_id=1, directory="/dir")
        await client.stop()  # should not raise
        assert client.is_connected is False


class TestUserClientSubmit:
    """Test submit() enqueues work and returns result."""

    @pytest.mark.asyncio
    async def test_submit_returns_result(self) -> None:
        client = UserClient(user_id=1, directory="/dir")
        client._running = True
        client._queue = asyncio.Queue()

        async def fake_consumer() -> None:
            item = await client._queue.get()
            item.future.set_result(QueryResult(response_text="hello", session_id="s1"))

        task = asyncio.create_task(fake_consumer())
        result = await client.submit("test prompt")
        assert result.response_text == "hello"
        assert result.session_id == "s1"
        await task

    @pytest.mark.asyncio
    async def test_submit_when_not_running_raises(self) -> None:
        client = UserClient(user_id=1, directory="/dir")
        with pytest.raises(RuntimeError, match="not running"):
            await client.submit("hello")


class TestUserClientInterrupt:
    """Test interrupt() delegates to SDK client."""

    @pytest.mark.asyncio
    async def test_interrupt_delegates_to_sdk(self) -> None:
        mock_sdk = AsyncMock()
        client = UserClient(user_id=1, directory="/dir")
        client._sdk_client = mock_sdk
        client._querying = True

        await client.interrupt()
        mock_sdk.interrupt.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_interrupt_noop_when_not_querying(self) -> None:
        mock_sdk = AsyncMock()
        client = UserClient(user_id=1, directory="/dir")
        client._sdk_client = mock_sdk
        client._querying = False

        await client.interrupt()
        mock_sdk.interrupt.assert_not_awaited()


class TestUserClientIdleTimeout:
    """Test that worker exits on idle timeout."""

    @pytest.mark.asyncio
    async def test_idle_timeout_stops_actor(self) -> None:
        client = UserClient(
            user_id=123,
            directory="/test",
            idle_timeout=0.1,  # 100ms for fast test
        )
        mock_sdk = AsyncMock()
        with patch("src.claude.user_client.ClaudeSDKClient", return_value=mock_sdk):
            await client.start(MagicMock())
            # Don't submit anything — let it idle timeout
            await asyncio.sleep(0.3)
            assert not client.is_connected
            mock_sdk.disconnect.assert_awaited_once()


class TestUserClientOnExit:
    """Test on_exit callback is called when worker stops."""

    @pytest.mark.asyncio
    async def test_on_exit_called_on_stop(self) -> None:
        exit_called: dict[str, int] = {}

        def on_exit(uid: int) -> None:
            exit_called["user_id"] = uid

        mock_sdk = AsyncMock()
        with patch("src.claude.user_client.ClaudeSDKClient", return_value=mock_sdk):
            client = UserClient(user_id=42, directory="/dir", on_exit=on_exit)
            await client.start(MagicMock())
            await client.stop()

        assert exit_called.get("user_id") == 42

    @pytest.mark.asyncio
    async def test_on_exit_called_on_idle_timeout(self) -> None:
        exit_called: dict[str, int] = {}

        def on_exit(uid: int) -> None:
            exit_called["user_id"] = uid

        mock_sdk = AsyncMock()
        with patch("src.claude.user_client.ClaudeSDKClient", return_value=mock_sdk):
            client = UserClient(
                user_id=42, directory="/dir", idle_timeout=0.1, on_exit=on_exit
            )
            await client.start(MagicMock())
            await asyncio.sleep(0.3)

        assert exit_called.get("user_id") == 42


class TestWorkItem:
    def test_work_item_creation(self) -> None:
        future = asyncio.get_event_loop().create_future()
        item = WorkItem(prompt="hello", future=future)
        assert item.prompt == "hello"
        assert item.on_stream is None
        assert item.future is future

    def test_work_item_with_callback(self) -> None:
        future = asyncio.get_event_loop().create_future()

        async def cb(x: object) -> None:
            pass

        item = WorkItem(prompt="hi", on_stream=cb, future=future)
        assert item.on_stream is cb


class TestUserClientSkillsCache:
    """Test get_server_info() caching after connect."""

    @pytest.mark.asyncio
    async def test_available_commands_populated_after_start(self) -> None:
        mock_sdk = AsyncMock()
        mock_sdk.get_server_info = AsyncMock(return_value={
            "commands": [
                {"name": "brainstorm", "description": "Brainstorm ideas", "argumentHint": "<topic>"},
                {"name": "commit", "description": "Commit changes", "argumentHint": ""},
            ]
        })
        mock_options = MagicMock()

        with patch("src.claude.user_client.ClaudeSDKClient", return_value=mock_sdk):
            client = UserClient(user_id=1, directory="/dir")
            await client.start(mock_options)
            assert len(client.available_commands) == 2
            assert client.available_commands[0]["name"] == "brainstorm"
            await client.stop()

    @pytest.mark.asyncio
    async def test_available_commands_empty_on_server_info_failure(self) -> None:
        mock_sdk = AsyncMock()
        mock_sdk.get_server_info = AsyncMock(return_value=None)
        mock_options = MagicMock()

        with patch("src.claude.user_client.ClaudeSDKClient", return_value=mock_sdk):
            client = UserClient(user_id=1, directory="/dir")
            await client.start(mock_options)
            assert client.available_commands == []
            await client.stop()

    @pytest.mark.asyncio
    async def test_available_commands_cleared_after_stop(self) -> None:
        mock_sdk = AsyncMock()
        mock_sdk.get_server_info = AsyncMock(return_value={
            "commands": [{"name": "test", "description": "", "argumentHint": ""}]
        })
        mock_options = MagicMock()

        with patch("src.claude.user_client.ClaudeSDKClient", return_value=mock_sdk):
            client = UserClient(user_id=1, directory="/dir")
            await client.start(mock_options)
            assert len(client.available_commands) == 1
            await client.stop()
            assert client.available_commands == []

    def test_has_command_checks_cache(self) -> None:
        client = UserClient(user_id=1, directory="/dir")
        client._available_commands = [
            {"name": "brainstorm", "description": "Ideas", "argumentHint": ""},
            {"name": "commit", "description": "Commit", "argumentHint": ""},
        ]
        assert client.has_command("brainstorm") is True
        assert client.has_command("nonexistent") is False


class TestQueryResult:
    def test_query_result_creation(self) -> None:
        result = QueryResult(
            response_text="hello",
            session_id="sess-1",
            cost=0.01,
            num_turns=2,
            duration_ms=1500,
        )
        assert result.response_text == "hello"
        assert result.session_id == "sess-1"
        assert result.cost == 0.01

    def test_query_result_defaults(self) -> None:
        result = QueryResult(response_text="hello")
        assert result.session_id is None
        assert result.cost == 0.0
        assert result.num_turns == 0
        assert result.duration_ms == 0
