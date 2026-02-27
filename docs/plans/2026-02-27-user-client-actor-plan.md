# UserClient Actor Pattern Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Rewrite UserClient as a long-lived actor task so connect/query/disconnect all run in the same asyncio task, fixing the anyio cancel scope error.

**Architecture:** UserClient becomes an actor with an asyncio.Queue. A single worker task owns the SDK lifecycle. Callers submit work items (prompt + callback + future) via the queue. Idle timeout is built into the queue.get() call. ClientManager drops its cleanup loop.

**Tech Stack:** Python 3.12, asyncio, claude-agent-sdk, python-telegram-bot, pytest-asyncio

---

### Task 1: Write WorkItem and QueryResult dataclasses

**Files:**
- Modify: `src/claude/user_client.py`
- Test: `tests/unit/test_claude/test_user_client.py`

**Step 1: Write the test**

```python
# tests/unit/test_claude/test_user_client.py
import asyncio
import pytest
from src.claude.user_client import WorkItem, QueryResult


class TestWorkItem:
    def test_work_item_creation(self):
        future = asyncio.get_event_loop().create_future()
        item = WorkItem(prompt="hello", future=future)
        assert item.prompt == "hello"
        assert item.on_stream is None
        assert item.future is future

    def test_work_item_with_callback(self):
        future = asyncio.get_event_loop().create_future()

        async def cb(x):
            pass

        item = WorkItem(prompt="hi", on_stream=cb, future=future)
        assert item.on_stream is cb


class TestQueryResult:
    def test_query_result_creation(self):
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
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_claude/test_user_client.py::TestWorkItem -v`
Expected: FAIL — `WorkItem` not found

**Step 3: Write minimal implementation**

Add to the top of `src/claude/user_client.py`:

```python
import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Optional

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
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_claude/test_user_client.py::TestWorkItem::test_work_item_creation tests/unit/test_claude/test_user_client.py::TestQueryResult -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/claude/user_client.py tests/unit/test_claude/test_user_client.py
git commit -m "feat: add WorkItem and QueryResult dataclasses for actor pattern"
```

---

### Task 2: Rewrite UserClient as actor

**Files:**
- Modify: `src/claude/user_client.py` (full rewrite)
- Test: `tests/unit/test_claude/test_user_client.py`

**Step 1: Write the tests**

```python
class TestUserClientActor:
    """Tests for the actor-based UserClient."""

    @pytest.fixture
    def mock_options(self):
        return MagicMock(spec=ClaudeAgentOptions)

    @pytest.fixture
    def client(self):
        return UserClient(
            user_id=123,
            directory="/test",
            session_id="sess-1",
            idle_timeout=5,
        )

    @pytest.mark.asyncio
    async def test_start_sets_connected(self, client, mock_options):
        """start() should spawn worker and set is_connected."""
        with patch.object(client, "_worker", new_callable=AsyncMock):
            await client.start(mock_options)
            assert client.is_connected
            await client.stop()

    @pytest.mark.asyncio
    async def test_stop_disconnects(self, client, mock_options):
        """stop() should cleanly shut down the worker."""
        with patch.object(client, "_worker", new_callable=AsyncMock):
            await client.start(mock_options)
            await client.stop()
            assert not client.is_connected

    @pytest.mark.asyncio
    async def test_submit_returns_result(self, client):
        """submit() should return the result from the worker."""
        # Simulate a running actor by manually setting state
        client._running = True
        client._queue = asyncio.Queue()

        # Create a fake worker that completes the future
        async def fake_consumer():
            item = await client._queue.get()
            item.future.set_result(
                QueryResult(response_text="hello", session_id="s1")
            )

        task = asyncio.create_task(fake_consumer())
        result = await client.submit("test prompt")
        assert result.response_text == "hello"
        await task

    @pytest.mark.asyncio
    async def test_submit_when_not_running_raises(self, client):
        """submit() should raise if actor is not running."""
        with pytest.raises(RuntimeError, match="not running"):
            await client.submit("hello")

    @pytest.mark.asyncio
    async def test_idle_timeout_stops_actor(self):
        """Worker should exit when queue.get times out."""
        client = UserClient(
            user_id=123,
            directory="/test",
            idle_timeout=0.1,  # 100ms for fast test
        )
        # Mock SDK client to avoid real subprocess
        mock_sdk = AsyncMock()
        with patch("src.claude.user_client.ClaudeSDKClient", return_value=mock_sdk):
            await client.start(MagicMock())
            # Don't submit anything — let it idle timeout
            await asyncio.sleep(0.3)
            assert not client.is_connected
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_claude/test_user_client.py::TestUserClientActor -v`
Expected: FAIL — `start()`, `submit()`, `stop()` don't exist yet

**Step 3: Rewrite UserClient**

Replace the `UserClient` class in `src/claude/user_client.py`:

```python
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
        idle_timeout: int = 3600,
        on_exit: Optional[Callable[[int], Any]] = None,
    ) -> None:
        self.user_id = user_id
        self.directory = directory
        self.session_id = session_id
        self.model = model
        self.betas = betas
        self.idle_timeout = idle_timeout
        self._on_exit = on_exit  # callback to remove self from ClientManager

        self._queue: asyncio.Queue[Optional[WorkItem]] = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task[None]] = None
        self._running = False
        self._querying = False
        self._sdk_client: Optional[ClaudeSDKClient] = None

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
        self._worker_task = asyncio.create_task(self._worker())
        # Wait for connect to complete
        await self._connected_event.wait()

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
        future: asyncio.Future[QueryResult] = asyncio.get_event_loop().create_future()
        await self._queue.put(WorkItem(prompt=prompt, on_stream=on_stream, future=future))
        return await future

    async def interrupt(self) -> None:
        """Interrupt the current query if one is running."""
        if self._querying and self._sdk_client is not None:
            await self._sdk_client.interrupt()
            logger.info("query_interrupted", user_id=self.user_id)

    async def _worker(self) -> None:
        """Long-lived task: connect, process queue, disconnect."""
        self._connected_event = asyncio.Event()
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

            await self._sdk_client.query(item.prompt)
            async for raw_data in self._sdk_client._query.receive_messages():
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
                    await item.on_stream(
                        StreamUpdate(type="assistant", content=event.content)
                    )
                elif event.type == "tool_use":
                    if not is_partial:
                        num_turns += 1
                    if item.on_stream:
                        await item.on_stream(
                            StreamUpdate(
                                type="assistant",
                                tool_calls=[
                                    {
                                        "name": event.tool_name or "",
                                        "input": event.tool_input or {},
                                    }
                                ],
                            )
                        )
                elif event.type == "thinking" and event.content and item.on_stream:
                    await item.on_stream(
                        StreamUpdate(
                            type="assistant",
                            content=f"\U0001f4ad {event.content}",
                        )
                    )

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
```

**Step 4: Run tests**

Run: `uv run pytest tests/unit/test_claude/test_user_client.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/claude/user_client.py tests/unit/test_claude/test_user_client.py
git commit -m "feat: rewrite UserClient as actor with queue-based lifecycle"
```

---

### Task 3: Update ClientManager to use actor API

**Files:**
- Modify: `src/claude/client_manager.py`
- Test: `tests/unit/test_claude/test_client_manager.py`

**Step 1: Update ClientManager**

Key changes:
- `get_or_connect()`: call `client.start(options)` instead of `client.connect(options)`. Pass `on_exit=self._on_client_exit` callback so actors can self-remove.
- `disconnect()`: call `client.stop()` instead of `client.disconnect()`.
- `disconnect_all()`: call `client.stop()` for each client.
- Remove `start_cleanup_loop()`, `stop_cleanup_loop()`, `_cleanup_loop()`, `_cleanup_idle()` — actors manage their own timeout.
- Add `_on_client_exit(user_id)` method that removes the client from `_clients`.
- Pass `idle_timeout` through to UserClient constructor.

```python
class ClientManager:
    def __init__(
        self,
        bot_session_repo: BotSessionRepository,
        options_builder: Optional[OptionsBuilder] = None,
        history_path: Optional[Path] = None,
        idle_timeout: int = DEFAULT_IDLE_TIMEOUT_SECONDS,
    ) -> None:
        self._bot_session_repo = bot_session_repo
        self._options_builder = options_builder or OptionsBuilder()
        self._session_resolver = SessionResolver(history_path=history_path)
        self._idle_timeout = idle_timeout
        self._clients: dict[int, UserClient] = {}

    async def get_or_connect(self, ...) -> UserClient:
        # ... same resolution logic ...

        client = UserClient(
            user_id=user_id,
            directory=directory,
            session_id=resolved_session_id,
            model=resolved_model,
            idle_timeout=self._idle_timeout,
            on_exit=self._on_client_exit,
        )
        options = self._options_builder.build(...)
        await client.start(options)
        self._clients[user_id] = client
        # ... persist session ...
        return client

    async def disconnect(self, user_id: int) -> None:
        client = self._clients.pop(user_id, None)
        if client is not None:
            await client.stop()

    async def disconnect_all(self) -> None:
        user_ids = list(self._clients.keys())
        for user_id in user_ids:
            await self.disconnect(user_id)

    def _on_client_exit(self, user_id: int) -> None:
        """Called by actor when it exits (idle timeout or error)."""
        self._clients.pop(user_id, None)
```

Remove entirely: `start_cleanup_loop()`, `stop_cleanup_loop()`, `_cleanup_loop()`, `_cleanup_idle()`.

**Step 2: Update tests**

Update `tests/unit/test_claude/test_client_manager.py`:
- Replace all `client.connect()` expectations with `client.start()`
- Replace all `client.disconnect()` expectations with `client.stop()`
- Remove `TestClientManagerCleanup` class (cleanup loop tests)
- Add test for `_on_client_exit` removing client from dict
- Mock `UserClient` to return an actor-style mock (with `start`, `stop`, `submit`)

**Step 3: Run tests**

Run: `uv run pytest tests/unit/test_claude/test_client_manager.py -v`
Expected: PASS

**Step 4: Commit**

```bash
git add src/claude/client_manager.py tests/unit/test_claude/test_client_manager.py
git commit -m "feat: update ClientManager for actor-based UserClient"
```

---

### Task 4: Update orchestrator to use submit()

**Files:**
- Modify: `src/bot/orchestrator.py:904-1007`
- Test: `tests/unit/test_orchestrator.py`

**Step 1: Simplify `_run_claude_query`**

The stream handling and message parsing now live inside `UserClient._process_item()`. The orchestrator just calls `submit()` and gets back a `QueryResult`:

```python
async def _run_claude_query(
    self,
    prompt: str,
    user_id: int,
    current_dir: Any,
    session_id: Optional[str],
    force_new: bool,
    on_stream: Optional[Callable[[StreamUpdate], Any]],
    context: ContextTypes.DEFAULT_TYPE,
) -> "ClaudeResponse":
    from ..claude.sdk_integration import ClaudeResponse

    client_manager: Optional[ClientManager] = context.bot_data.get("client_manager")
    if client_manager is None:
        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            raise RuntimeError(
                "Neither client_manager nor claude_integration available"
            )
        return await claude_integration.run_command(
            prompt=prompt,
            working_directory=current_dir,
            user_id=user_id,
            session_id=session_id,
            on_stream=on_stream,
            force_new=force_new,
        )

    directory = str(current_dir)
    approved_dir = str(self.settings.approved_directory)

    client = await client_manager.get_or_connect(
        user_id=user_id,
        directory=directory,
        session_id=None if force_new else session_id,
        approved_directory=approved_dir,
        force_new=force_new,
    )

    result = await client.submit(prompt, on_stream=on_stream)

    if result.session_id:
        await client_manager.update_session_id(user_id, result.session_id)

    return ClaudeResponse(
        content=result.response_text,
        session_id=result.session_id or "",
        cost=result.cost,
        duration_ms=result.duration_ms,
        num_turns=result.num_turns,
    )
```

This removes ~40 lines of stream handling from the orchestrator (now in UserClient).

**Step 2: Remove StreamHandler import from orchestrator**

The orchestrator no longer needs `StreamHandler` — remove `from ..claude.stream_handler import StreamHandler` if no longer used elsewhere in the file.

**Step 3: Update tests**

Update `tests/unit/test_orchestrator.py` and `tests/unit/test_bot/test_orchestrator_integration.py` to mock `client.submit()` returning a `QueryResult` instead of `client.query()` returning an async iterator.

**Step 4: Run tests**

Run: `uv run pytest tests/unit/test_orchestrator.py tests/unit/test_bot/test_orchestrator_integration.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/bot/orchestrator.py tests/unit/test_orchestrator.py tests/unit/test_bot/test_orchestrator_integration.py
git commit -m "refactor: simplify orchestrator to use actor submit() API"
```

---

### Task 5: Update main.py startup/shutdown

**Files:**
- Modify: `src/main.py:148-151,280,366-367`

**Step 1: Remove cleanup loop calls**

In `src/main.py`:
- Line 280: Remove `client_manager.start_cleanup_loop()`
- Line 366: Remove `client_manager.stop_cleanup_loop()`
- Line 367: Keep `await client_manager.disconnect_all()` (now calls `stop()` internally)

**Step 2: Pass idle_timeout from config**

```python
client_manager = ClientManager(
    bot_session_repo=storage.bot_sessions,
    options_builder=options_builder,
    idle_timeout=config.session_timeout_hours * 3600,
)
```

**Step 3: Run full test suite**

Run: `uv run pytest tests/ -q --tb=short`
Expected: All tests pass

**Step 4: Commit**

```bash
git add src/main.py
git commit -m "chore: remove cleanup loop, actors manage own idle timeout"
```

---

### Task 6: Update CLAUDE.md and verify end-to-end

**Files:**
- Modify: `CLAUDE.md`

**Step 1: Update architecture docs**

Update the Claude SDK Integration section to mention the actor pattern:
- `UserClient` is now described as an actor with a message queue
- Remove mention of cleanup loop from ClientManager description
- Note idle timeout is built into the actor

**Step 2: Run full test suite + lint**

```bash
uv run pytest tests/ -q --tb=short
make lint
```

Expected: All tests pass, no lint errors.

**Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md for actor-based UserClient"
```
