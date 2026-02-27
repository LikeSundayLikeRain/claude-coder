# Thin Client Session Management

Date: 2026-02-25

## Problem

The bot maintains a separate SQLite `sessions` table to track Claude SDK sessions.
When a user picks a CLI-initiated session from `/sessions`, the resume fails because
the CLI session ID doesn't exist in the bot's SQLite database. The bot falls through
to creating a brand new session instead of resuming.

Root cause in `SessionManager.get_or_create_session()`: when a session_id is not
found in SQLite, it creates a new session with `session_id=""` and
`is_new_session=True`, which causes `facade.run_command()` to call the SDK with
`session_id=None` and `continue_session=False`.

## Goal

Seamlessly continue a CLI dev session from mobile (and vice versa). The machine's
Claude CLI data is the single source of truth.

## Design

The bot becomes a stateless pass-through for sessions. No SQLite session tracking.

### Source of truth

- `~/.claude/history.jsonl` — session index (session_id, display, timestamp, project)
- `~/.claude/projects/<slug>/<session-id>.jsonl` — session transcripts
- Claude SDK `options.resume` — actual session resume mechanism

### Session flow

```
User sends message
  -> Read context.user_data["claude_session_id"]
  -> If present: pass to SDK as options.resume
  -> If absent and not force_new: auto-resume from history.jsonl
  -> If absent and force_new: let SDK create new session
  -> After response: write to history.jsonl, store session_id in user_data

/sessions
  -> Read history.jsonl, filter by directory, show picker
  -> On select: set user_data["claude_session_id"]
  -> Next message resumes that session via SDK

Auto-resume (implicit)
  -> Read history.jsonl, filter by current directory
  -> Pick most recent entry's session_id
  -> Pass to SDK
```

### Resume failure recovery

If SDK resume fails (session expired/missing on Claude's side), catch the error,
clear the session_id, and retry as a fresh session. Write the new session to
history.jsonl.

### Files to delete

- `src/storage/session_storage.py` — entire file (SQLiteSessionStorage)
- `src/claude/session.py` — entire file (SessionManager, ClaudeSession, all storage classes)

### Files to simplify

- `src/claude/facade.py` — remove SessionManager dependency; run_command accepts
  session_id directly, passes to SDK, writes history on success. Auto-resume reads
  history.jsonl instead of SQLite.
- `src/storage/models.py` — remove SessionModel
- `src/storage/database.py` — add migration 8: DROP TABLE sessions
- `src/bot/orchestrator.py` — remove session update/persist logic; just set
  user_data["claude_session_id"] from SDK response
- `src/main.py` — remove SessionManager and SQLiteSessionStorage wiring

### Files unchanged

- `src/claude/history.py` — already reads/writes/filters history.jsonl correctly
- `src/bot/orchestrator.py` /sessions UI — already reads from history.jsonl
- `src/bot/orchestrator.py` _agentic_callback session picker — already sets user_data

### DB tables kept

- `users` (current_directory persistence, auth)
- `audit_log` (security events)
- `scheduled_jobs` (cron agent tasks)
- `webhook_events` (webhook dedup)
- `project_threads` (forum topic routing)

### Facade API (after)

```python
class ClaudeIntegration:
    def __init__(self, config, sdk_manager):
        # No session_manager dependency

    async def run_command(self, prompt, working_directory, user_id,
                          session_id=None, on_stream=None,
                          force_new=False) -> ClaudeResponse:
        # Auto-resume: if no session_id and not force_new,
        #   read history.jsonl -> filter by directory -> pick most recent
        # Pass session_id to SDK as options.resume
        # On success: append to history.jsonl
        # On resume failure: retry as fresh session

    def find_resumable_session_id(self, directory) -> Optional[str]:
        # Read history.jsonl, filter by directory, return most recent session_id
```
