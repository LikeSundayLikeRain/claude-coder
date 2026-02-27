"""Wraps a persistent ClaudeSDKClient for a single user."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

import structlog
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage
from claude_agent_sdk._errors import MessageParseError
from claude_agent_sdk._internal.message_parser import parse_message

from .stream_handler import StreamHandler

logger = structlog.get_logger()


@dataclass
class WorkItem:
    """A unit of work for the actor's queue."""

    prompt: str
    future: asyncio.Future[Any]
    on_stream: Optional[Callable[..., Any]] = None


@dataclass
class QueryResult:
    """Result returned from a completed query."""

    response_text: str
    session_id: Optional[str] = None
    cost: float = 0.0
    num_turns: int = 0
    duration_ms: int = 0


class UserClient:
    """Actor-based Claude SDK client for one user.

    A single long-lived asyncio task owns the full SDK lifecycle:
    connect → query → ... → query → disconnect.  This satisfies
    the SDK's requirement that all operations happen in the same
    async context (anyio cancel scope constraint).
    """

    def __init__(
        self,
        user_id: int,
        directory: str,
        session_id: Optional[str] = None,
        model: Optional[str] = None,
        betas: Optional[list[str]] = None,
        idle_timeout: float = 3600,
        on_exit: Optional[Callable[[int], Any]] = None,
    ) -> None:
        self.user_id = user_id
        self.directory = directory
        self.session_id = session_id
        self.model = model
        self.betas = betas
        self.idle_timeout = idle_timeout
        self._on_exit = on_exit

        self._queue: asyncio.Queue[Optional[WorkItem]] = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task[None]] = None
        self._running = False
        self._querying = False
        self._sdk_client: Optional[ClaudeSDKClient] = None
        self._options: Optional[ClaudeAgentOptions] = None
        self._connected_event: asyncio.Event = asyncio.Event()
        self._connect_error: Optional[Exception] = None

    @property
    def is_connected(self) -> bool:
        return self._running

    @property
    def is_querying(self) -> bool:
        return self._querying

    async def start(self, options: ClaudeAgentOptions) -> None:
        """Spawn the worker task and connect the SDK client."""
        if self._running:
            await self.stop()
        self._options = options
        self._running = True
        self._connect_error = None
        self._connected_event.clear()
        self._worker_task = asyncio.create_task(self._worker())
        await self._connected_event.wait()
        # If connect failed, the worker stored the error for us
        if self._connect_error is not None:
            self._running = False
            raise self._connect_error

    async def stop(self) -> None:
        """Send stop sentinel and wait for worker to exit."""
        if not self._running:
            return
        self._running = False
        await self._queue.put(None)  # sentinel
        if self._worker_task is not None:
            try:
                await asyncio.wait_for(self._worker_task, timeout=10.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._worker_task.cancel()
            self._worker_task = None

    async def submit(
        self,
        prompt: str,
        on_stream: Optional[Callable[..., Any]] = None,
    ) -> QueryResult:
        """Submit a query and await the result."""
        if not self._running:
            raise RuntimeError("UserClient is not running. Call start() first.")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[QueryResult] = loop.create_future()
        await self._queue.put(
            WorkItem(prompt=prompt, on_stream=on_stream, future=future)
        )
        return await future

    async def interrupt(self) -> None:
        """Interrupt the current query if one is running."""
        if self._querying and self._sdk_client is not None:
            await self._sdk_client.interrupt()
            logger.info("query_interrupted", user_id=self.user_id)

    async def _worker(self) -> None:
        """Long-lived task: connect, process queue, disconnect."""
        try:
            self._sdk_client = ClaudeSDKClient(self._options)
            await self._sdk_client.connect()
            self._connected_event.set()
            logger.info(
                "user_client_connected",
                user_id=self.user_id,
                directory=self.directory,
                session_id=self.session_id,
            )

            while True:
                try:
                    item = await asyncio.wait_for(
                        self._queue.get(), timeout=self.idle_timeout
                    )
                except asyncio.TimeoutError:
                    logger.info("user_client_idle_timeout", user_id=self.user_id)
                    break

                if item is None:  # stop sentinel
                    break

                await self._process_item(item)

        except Exception as e:
            logger.error("worker_fatal_error", user_id=self.user_id, error=str(e))
            self._connect_error = e
            self._connected_event.set()  # unblock start() if connect failed
        finally:
            if self._sdk_client is not None:
                try:
                    await self._sdk_client.disconnect()
                except Exception as e:
                    logger.debug("disconnect_error", error=str(e))
                self._sdk_client = None
            self._running = False
            self._querying = False
            logger.info("user_client_stopped", user_id=self.user_id)
            if self._on_exit:
                try:
                    self._on_exit(self.user_id)
                except Exception:
                    pass

    async def _process_item(self, item: WorkItem) -> None:
        """Execute a single query and set the result on the future."""
        self._querying = True
        start_ms = int(time.time() * 1000)
        stream_handler = StreamHandler()
        try:
            response_text = ""
            result_session_id: Optional[str] = None
            cost = 0.0
            num_turns = 0

            await self._sdk_client.query(item.prompt)  # type: ignore[union-attr]
            async for raw_data in self._sdk_client._query.receive_messages():  # type: ignore[union-attr]
                try:
                    message = parse_message(raw_data)
                except MessageParseError:
                    continue

                event = stream_handler.extract_content(message)
                is_partial = message.__class__.__name__ == "StreamEvent"

                if event.type == "result":
                    response_text = event.content or ""
                    result_session_id = event.session_id
                    cost = event.cost or 0.0
                elif event.type == "text" and event.content and item.on_stream:
                    await item.on_stream(event.type, event.content)
                elif event.type == "tool_use":
                    if not is_partial:
                        num_turns += 1
                    if item.on_stream:
                        await item.on_stream(
                            event.type,
                            {
                                "name": event.tool_name or "",
                                "input": event.tool_input or {},
                            },
                        )
                elif event.type == "thinking" and event.content and item.on_stream:
                    await item.on_stream(event.type, event.content)

                if isinstance(message, ResultMessage):
                    break

            duration_ms = int(time.time() * 1000) - start_ms

            if result_session_id:
                self.session_id = result_session_id

            item.future.set_result(
                QueryResult(
                    response_text=response_text,
                    session_id=result_session_id,
                    cost=cost,
                    num_turns=num_turns,
                    duration_ms=duration_ms,
                )
            )
        except Exception as e:
            if not item.future.done():
                item.future.set_exception(e)
        finally:
            self._querying = False
