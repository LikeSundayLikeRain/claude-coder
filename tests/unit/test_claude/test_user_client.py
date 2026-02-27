"""Tests for UserClient: per-user persistent ClaudeSDKClient wrapper."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.claude.user_client import UserClient


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
        )
        assert client.session_id == "abc"
        assert client.model == "claude-sonnet-4-5"


class TestUserClientConnect:
    """Test connect() creates and connects a ClaudeSDKClient."""

    @pytest.mark.asyncio
    async def test_connect_creates_sdk_client(self) -> None:
        mock_sdk = AsyncMock()
        mock_options = MagicMock()

        with patch(
            "src.claude.user_client.ClaudeSDKClient", return_value=mock_sdk
        ) as mock_cls:
            uc = UserClient(user_id=1, directory="/dir")
            await uc.connect(mock_options)

            mock_cls.assert_called_once_with(mock_options)
            mock_sdk.connect.assert_awaited_once()
            assert uc.is_connected is True

    @pytest.mark.asyncio
    async def test_connect_disconnects_existing_client(self) -> None:
        """If already connected, connect() disconnects first."""
        mock_sdk_old = AsyncMock()
        mock_sdk_new = AsyncMock()
        mock_options = MagicMock()

        with patch(
            "src.claude.user_client.ClaudeSDKClient",
            side_effect=[mock_sdk_old, mock_sdk_new],
        ):
            uc = UserClient(user_id=1, directory="/dir")
            await uc.connect(mock_options)
            assert uc.is_connected is True

            await uc.connect(mock_options)
            mock_sdk_old.disconnect.assert_awaited_once()
            assert uc.is_connected is True


class TestUserClientDisconnect:
    """Test disconnect() cleans up the SDK client."""

    @pytest.mark.asyncio
    async def test_disconnect_cleans_up(self) -> None:
        mock_sdk = AsyncMock()
        mock_options = MagicMock()

        with patch("src.claude.user_client.ClaudeSDKClient", return_value=mock_sdk):
            uc = UserClient(user_id=1, directory="/dir")
            await uc.connect(mock_options)
            assert uc.is_connected is True

            await uc.disconnect()

            mock_sdk.disconnect.assert_awaited_once()
            assert uc.is_connected is False
            assert uc._sdk_client is None

    @pytest.mark.asyncio
    async def test_disconnect_noop_when_not_connected(self) -> None:
        uc = UserClient(user_id=1, directory="/dir")
        # Should not raise
        await uc.disconnect()
        assert uc.is_connected is False


class TestUserClientInterrupt:
    """Test interrupt() delegates to SDK client."""

    @pytest.mark.asyncio
    async def test_interrupt_delegates_to_sdk(self) -> None:
        mock_sdk = AsyncMock()
        mock_options = MagicMock()

        with patch("src.claude.user_client.ClaudeSDKClient", return_value=mock_sdk):
            uc = UserClient(user_id=1, directory="/dir")
            await uc.connect(mock_options)
            # Simulate querying state
            uc._querying = True

            await uc.interrupt()

            mock_sdk.interrupt.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_interrupt_noop_when_not_querying(self) -> None:
        mock_sdk = AsyncMock()
        mock_options = MagicMock()

        with patch("src.claude.user_client.ClaudeSDKClient", return_value=mock_sdk):
            uc = UserClient(user_id=1, directory="/dir")
            await uc.connect(mock_options)
            assert uc.is_querying is False

            # Should not raise and should NOT call interrupt
            await uc.interrupt()

            mock_sdk.interrupt.assert_not_awaited()


class TestUserClientQuery:
    """Test query() method behavior."""

    @pytest.mark.asyncio
    async def test_query_raises_when_not_connected(self) -> None:
        """query() raises RuntimeError when not connected."""
        uc = UserClient(user_id=123, directory="/tmp")

        with pytest.raises(RuntimeError, match="not connected"):
            async for _ in uc.query("hello"):
                pass

    @pytest.mark.asyncio
    async def test_query_sets_querying_state(self) -> None:
        """query() sets is_querying during execution."""
        uc = UserClient(user_id=123, directory="/tmp")

        mock_sdk = AsyncMock()

        # Raw data dict that parse_message will receive
        raw_msg = {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}

        async def mock_receive():
            yield raw_msg

        # The code accesses sdk_client._query.receive_messages()
        mock_query_obj = MagicMock()
        mock_query_obj.receive_messages = mock_receive
        mock_sdk._query = mock_query_obj

        uc._sdk_client = mock_sdk
        uc._connected = True

        messages = []
        with patch("src.claude.user_client.parse_message") as mock_parse:
            parsed_msg = MagicMock()
            mock_parse.return_value = parsed_msg

            async for msg in uc.query("hello"):
                assert uc.is_querying is True
                messages.append(msg)

        assert uc.is_querying is False
        assert len(messages) == 1

    @pytest.mark.asyncio
    async def test_query_resets_querying_on_error(self) -> None:
        """query() resets is_querying even if an error occurs."""
        uc = UserClient(user_id=123, directory="/tmp")

        mock_sdk = AsyncMock()
        mock_sdk.query.side_effect = Exception("SDK error")

        uc._sdk_client = mock_sdk
        uc._connected = True

        with pytest.raises(Exception, match="SDK error"):
            async for _ in uc.query("hello"):
                pass

        assert uc.is_querying is False


class TestUserClientTouch:
    """Test touch() updates last_active."""

    def test_touch_updates_last_active(self) -> None:
        uc = UserClient(user_id=1, directory="/dir")
        before = uc.last_active
        # Force a tiny sleep equivalent by nudging time
        uc.touch()
        after = uc.last_active
        # last_active must be a timezone-aware UTC datetime
        assert after.tzinfo is not None
        assert after >= before
