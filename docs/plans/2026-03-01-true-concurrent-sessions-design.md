# True Concurrent Sessions Design (WIP)

**Goal:** Each Telegram forum topic acts as an independent terminal panel with its own Claude session. Multiple topics can target the same directory for true concurrency.

**Status:** Design finalized. Ready for implementation.

---

## Key Decisions

### 1. Topic = Session (1:1 mapping)

Each forum topic IS a session. The `message_thread_id` (stable, server-assigned integer) uniquely identifies a session within a chat.

- `/add /path/to/project` twice → two topics, two independent sessions, same directory
- No separate session abstraction needed — the topic is the session

### 2. ClientManager key: `(user_id, chat_id, message_thread_id)`

- `message_thread_id` is unique within a chat but NOT globally (two groups could both have topic `5`)
- `chat_id` disambiguates across groups
- `user_id` for auth/ownership
- For private DM: `chat_id = user_id`, `message_thread_id = 0`

### 3. Unified `chat_sessions` table

Merge `user_sessions` + `project_threads` + effectively replace `users`:

```sql
chat_sessions:
  chat_id            INTEGER NOT NULL,
  message_thread_id  INTEGER NOT NULL DEFAULT 0,
  user_id            INTEGER NOT NULL,
  directory          TEXT NOT NULL,
  session_id         TEXT,          -- Claude SDK session_id, nullable until first query
  topic_name         TEXT,          -- NULL for private chat
  is_active          BOOLEAN DEFAULT TRUE,
  created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (chat_id, message_thread_id)
```

| Context | chat_id | message_thread_id | topic_name |
|---------|---------|-------------------|------------|
| Private DM | user_id | 0 | NULL |
| Group General | group_id | 0 | NULL |
| Group topic | group_id | topic_id | "my-app" |

### 4. Drop `users` table

Analysis of current usage:
- `telegram_username` — stored via `ensure_user()` but **never read back** anywhere
- `save_user_directory` — referenced in orchestrator but method doesn't exist on facade (dead code)
- FK constraints from `sessions`/`messages`/`tool_usage`/`audit_log` — referential integrity only, not functional

**Action:** Drop `users` table entirely. Remove FK constraints. `user_id` is a plain column in other tables. `UserRepository` and `UserModel` deleted.

### 5. Private chat: single session (unchanged)

- Private chat = one row in `chat_sessions` with `message_thread_id = 0`
- No topic separation possible (BotFather threaded mode not available for private bots)
- Group chat is the path to concurrency

### 6. Bidirectional lookup

- **Forward** (message routing): `WHERE chat_id=X AND message_thread_id=Y` → get session
- **Reverse** (control plane): `WHERE user_id=X` → list all sessions with their topics
- **Resume from General**: `/status` shows all sessions, user picks one, bot routes to the topic

---

## Telegram Forum Topics Lifecycle Reference

```
CREATE:   bot.create_forum_topic(chat_id, name, icon_color?, icon_custom_emoji_id?)
          → ForumTopic(message_thread_id, name, ...)
          Requires: can_manage_topics

SEND:     bot.send_message(chat_id, text, message_thread_id=topic_id)

EDIT:     bot.edit_forum_topic(chat_id, thread_id, name?, icon_custom_emoji_id?)
          Cannot change icon_color after creation.

CLOSE:    bot.close_forum_topic(chat_id, thread_id)
          Prevents non-admin messages. Topic remains visible but grayed out.

REOPEN:   bot.reopen_forum_topic(chat_id, thread_id)

DELETE:   bot.delete_forum_topic(chat_id, thread_id)
          PERMANENTLY destroys topic AND all messages. Irreversible.
          Requires: can_delete_messages (different permission!)
```

**Gotchas:**
- No `getForumTopics` API — cannot enumerate existing topics, must track in DB
- `message_thread_id` is stable across bot restarts
- General topic = `message_thread_id` absent/None (not 0)
- `delete` requires `can_delete_messages`, not `can_manage_topics`
- Current code correctly uses `close` (soft) not `delete` (hard) for `/remove`

### 7. Session resume: DB is source of truth

Claude SDK's `history.jsonl` keys sessions by directory. With multiple topics for the same directory, auto-resolve from history would conflict. Instead:

- **Always use the stored `session_id` from `chat_sessions`** — each topic resumes its own session
- Ignore `history.jsonl` auto-resolve for group topics
- Private DM can still use history-based auto-resolve as fallback

### 8. Topic naming: auto-suffix with optional override

When `/add /path/to/myapp` is used for a directory that already has a topic:

- Default: auto-suffix — `myapp`, `myapp (2)`, `myapp (3)`
- Optional: `/add /path/to/myapp "bug fix"` — second arg overrides topic name
- **Future:** Claude helps name the topic based on the task description

### 9. `/remove` disconnects the UserClient

When a topic is removed:
1. Close the forum topic (soft, preserves history)
2. Disconnect the associated `UserClient` (stop the actor, release resources)
3. Deactivate the `chat_sessions` row (`is_active = 0`)

### 10. No max session limit

No explicit cap on concurrent sessions per user. Resource-bounded naturally by idle timeout — inactive UserClients self-remove.
