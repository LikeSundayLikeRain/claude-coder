# /start Wizard UX Redesign

**Date:** 2026-03-01
**Status:** Approved

## Summary

Replace `/add` with a unified `/start` wizard in supergroups. Each topic binds to exactly one session (1:1). Auto-name topics via Haiku after ~3 exchanges.

## Command Changes (Supergroup)

| Command | Where | Behavior |
|---------|-------|----------|
| `/start` | General | Wizard: dir picker -> session picker -> create topic |
| `/start` | Topic | Welcome/status (unchanged) |
| `/new` | Topic | Creates new topic for same dir with fresh session |
| `/remove` | Topic | Double-confirm -> delete (unchanged) |
| `/history` | Topic | Transcript display (unchanged) |
| `/resume` | Topic | **Removed** (session fixed per topic) |
| `/add` | — | **Removed** (replaced by `/start`) |

Private DMs: `/start` keeps current welcome message behavior.

## Wizard Flow

```
/start (General topic)
  -> Directory browser (inline keyboard)
  -> User selects directory
  -> Session picker: existing sessions for dir + "New Session"
  -> User picks session or "New"
  -> Topic created with session bound, eagerly connected
```

## Topic = Session (1:1)

Each forum topic is bound to exactly one session. Same directory can have multiple topics (different sessions). The `chat_sessions` row gets its `session_id` at creation time (not on first message).

## Topic Auto-naming

1. **Fresh session**: `dir-name — a3f2b1c8` (session ID snippet)
2. **After ~3 user messages**: Haiku generates descriptive name -> `edit_forum_topic`
3. **Resumed existing session**: Haiku generates from transcript if not yet named

## /new in Topic

`/new` inside a topic creates a **new topic** for the same directory with a fresh session. The old topic stays for reference. Maintains the 1:1 model.

## Callback Patterns

Old: `add_nav:`, `add_sel:` (for /add browser)
New: `start_nav:`, `start_sel:`, `start_session:` (for /start wizard)

The `session:` callback pattern (existing) is reused for session selection, but now also triggers topic creation when used from General.

## Components Affected

- `orchestrator.py`: Replace `agentic_add` with `agentic_start` wizard, update `agentic_new` for topic creation, remove `/resume` from topics
- `thread_manager.py`: `create_topic` updated to accept `session_id` at creation
- `lifecycle.py`: Topic auto-naming via Haiku after message count threshold
- `client_manager.py`: Track message count per topic for auto-naming trigger
- `_agentic_callback`: Replace `add_nav:`/`add_sel:` with `start_nav:`/`start_sel:`/`start_session:`
- `get_bot_commands()`: Update command lists
- `_inject_deps`: Update management bypass list
