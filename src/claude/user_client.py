"""Wraps a persistent ClaudeSDKClient for a single user."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any, Optional

import structlog
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage
from claude_agent_sdk._errors import MessageParseError
from claude_agent_sdk._internal.message_parser import parse_message

logger = structlog.get_logger()


class UserClient:
    """A persistent Claude SDK client for one user."""

    def __init__(
        self,
        user_id: int,
        directory: str,
        session_id: Optional[str] = None,
        model: Optional[str] = None,
        betas: Optional[list[str]] = None,
    ) -> None:
        self.user_id = user_id
        self.directory = directory
        self.session_id = session_id
        self.model = model
        self.betas = betas
        self.last_active = datetime.now(UTC)

        self._sdk_client: Optional[ClaudeSDKClient] = None
        self._connected = False
        self._querying = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def is_querying(self) -> bool:
        return self._querying

    async def connect(self, options: ClaudeAgentOptions) -> None:
        """Create and connect the SDK client."""
        if self._connected:
            await self.disconnect()
        self._sdk_client = ClaudeSDKClient(options)
        await self._sdk_client.connect()
        self._connected = True
        self.touch()
        logger.info(
            "user_client_connected",
            user_id=self.user_id,
            directory=self.directory,
            session_id=self.session_id,
        )

    async def disconnect(self) -> None:
        """Disconnect and clean up the SDK client."""
        if self._sdk_client is not None:
            try:
                await self._sdk_client.disconnect()
            except Exception as e:
                logger.warning("disconnect_error", error=str(e))
            finally:
                self._sdk_client = None
                self._connected = False
                self._querying = False

    async def query(self, prompt: str) -> AsyncGenerator[Any, None]:
        """Send a query and yield parsed SDK messages.

        Uses client._query.receive_messages() to get raw data, then
        parses each message ourselves.  This mirrors the working pattern
        in sdk_integration.py and avoids MessageParseError killing the
        underlying async generator.
        """
        if not self._connected or self._sdk_client is None:
            raise RuntimeError("UserClient not connected. Call connect() first.")
        self._querying = True
        self.touch()
        try:
            await self._sdk_client.query(prompt)
            async for raw_data in self._sdk_client._query.receive_messages():
                try:
                    message = parse_message(raw_data)
                except MessageParseError as e:
                    logger.debug("skipping_unparseable_message", error=str(e))
                    continue
                yield message
                if isinstance(message, ResultMessage):
                    break
        finally:
            self._querying = False
            self.touch()

    async def interrupt(self) -> None:
        """Interrupt the current query if one is running."""
        if self._querying and self._sdk_client is not None:
            await self._sdk_client.interrupt()
            logger.info("query_interrupted", user_id=self.user_id)

    def touch(self) -> None:
        """Update last_active timestamp."""
        self.last_active = datetime.now(UTC)
