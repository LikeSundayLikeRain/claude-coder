# Command UX Redesign: CLI-Aligned Session Lifecycle

**Date:** 2026-02-28
**Status:** Draft

## Problem

The current agentic command UX has several friction points:

1. **`/new` is lazy** — disconnects the old session but doesn't create a new one. The SDK only connects on the next user message, so skills/commands aren't available until the user sends a throwaway message.
2. **`/repo` auto-resumes** — selecting a directory looks up the latest session from `history.jsonl` and sets it. Users expect `/repo` to be a pure `cd` operation.
3. **`/sessions` naming** — doesn't match Claude Code CLI conventions. `claude --resume` maps better to `/resume`.
4. **`/resume <session_id>` is redundant** — the resume-bot skill handles direct session handoff via the API.

## Design

### Mental Model

The redesigned commands mirror the Claude Code CLI workflow:

| CLI equivalent | Bot command | Behavior |
|---|---|---|
| `cd /path` | `/repo /path` | Switch working directory. Disconnects active SDK session. No auto-resume. |
| `claude` | `/new` | Start fresh SDK session in current directory. Eagerly connects. |
| `claude --resume` | `/resume` | Show session picker for current directory. Selecting eagerly connects. |

Flow: **`/repo` (pick directory) -> `/new` or `/resume` (start or resume session) -> work**

### Change 1: `/repo` — Pure Directory Switch

**Current:** `_select_directory()` sets `current_directory`, saves to storage, looks up latest session via `SessionResolver.get_latest_session()`, and sets `claude_session_id`.

**New:** `_select_directory()` becomes a pure directory switch:

1. Set `context.user_data["current_directory"]` to selected path
2. Save to storage via `save_user_directory()`
3. **Remove** the `get_latest_session()` lookup
4. Clear session state (see "Session ID None Safety" below)
5. Call `client_manager.disconnect(user_id)` to stop any active SDK session
6. Reply: `"Switched to project-name/"`

The directory browser UI (inline keyboard with `nav:` / `sel:` buttons) is unchanged.

**Implicit `/new`:** If the user sends a bare message after `/repo` without doing `/new` or `/resume`, `agentic_text()` treats this as an implicit `/new` — passes `force_new=True` to `get_or_connect()`. This matches CLI behavior where typing `claude` in a directory starts a fresh session.

### Change 2: `/new` — Eager SDK Init

**Current:** Clears `claude_session_id`, sets `force_new_session = True`, disconnects old client, replies "Session reset. What's next?" SDK connects lazily on next message.

**New:**

1. Clear session state (see "Session ID None Safety" below)
2. Set `force_new_session = True`
3. Disconnect old client via `client_manager.disconnect(user_id)`
4. Resolve `current_directory` from `user_data` (fall back to first approved directory)
5. Eagerly call `client_manager.get_or_connect(user_id, directory, session_id=None, force_new=True)`
6. Store the new session ID from the connected client in `user_data["claude_session_id"]`
7. Reply: `"New session started in project-name/. Ready."`

**Error handling:** If SDK connection fails, reply `"Session reset but connection failed. Will retry on your next message."` and leave session state in a lazy-connect-ready state.

### Change 3: `/resume` — Renamed from `/sessions`, Eager Connect

**Rename:**
- Command: `/sessions` -> `/resume`
- Handler: `agentic_sessions` -> `agentic_resume`
- Old `agentic_resume` (the `/resume <session_id>` handler) is removed entirely

**Picker display changes:**
- **Session description:** Show the **first user message** of the session (truncated to fit button) instead of last message. This tells you *what* the session was about.
- **Date format:** Relative time like the CLI — `"2 min ago"`, `"3 hours ago"`, `"1 day ago"`, `"2 weeks ago"` — instead of absolute dates.
- **Cap:** 10 sessions (unchanged).

Button label format:
```
1 day ago — Add authentication to the API
```

**On session selection (`session:` callback):**
1. Set `claude_session_id` in `user_data`
2. Eagerly call `client_manager.switch_session(user_id, session_id, directory)`
3. Show transcript preview (last 3 messages) + "Session resumed. Ready."

**On "+ New Session" button:** Behaves like `/new` — eagerly connects with `force_new=True`.

**Error handling:** If `switch_session()` fails, show transcript preview anyway with note: `"Session loaded but connection failed. Will retry on your next message."`

### Change 4: Remove Old `/resume <session_id>`

- Remove handler `agentic_resume` (the old one that takes a session ID argument)
- Remove registration from `_register_agentic_handlers()`
- Remove entry from `get_bot_commands()`
- The resume-bot skill covers the direct-by-ID use case via the API `/api/resume` endpoint

### Session ID None Safety

**CAUTION:** Setting `claude_session_id` to `None` has caused bugs previously — downstream code may assume it's a string (e.g. `.replace()`, string formatting, dict keys). When clearing session state:

- Audit all consumers of `context.user_data["claude_session_id"]` for `None` safety
- Guard with `isinstance` or truthiness checks before string operations
- Consider using a sentinel empty string `""` if `None` proves too risky, but prefer `None` with proper guards as it's semantically clearer
- Key locations to audit: `_run_claude_query()`, `get_or_connect()`, `switch_session()`, session callback handler, any storage methods that persist session ID

### New Utility: `relative_time()`

A small utility function `relative_time(dt: datetime) -> str` for the session picker display:

- `"just now"` (< 1 min)
- `"2 min ago"` (< 1 hour)
- `"3 hours ago"` (< 1 day)
- `"1 day ago"` (< 1 week)
- `"2 weeks ago"` (< 1 month)
- `"3 months ago"` (>= 1 month)

## Updated Bot Commands Menu

| Command | Description |
|---|---|
| `/start` | Start the bot |
| `/new` | Start a fresh session |
| `/resume` | Choose a session to resume |
| `/interrupt` | Interrupt running query |
| `/status` | Show session status |
| `/verbose` | Set output verbosity (0/1/2) |
| `/compact` | Compress context, keep continuity |
| `/model` | Switch Claude model |
| `/repo` | List repos / switch workspace |
| `/commands` | Browse available skills |

## Files Touched

- `src/bot/orchestrator.py` — main changes (handlers, registration, command menu, callback handler)
- `src/claude/client_manager.py` — possibly minor adjustments for None-safe session handling
- New utility function for `relative_time()` (location TBD — likely `src/bot/utils.py` or similar)
- Session transcript reader — may need `from_start=True` support to read first message
