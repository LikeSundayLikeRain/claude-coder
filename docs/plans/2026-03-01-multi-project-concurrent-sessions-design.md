# Multi-Project Concurrent Sessions

## Problem

Today: 1 user = 1 `UserClient` = 1 session = 1 directory. Switching projects
means stopping the current session and starting a new one. No parallel work.

## Goal

Enable concurrent Claude sessions across multiple projects using Telegram group
forum topics as the routing mechanism. Each topic = one project directory = one
independent Claude session running in parallel.

## UX Flow

### Creating a project topic

1. User creates a Telegram group, enables Topics in group settings, adds the bot
2. In the **General** topic, sends `/add ~/project-a` (or `/add` to browse)
3. Bot validates path is under `APPROVED_DIRECTORY`
4. Bot calls `createForumTopic(name="project-a")`
5. Bot stores `(chat_id, thread_id, directory)` in `project_threads` table
6. Bot sends bootstrap message in the new topic
7. User taps into topic and starts working

### Removing a project topic

- `/remove` inside a project topic — closes the topic and deletes the binding

### Working in a topic

- All messages route to that topic's bound directory
- Commands (`/new`, `/interrupt`, `/status`, `/model`) are scoped to that
  topic's project automatically
- Multiple topics can have active Claude sessions running in parallel

### Private chat (unchanged)

- Works exactly as today: single session, `/repo` to switch directories
- No breaking changes to existing workflow

### General topic

- Control plane: `/add`, `/start`, admin commands
- Regular messages rejected with guidance to use a project topic

## Data Model

### `users` table (simplified)

Strip `session_id` and `directory` — they move to `user_sessions`:

```sql
users (
    user_id            INTEGER PRIMARY KEY,
    telegram_username  TEXT
)
```

### `user_sessions` table (new)

One row per user+directory. Single source of truth for session persistence:

```sql
user_sessions (
    user_id    INTEGER NOT NULL,
    directory  TEXT NOT NULL,
    session_id TEXT,
    PRIMARY KEY (user_id, directory)
)
```

### `project_threads` table (modified)

Drop `project_slug`, add `directory`. No YAML registry — DB is source of truth:

```sql
project_threads (
    chat_id            INTEGER NOT NULL,
    message_thread_id  INTEGER NOT NULL,
    directory          TEXT NOT NULL,
    topic_name         TEXT NOT NULL,
    is_active          BOOLEAN DEFAULT TRUE,
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (chat_id, message_thread_id)
)
```

### Migration 11

1. Create `user_sessions`, migrate data from `users.session_id` + `users.directory`
2. Rebuild `users` with only `user_id` + `telegram_username`
3. Rebuild `project_threads` — drop `project_slug`, `id`, `updated_at`; add `directory`

## Architecture

### ClientManager

Re-key `_clients` from `user_id` to `(user_id, directory)`:

```python
_clients: dict[tuple[int, str], UserClient]
```

Methods updated:
- `get_or_connect(user_id, directory)` — looks up `(user_id, directory)` key
- `disconnect(user_id, directory)` — stops specific client
- `disconnect_all_for_user(user_id)` — stops all of a user's clients
- `interrupt(user_id, directory)` — interrupts specific project's query
- Session resolution from `user_sessions` table by `(user_id, directory)`

### UserClient

Zero changes. Already takes `user_id` + `directory` in constructor. Each instance
is an independent actor with its own asyncio task, queue, and SDK connection.

### Orchestrator Routing

**Group topic message:**
1. Extract `chat_id` + `message_thread_id` from update
2. Lookup `project_threads` table → get `directory`
3. Call `client_manager.get_or_connect(user_id, directory)`
4. Commands scoped to that directory automatically

**Private chat message:**
1. Resolve directory from `context.user_data` (same as today)
2. Call `client_manager.get_or_connect(user_id, directory)`
3. Single-session behavior preserved

### ProjectThreadManager (simplified)

Delete YAML registry, sync logic, `/sync_threads`. Replace with:
- `create_topic(bot, chat_id, directory)` — create topic + store binding
- `remove_topic(bot, chat_id, message_thread_id)` — close topic + delete binding
- `resolve_directory(chat_id, message_thread_id)` → `Optional[str]`

### Scoped Commands

Different command menus via `bot.set_my_commands()` with Telegram scopes:

```
Private chat:  /start /new /interrupt /status /model /repo /resume /compact
Group chat:    /add /new /interrupt /status /model /resume /compact /remove
```

`/repo` excluded from group (topics replace it).
`/add` and `/remove` excluded from private chat.

## Concurrency

Each `UserClient` is an independent actor. Two clients for the same user but
different directories share no state. The SDK spawns separate `claude` CLI
processes per client. Messages in different topics don't interleave in the UI.

Resource management: existing idle timeout (default 1 hour) naturally cleans up
inactive clients. No hard cap needed.

## What to Delete

- `src/projects/registry.py` — YAML project registry + `ProjectDefinition`
- `PROJECTS_CONFIG_PATH` config setting
- `/sync_threads` command
- `config/projects.example.yaml`
- Sync logic in `ProjectThreadManager` (replaced by create/remove/resolve)

## Design Decisions

### `/add` UX

Both modes: `/add <path>` as power-user shortcut, bare `/add` opens the
`/repo`-style inline keyboard browser. Same UX as `/repo` — navigate directories,
select button creates the topic. `/add` appears in the group command menu.

### Topic Naming

Use directory **basename** (e.g., `/home/moxu/claude-coder` → "claude-coder").

On duplicate basenames, disambiguate using the **relative path from the lowest
common ancestor** of the colliding directories:

```
/home/moxu/work/frontend/app   →  "app"
/home/moxu/personal/app        →  collision detected →
    "work/frontend/app" + "personal/app"  (LCA = /home/moxu)
```

Rename is applied at creation time only — existing topics keep their names.

### `/status` in General

Shows a dashboard of all active project sessions for the user:

```
📊 Active Sessions

claude-coder — querying (2m ago)
my-api — idle (15m ago)
frontend — disconnected
```

`/status` inside a project topic shows only that project's session detail.

### Telegram Limits

Forum supergroups support ~5000 topics. No artificial cap needed.
