# UserClient Actor Pattern Design

## Context

The Claude SDK uses anyio cancel scopes internally which are bound to the task that called `connect()`. In a Telegram bot, every message is handled as a separate asyncio task. This means `disconnect()` is always called from a different task than `connect()`, causing: "Attempted to exit cancel scope in a different task than it was entered in."

The SDK documents this as a known limitation: "You must complete all operations within the same async context where it was connected."

## Approach

Rewrite `UserClient` as an actor — a long-lived asyncio task with a message queue. One task per user owns the full `connect → query → ... → query → disconnect` lifecycle, aligning with the SDK's expected usage.

## Architecture

```
Telegram handler (ephemeral task)     UserClient actor (long-lived task)
─────────────────────────────────     ────────────────────────────────────
                                      start() → spawns worker task
                                        connect()
user sends message ──→ submit()
  queue.put(WorkItem) ────────────→     queue.get(timeout=idle_timeout)
                                        await query(prompt, on_stream)
                   ←── await future ←── future.set_result(response)

user sends /new    ──→ stop()
  queue.put(STOP)  ────────────→        break loop
                                        disconnect()  ← same task as connect ✓
                                        task exits

idle timeout                            queue.get() times out
                                        disconnect()  ← same task as connect ✓
                                        task exits
```

## Components

### WorkItem

A dataclass holding everything the worker needs for one query:
- `prompt: str`
- `on_stream: Optional[Callable]` — streaming callback for progress updates
- `future: asyncio.Future` — for returning the result to the caller

### UserClient (rewritten)

Public API:
- `start(options)` — spawns the worker task, calls `connect()` inside it
- `submit(prompt, on_stream) → Future` — enqueues a WorkItem, returns a future
- `stop()` — enqueues a sentinel, waits for worker task to finish
- `interrupt()` — interrupts current query (delegates to SDK)

Internal:
- `_worker()` — the long-lived task: connect, loop on queue.get with timeout, disconnect
- `_queue: asyncio.Queue[WorkItem | None]` — None is the stop sentinel

Properties (unchanged):
- `is_connected`, `is_querying`, `session_id`, `directory`, `model`, etc.

### ClientManager changes

- `get_or_connect()` → calls `client.start(options)` instead of `client.connect(options)`
- `_run_claude_query()` in orchestrator → calls `await client.submit(prompt, on_stream)` instead of `async for msg in client.query(prompt)`
- Remove `start_cleanup_loop()` / `stop_cleanup_loop()` / `_cleanup_loop()` / `_cleanup_idle()` — actors manage their own timeout
- `disconnect()` → calls `client.stop()` instead of `client.disconnect()`

### Streaming

The `on_stream` callback is passed as part of the WorkItem. The worker calls it from inside the query loop. `progress_msg.edit_text()` is asyncio-safe across tasks (plain HTTP call, no cancel scopes), so this works.

The key change: the orchestrator no longer iterates `async for message in client.query()` directly. Instead, the worker iterates the stream, calls `on_stream` for progress, and collects the final result to set on the future.

### Error handling

- Query exceptions are caught in the worker and set on the future via `future.set_exception(e)`
- The caller awaits the future and gets the exception re-raised naturally
- If the worker task crashes unexpectedly, `is_connected` returns False and the next `get_or_connect` starts a fresh actor

### Idle timeout

- Built into the worker loop: `queue.get(timeout=idle_timeout)`
- On timeout → disconnect, exit task, remove self from ClientManager
- No separate cleanup loop needed
- The actor needs a reference to ClientManager (or a cleanup callback) to remove itself from `_clients` on exit

## What stays the same

- `OptionsBuilder`, `StreamHandler`, `SessionResolver` — unchanged
- `ClaudeResponse` dataclass — unchanged
- `BotSessionRepository` persistence — unchanged
- `ClientManager` public API shape — mostly the same, callers don't change much
- All existing tests for non-UserClient components — unchanged

## What changes

- `src/claude/user_client.py` — full rewrite (actor pattern)
- `src/claude/client_manager.py` — remove cleanup loop, adapt to start/submit/stop API
- `src/bot/orchestrator.py` — `_run_claude_query` uses submit() instead of async iteration
- `tests/unit/test_claude/test_user_client.py` — rewrite for new API
- `tests/unit/test_claude/test_client_manager.py` — remove cleanup loop tests, adapt
