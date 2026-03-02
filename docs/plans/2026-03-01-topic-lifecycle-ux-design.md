# Topic Lifecycle UX Design

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Improve group topic UX with visual idle/active states, history replay on resume, `/history` command, and destructive `/remove` with confirmation.

**Architecture:** Extends the existing `ProjectThreadManager` and `ClientManager` with lifecycle hooks (close/reopen), a transcript reader for JSONL history, and condensed message formatting.

**Tech Stack:** python-telegram-bot (forum topic APIs), aiosqlite, Claude CLI transcript JSONL files.

---

## Decisions

| # | Decision | Choice |
|---|----------|--------|
| 1 | Idle timeout visual | Close topic (`close_forum_topic`) — greyed out, read-only |
| 2 | Resume from idle | `reopen_forum_topic` + SDK reconnect on next message |
| 3 | `/remove` behavior | `delete_forum_topic` (permanent) with double-confirm |
| 4 | History format | Condensed single message, split if >4000 chars |
| 5 | History auto-replay | Only on fresh topic resuming existing session (no prior messages in topic) |
| 6 | History source | Claude CLI JSONL transcripts (`~/.claude/projects/<slug>/<session>.jsonl`) |
| 7 | `/history` command | On-demand transcript retrieval, supports `/history N` for last N exchanges |

---

## Feature 1: Topic Idle Close/Reopen

### Lifecycle States

| Action | Telegram API | DB `is_active` | Reconnectable? |
|--------|-------------|----------------|----------------|
| Topic created (`/add`) | `create_forum_topic` | `True` | Yes |
| Idle timeout (1h) | `close_forum_topic` | `True` | Yes |
| User sends message to closed topic | `reopen_forum_topic` | `True` | Yes |
| `/remove` (confirmed) | `delete_forum_topic` | `False` | No |
| Bot restart | No API call | `True` | Yes (lazy reconnect) |

### Implementation

**On idle timeout** (`UserClient.on_exit` callback):

1. `ClientManager._make_on_exit()` gains access to a `TopicLifecycleManager` reference
2. On exit, if `chat_id != user_id` (i.e., not a private DM):
   - Send message: "Session disconnected (idle). Send a message to reconnect."
   - Call `bot.close_forum_topic(chat_id, message_thread_id)`
3. The `on_exit` callback currently runs synchronously. Since we need async Telegram API calls, the callback should schedule a coroutine via `asyncio.create_task()` or the lifecycle manager should expose a sync-safe scheduling method.

**On resume** (user sends message to closed topic):

1. `MessageOrchestrator._execute_query()` already calls `get_or_connect()` which reconnects
2. Before reconnecting, call `bot.reopen_forum_topic(chat_id, message_thread_id)`
3. If `reopen_forum_topic` raises `TelegramError` (topic wasn't closed), ignore silently

**Edge cases:**
- Private DMs (`message_thread_id=0`): No topic lifecycle. Idle just disconnects SDK silently.
- Bot restart: Topics may be closed in Telegram but DB says `is_active=True`. On first message, bot reopens and reconnects — seamless.
- Rapid idle/resume: Close fires after timeout, user sends message during close API call. `reopen_forum_topic` handles this gracefully.

### Where it lives

- `src/projects/lifecycle.py` — new `TopicLifecycleManager` class
- `src/claude/client_manager.py` — `_make_on_exit` enhanced with lifecycle hook
- `src/bot/orchestrator.py` — `_execute_query` calls reopen before reconnect

---

## Feature 2: History Replay on Resume

### Trigger Conditions

History replay fires when ALL of:
1. Topic is newly created (first message in this topic — no prior bot messages)
2. Session has an existing `session_id` (resumed from CLI or `/resume`)
3. Transcript file exists at `~/.claude/projects/<slug>/<session-id>.jsonl`

History replay does NOT fire when:
- Topic already has messages (idle reopen — user has context)
- Session is brand new (`/new`, `/add` without resume)
- No transcript file found

### Data Source

Claude CLI stores transcripts at:
```
~/.claude/projects/<project-slug>/<session-id>.jsonl
```

Each line is a JSON object. Relevant message types:
- `type: "human"` with `message.content[].text` — user messages
- `type: "assistant"` with `message.content[].text` — assistant text responses
- `type: "assistant"` with `message.content[].type == "tool_use"` — tool calls

### Format

Condensed single message with all exchanges:

```
📜 Session history (5 exchanges):
━━━━━━━━━━━━━━━━━━━━━━

👤 Fix the login bug in auth.py

🤖 Found the issue in auth.py:45 — token expiry
check used < instead of <=. Fixed and tested.
[used Edit on src/auth.py]

👤 Add a test for that

🤖 Added test_token_expiry_boundary in
test_auth.py. All 12 tests pass.

━━━━━━━━━━━━━━━━━━━━━━
Session resumed. Send a message to continue.
```

**Formatting rules:**
- User messages: `👤 <text>` (truncate at 200 chars with `...`)
- Assistant text: `🤖 <text>` (truncate at 500 chars with `...`)
- Tool calls: collapsed to `[used <ToolName> on <file>]` one-liner
- Tool results: omitted (too verbose)
- Thinking blocks: omitted

**Splitting:** If formatted history exceeds 4000 chars, split into multiple messages (oldest exchanges first). Each message is self-contained with its own header showing the range.

### Where it lives

- `src/claude/transcript.py` — new module: `TranscriptReader` class
  - `read_transcript(session_id, project_dir) -> list[TranscriptEntry]`
  - `format_condensed(entries, max_chars=4000) -> list[str]`
- `src/bot/orchestrator.py` — `_execute_query` checks for fresh-topic + existing session

---

## Feature 3: `/remove` with Confirmation

### Flow

1. User sends `/remove` in a topic
2. Bot replies: "⚠️ This will permanently delete this topic and all messages. Send `/remove` again to confirm."
3. Bot sets `context.chat_data["pending_remove"] = True`
4. If user sends `/remove` again within same topic:
   - Disconnect `UserClient` via `ClientManager.disconnect()`
   - Deactivate DB row via `repository.deactivate()`
   - Delete topic via `bot.delete_forum_topic(chat_id, message_thread_id)`
5. If user sends anything else: flag cleared, no deletion
6. Flag auto-expires (not persisted across bot restarts)

### Edge cases
- User sends `/remove` in General topic: "Use /remove inside the topic you want to delete."
- User sends `/remove` in private DM: "Topics are only available in group chats."
- `delete_forum_topic` fails: fall back to `close_forum_topic`, inform user

---

## Feature 4: `/history` Command

### Usage

```
/history      — show full transcript
/history 5    — show last 5 exchanges
```

### Behavior

1. Resolve `session_id` from DB for current `(chat_id, message_thread_id)`
2. Resolve `directory` to find the project slug
3. Read transcript from `~/.claude/projects/<slug>/<session-id>.jsonl`
4. Format using same condensed format as auto-replay
5. Send to topic (split across messages if needed)

### Edge cases
- No session: "No active session in this topic."
- No transcript file: "No transcript available for session `<id>`."
- Empty transcript: "Session has no conversation history yet."
- Very long transcript (100+ exchanges): warn "Loading full history..." then send

### Where it lives
- `src/bot/orchestrator.py` — new `agentic_history` handler
- Reuses `TranscriptReader` from Feature 2

---

## Components Summary

| Component | File | New/Modified |
|-----------|------|-------------|
| `TopicLifecycleManager` | `src/projects/lifecycle.py` | New |
| `TranscriptReader` | `src/claude/transcript.py` | New |
| `ClientManager` | `src/claude/client_manager.py` | Modified (on_exit hook) |
| `ProjectThreadManager` | `src/projects/thread_manager.py` | Modified (delete_topic) |
| `MessageOrchestrator` | `src/bot/orchestrator.py` | Modified (reopen, history, remove confirm) |
| `main.py` | `src/main.py` | Modified (wire lifecycle manager) |
