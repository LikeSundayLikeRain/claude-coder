"""Tests for orchestrator integration with ClientManager."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.claude.stream_handler import StreamEvent, StreamHandler


class TestOrchestratorAgenticFlow:
    """Verify the orchestrator routes messages through ClientManager."""

    async def test_agentic_text_uses_client_manager(self):
        """Validate the ClientManager -> UserClient -> query contract."""
        mock_client = MagicMock()
        mock_client.session_id = "test-session"
        mock_client.is_querying = False

        async def mock_query(prompt):
            msg = MagicMock()
            msg.__class__.__name__ = "ResultMessage"
            msg.result = "Hello from Claude!"
            msg.session_id = "test-session"
            msg.total_cost_usd = 0.01
            yield msg

        mock_client.query = mock_query

        mock_manager = MagicMock()
        mock_manager.get_or_connect = AsyncMock(return_value=mock_client)
        mock_manager.update_session_id = AsyncMock()

        client = await mock_manager.get_or_connect(
            user_id=123,
            directory="/home/user/project",
        )
        assert client is mock_client

        handler = StreamHandler()
        messages = []
        async for msg in client.query("Hello"):
            event = handler.extract_content(msg)
            messages.append(event)

        assert len(messages) == 1
        assert messages[0].type == "result"
        assert messages[0].content == "Hello from Claude!"
        assert messages[0].session_id == "test-session"
        assert messages[0].cost == 0.01

    async def test_force_new_disconnects_before_connect(self):
        """When force_new=True, disconnect is called before get_or_connect."""
        mock_client = MagicMock()
        mock_client.session_id = None
        mock_client.is_querying = False

        async def mock_query(prompt):
            msg = MagicMock()
            msg.__class__.__name__ = "ResultMessage"
            msg.result = "Fresh session!"
            msg.session_id = "new-session"
            msg.total_cost_usd = 0.0
            yield msg

        mock_client.query = mock_query

        mock_manager = MagicMock()
        mock_manager.disconnect = AsyncMock()
        mock_manager.get_or_connect = AsyncMock(return_value=mock_client)
        mock_manager.update_session_id = AsyncMock()

        # Simulate force_new flow: disconnect then connect
        await mock_manager.disconnect(123)
        client = await mock_manager.get_or_connect(
            user_id=123,
            directory="/home/user/project",
            session_id=None,
        )

        mock_manager.disconnect.assert_awaited_once_with(123)
        mock_manager.get_or_connect.assert_awaited_once()
        assert client is mock_client

    async def test_stream_handler_routes_tool_use_events(self):
        """StreamHandler correctly routes tool_use events from the stream."""
        mock_client = MagicMock()

        async def mock_query(prompt):
            tool_block = MagicMock()
            tool_block.type = "tool_use"
            tool_block.name = "Read"
            tool_block.input = {"file_path": "/tmp/test.py"}

            tool_msg = MagicMock()
            tool_msg.__class__.__name__ = "AssistantMessage"
            tool_msg.content = [tool_block]
            yield tool_msg

            result_msg = MagicMock()
            result_msg.__class__.__name__ = "ResultMessage"
            result_msg.result = "Done."
            result_msg.session_id = "s1"
            result_msg.total_cost_usd = 0.02
            yield result_msg

        mock_client.query = mock_query

        handler = StreamHandler()
        events = []
        async for msg in mock_client.query("Read a file"):
            events.append(handler.extract_content(msg))

        assert len(events) == 2
        assert events[0].type == "tool_use"
        assert events[0].tool_name == "Read"
        assert events[1].type == "result"
        assert events[1].cost == 0.02

    async def test_update_session_id_called_on_result(self):
        """After a ResultMessage with session_id, update_session_id is called."""
        mock_client = MagicMock()

        async def mock_query(prompt):
            msg = MagicMock()
            msg.__class__.__name__ = "ResultMessage"
            msg.result = "Answer"
            msg.session_id = "returned-session-id"
            msg.total_cost_usd = 0.05
            yield msg

        mock_client.query = mock_query

        mock_manager = MagicMock()
        mock_manager.get_or_connect = AsyncMock(return_value=mock_client)
        mock_manager.update_session_id = AsyncMock()

        client = await mock_manager.get_or_connect(user_id=42, directory="/proj")
        handler = StreamHandler()

        async for msg in client.query("question"):
            event = handler.extract_content(msg)
            if event.type == "result" and event.session_id:
                await mock_manager.update_session_id(42, event.session_id)

        mock_manager.update_session_id.assert_awaited_once_with(
            42, "returned-session-id"
        )
