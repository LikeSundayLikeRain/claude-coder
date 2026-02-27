# Claude Integration Layer Rewrite — Design

**Date:** 2026-02-26
**Status:** Approved
**Scope:** Rewrite `src/claude/` to achieve CLI feature parity via persistent SDK client

## Goal

Seamless device switching between Claude CLI on EC2 and Telegram bot on mobile. The bot becomes a thin client over the CLI's local storage, sharing the same sessions, config, and plugins.

## Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Rewrite scope | `src/claude/` only | Keep bot infrastructure (auth, Telegram, storage, events) |
| Client architecture | Persistent client per user | Enables interrupt, model switching, approval flow |
| Multi-session | One active, fast switch | Low resource, ~1s reconnect on switch |
| Session source of truth | `~/.claude/history.jsonl` | Shared with CLI — bidirectional resume works out of the box |
| Config source of truth | `~/.claude/settings.json` | Read CLI settings, don't duplicate in bot env vars |
| Permission mode | `bypassPermissions` default | Headless — no interactive terminal. Telegram approval as stretch goal |
| Bot state persistence | SQLite `bot_sessions` table | Survive bot restarts, consistent with existing storage |

## Architecture

### Module Structure

```
src/claude/
├── client_manager.py    # Owns persistent ClaudeSDKClient instances
├── options.py           # Builds ClaudeAgentOptions from CLI config
├── session.py           # Reads history.jsonl, resolves session IDs
├── stream_handler.py    # Processes SDK message stream → Telegram output
└── exceptions.py        # (keep existing)
```

### ClientManager

Core component — maintains one long-lived `ClaudeSDKClient` per user.

```
┌─────────────────────────────────────────────────┐
│                  ClientManager                   │
│                                                  │
│  active_clients: dict[user_id, UserClient]       │
│                                                  │
│  get_or_connect(user_id, dir, session_id=None)   │
│  switch_session(user_id, session_id) → UserClient│
│  interrupt(user_id)                              │
│  set_model(user_id, model)                       │
│  disconnect(user_id)                             │
│  cleanup_idle(timeout=300s)                      │
└─────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────┐
│                   UserClient                     │
│                                                  │
│  sdk_client: ClaudeSDKClient                     │
│  directory: str                                  │
│  session_id: str | None                          │
│  model: str | None                               │
│  last_active: datetime                           │
│  is_querying: bool                               │
│                                                  │
│  query(message, options) → AsyncIterator[msg]    │
│  interrupt()                                     │
│  set_model(model)                                │
└─────────────────────────────────────────────────┘
```

### Lifecycle

1. **First message** → `ClientManager.get_or_connect()` creates `UserClient`, connects SDK client
2. **Subsequent messages** → Reuses existing `UserClient`
3. **Directory change** (`/repo`) → Disconnects old client, creates new one for new directory
4. **Session switch** (`/sessions`) → Disconnects current, connects to selected session (~1s)
5. **Idle timeout** (5 min) → Background asyncio task disconnects idle clients
6. **`/stop` command** → Calls `UserClient.interrupt()` on active query
7. **Bot shutdown** → Disconnects all clients gracefully
8. **Bot restart** → Reads `bot_sessions` table, auto-reconnects on first message per user

### Integration Point

`ClaudeIntegration` facade is replaced. The orchestrator calls `ClientManager` directly via `context.bot_data["client_manager"]`. All other bot infrastructure (auth, middleware, storage, events) stays untouched.

## Session Management

### Source of Truth

`~/.claude/history.jsonl` — the same file the CLI reads/writes. No SQLite session table.

### Bidirectional Resume

The SDK launches a `claude` CLI subprocess. Every bot conversation IS a CLI session:

- **Bot → CLI:** SDK subprocess writes to `history.jsonl`. User SSHs to EC2, runs `claude --resume` — picks up same session.
- **CLI → Bot:** User works in CLI, switches to phone. Bot reads `history.jsonl`, auto-resumes latest session.

No bot bookkeeping needed for this. It's a natural consequence of using CLI local storage as source of truth.

### Auto-Resume Flow

```
User sends message
  → ClientManager.get_or_connect(user_id, directory)
  → SessionResolver.get_latest_session(directory)
     reads history.jsonl, filters by directory, sorts by timestamp
  → Returns session_id (or None for new session)
  → SDK client connects with options.resume = session_id
```

### Session Picker (`/sessions`)

```
User taps /sessions
  → SessionResolver.list_sessions(directory, limit=10)
  → Bot sends inline keyboard:
     [📍 Current] feat: add auth middleware — 2h ago
     [ ] fix: rate limiter bug — 5h ago
     [ ] refactor: storage layer — 1d ago
     [➕ New Session]
  → User taps one → ClientManager.switch_session()
```

### Bot State Persistence (SQLite)

```sql
CREATE TABLE bot_sessions (
    user_id     INTEGER PRIMARY KEY,
    session_id  TEXT NOT NULL,
    directory   TEXT NOT NULL,
    model       TEXT,
    betas       TEXT,          -- JSON array, e.g. '["context-1m-2025-08-07"]'
    last_active TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

`BotSessionRepository` with `upsert()`, `get_by_user()`, `cleanup_expired()`. Follows existing repository pattern in `src/storage/`.

On bot restart: read `bot_sessions`, auto-reconnect on first message per user (if not expired).

## SDK Options & Configuration

### Read from CLI, Don't Duplicate

| Option | Source |
|--------|--------|
| `model` | `~/.claude/settings.json` → `model` |
| `plugins` | `~/.claude/installed_plugins.json` |
| `system_prompt` | `SystemPromptPreset` — SDK auto-loads all CLAUDE.md levels |
| `thinking` | `~/.claude/settings.json` → `alwaysThinkingEnabled` |

### Bot-Only Settings

| Option | Value | Rationale |
|--------|-------|-----------|
| `permission_mode` | `bypassPermissions` | Headless — no interactive terminal |
| `system_prompt` append | Mobile-specific hints | "Keep responses concise for mobile reading" |
| `resume` | From `UserClient.session_id` | Session continuity |

### CLAUDE.md Levels (Automatic)

`SystemPromptPreset(preset="claude_code", append="...")` loads all three levels automatically:
- `~/.claude/CLAUDE.md` (user)
- `<project>/CLAUDE.md` (project)
- `<project>/.claude/CLAUDE.md` (local)

### OptionsBuilder

Constructs `ClaudeAgentOptions` per request:

**Always set:**
- `permission_mode` — `bypassPermissions`
- `model` — from CLI settings or user override via `/model`
- `system_prompt` — `SystemPromptPreset` with mobile append
- `cwd` — user's active directory
- `resume` — session ID from `UserClient`
- `betas` — if 1M context model selected

**Conditionally set:**
- `agents` — if AGENTS.md exists in directory
- `plugins` — from CLI plugin registry
- `fork_session` — if user requests fork

### Remaining Bot Env Vars

```
TELEGRAM_BOT_TOKEN          # Telegram auth
TELEGRAM_BOT_USERNAME       # Bot identity
ALLOWED_USERS               # Telegram user whitelist
APPROVED_DIRECTORIES        # Allowed working dirs
```

## Commands & Skills

### Telegram Commands

| Command | Action |
|---------|--------|
| `/start` | Welcome + auth check (keep existing) |
| `/new` | Disconnect current session, start fresh |
| `/sessions` | Inline keyboard of recent sessions per directory |
| `/repo` | Switch working directory (keep existing) |
| `/model` | Inline keyboard: sonnet / opus / haiku / sonnet 1M / opus 1M |
| `/stop` | Interrupt running query via `client.interrupt()` |
| `/status` | Show active session, directory, model, cost |
| `/commands` | Discover and display available skills as inline buttons |
| `/verbose` | Toggle output verbosity (keep existing) |
| `/compact` | Trigger context compaction |

### `/model` with 1M Context

```
User taps /model → inline keyboard:
  [sonnet]  [opus]  [haiku]
  [sonnet 1M]  [opus 1M]

Picking "opus 1M" → model="opus", betas=["context-1m-2025-08-07"]
Picking "sonnet"  → model="sonnet", betas=[]
```

### Context Compaction (`/compact`)

Primary: send `"/compact"` as user message to persistent client — if CLI recognizes it internally, native compaction happens. Fallback: start new session seeded with summary (lossy).

### Skill Discovery (`/commands`)

```
User taps /commands
  → SkillDiscoverer reads:
     1. ~/.claude/installed_plugins.json
     2. Project-local .claude/commands/
     3. SDK client.get_server_info()
  → Bot sends inline keyboard grouped by source:

     📦 Plugins:
     [/commit]  [/review-pr]  [/plan]

     📁 Project:
     [/deploy]  [/test]

  → User taps one
  → Bot sends skill name as message to Claude via persistent client
```

### Stream Output to Telegram

| SDK Event | Telegram Action |
|-----------|----------------|
| `AssistantMessage` (text) | Send/edit message with text |
| `AssistantMessage` (ThinkingBlock) | Show as collapsed or expanded per verbose level |
| `ToolUseBlock` | Show tool name + brief summary (verbose≥1) |
| `ToolResultBlock` | Update tool status (success/error) |
| `ResultMessage` | Final message + cost footer |

## Error Handling & Lifecycle

| Scenario | Handling |
|----------|----------|
| CLI session changes out-of-band | Detect stale session via history.jsonl timestamp. Reconnect if newer. |
| SDK subprocess crashes | Catch exception, clean up UserClient, reconnect on next message. Inform user. |
| Idle timeout (5 min) | Background task disconnects idle clients. Next message reconnects transparently. |
| Concurrent messages from same user | Queue per user — one query at a time. Second message gets "Please wait." |
| Bot restart | Read `bot_sessions` table. First message per user auto-reconnects to persisted session. |
| Directory not in APPROVED_DIRECTORIES | Reject before connecting. Keep existing middleware. |

### Cost Tracking

- `ResultMessage.total_cost_usd` captured after each query
- Displayed in response footer: `$0.03`
- `/status` shows cumulative session cost

## Stretch Goal: Telegram Permission Approval

Depends on SDK `can_use_tool` supporting async/awaitable callbacks.

**If supported:**
```
Claude wants to run: bash("rm -rf node_modules")
  → SDK fires can_use_tool callback
  → Bot sends Telegram message with inline buttons:
     [✅ Approve]  [❌ Deny]
  → User taps → callback returns True/False
  → Timeout 60s → auto-deny
```

**If not supported:** Fall back to `bypassPermissions`. Not a blocker.

## Feature Parity Gaps Closed

| # | Feature | Before | After |
|---|---------|--------|-------|
| 1 | Permission mode | Not set (broken headless) | `bypassPermissions` |
| 2 | Model selection | Config ignored | Read from CLI settings + `/model` command |
| 3 | Thinking config | Not set | Read from CLI settings |
| 4 | System prompt | Overwrites CLAUDE.md | `SystemPromptPreset` preserves all levels |
| 5 | Context compaction | Fake (lossy) | Native via persistent client |
| 6 | Interrupt/cancel | Impossible | `/stop` → `client.interrupt()` |
| 7 | Model switching | Impossible | `/model` → inline keyboard |
| 8 | 1M context beta | Not exposed | Blended into `/model` menu |
| 9 | Custom agents | Not used | Auto-detected AGENTS.md |
| 10 | Plugin/skill discovery | Partial | `/commands` with inline buttons |
| 11 | ThinkingBlock display | Silently dropped | Shown per verbose level |
| 12 | Per-request cost | Not shown | Response footer |
| 13 | Session continuity | Bot-managed SQLite | CLI's history.jsonl as source of truth |
| 14 | Session switching | Not possible | `/sessions` with inline picker |
| 15 | Bot restart recovery | Lost state | SQLite `bot_sessions` persists active session |

## Out of Scope

- Webhooks, scheduler, events, notifications — kept as-is
- Classic mode — kept as-is (low priority)
- Full hook system — `can_use_tool` sufficient for now
- Partial message streaming — nice-to-have, not in initial rewrite
- Session fork — available via SDK, low priority
