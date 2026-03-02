# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Telegram bot providing remote access to Claude Code. Python 3.12+, built with uv, using `python-telegram-bot` for Telegram and `claude-agent-sdk` for Claude Code integration.

## Commands

```bash
make dev              # Install all deps (including dev)
make install          # Production deps only
make run              # Run the bot
make run-debug        # Run with debug logging
make test             # Run tests with coverage
make lint             # Black + isort + flake8 + mypy
make format           # Auto-format with black + isort

# Run a single test
uv run pytest tests/unit/test_config.py -k test_name -v

# Type checking only
uv run mypy src
```

## Architecture

### Claude SDK Integration

**Agentic mode** uses a layered client architecture:

- `ClientManager` (`src/claude/client_manager.py`) — Manages persistent per-(user, chat, thread) `UserClient` actors keyed by `(user_id, chat_id, message_thread_id)` for concurrent multi-topic sessions. Actors self-remove on idle timeout via `on_exit` callback.
- `UserClient` (`src/claude/user_client.py`) — Actor-based Claude SDK client for one user. A long-lived asyncio task owns the full `connect → query → disconnect` lifecycle in a single task (required by the SDK's anyio cancel scopes). Public API: `start()`, `submit()`, `stop()`, `interrupt()`. Idle timeout built into the worker's `queue.get()`.
- `OptionsBuilder` (`src/claude/options.py`) — Builds `ClaudeAgentOptions` from CLI settings (`~/.claude/settings.json`) with full feature parity: model selection, system prompt, permission mode, tool monitoring via `can_use_tool`, and `CLAUDECODE` env clearing.
- `StreamHandler` (`src/claude/stream_handler.py`) — Extracts structured `StreamEvent`s from SDK messages (`ResultMessage`, `AssistantMessage`, `StreamEvent` partials).
- `ProgressMessageManager` (`src/bot/progress.py`) — Rich progress display for Telegram. Maintains a persistent activity log message showing narrative text, tool calls, and thinking indicators in real-time. Intermediate text appears inline (💬) so users can follow Claude's reasoning as it works; only the final text block is sent as the response message.
- `SessionResolver` (`src/claude/session.py`) — Resolves session IDs from the `chat_sessions` DB table, keyed by `(user_id, chat_id, message_thread_id)`.

**Classic mode** still uses `ClaudeIntegration` (facade in `src/claude/facade.py`) wrapping `ClaudeSDKManager` (`src/claude/sdk_integration.py`).

Sessions auto-resume: per `(user_id, chat_id, message_thread_id)`, stored in the `chat_sessions` SQLite table. Real-time streaming enabled via `include_partial_messages=True`. Native skill/plugin pass-through via `tools={"type": "preset", "preset": "claude_code"}` and `setting_sources=["user", "project"]`.

### Request Flow

**Agentic mode** (default, `AGENTIC_MODE=true`):

```
Telegram message -> Security middleware (group -3) -> Auth middleware (group -2)
-> MessageOrchestrator.agentic_text() (group 10)
-> (chat_id, message_thread_id) routing — supergroups auto-detected, DMs use thread_id=0
-> ClientManager.get_or_connect(user_id, chat_id, message_thread_id) -> UserClient.submit() -> actor processes query
-> StreamHandler extracts events inside actor -> ProgressMessageManager renders real-time activity log
-> Final response stored in SQLite -> Sent back to Telegram
```

Unrecognized `/commands` are passed through to the Claude SDK as skill invocations via native tool preset.

**External triggers** (webhooks, scheduler):

```
Webhook POST /webhooks/{provider} -> Signature verification -> Deduplication
-> Publish WebhookEvent to EventBus -> AgentHandler.handle_webhook()
-> ClaudeIntegration.run_command() -> Publish AgentResponseEvent
-> NotificationService -> Rate-limited Telegram delivery
```

**Classic mode** (`AGENTIC_MODE=false`): Same middleware chain, but routes through full command/message handlers in `src/bot/handlers/` with 13 commands and inline keyboards.

### Dependency Injection

Bot handlers access dependencies via `context.bot_data`:
```python
context.bot_data["auth_manager"]
context.bot_data["claude_integration"]  # classic mode
context.bot_data["client_manager"]      # agentic mode (ClientManager)
context.bot_data["storage"]
context.bot_data["security_validator"]
```

### Key Directories

- `src/config/` -- Pydantic Settings v2 config with env detection, feature flags (`features.py`), YAML project loader (`loader.py`)
- `src/bot/handlers/` -- Telegram command, message, and callback handlers (classic mode + project thread commands)
- `src/bot/middleware/` -- Auth, security input validation
- `src/bot/features/` -- Git integration, file handling, quick actions, session export
- `src/bot/orchestrator.py` -- MessageOrchestrator: routes to agentic or classic handlers, project-topic routing
- `src/claude/` -- Claude integration: `client_manager.py` (per-(user,chat,thread) clients), `user_client.py` (SDK wrapper), `options.py` (SDK options builder), `stream_handler.py` (message parsing), `session.py` (session resolver), `monitor.py` (tool monitoring), `facade.py` (classic mode), `transcript.py` (session transcript reader for history replay)
- `src/projects/` -- Multi-project support: `thread_manager.py` (Telegram topic routing, auto-detected for supergroups), `lifecycle.py` (topic close/reopen/delete/rename lifecycle), `topic_namer.py` (topic name generation, currently unused — names use `dir_name — session_id[:8]` format)
- `src/storage/` -- SQLite via aiosqlite, repository pattern (`chat_sessions` and `audit_log` tables only)
- `src/security/` -- Multi-provider auth (whitelist + token), input validators (with optional `disable_security_patterns`), audit logging
- `src/events/` -- EventBus (async pub/sub), event types, AgentHandler, EventSecurityMiddleware
- `src/api/` -- FastAPI webhook server, GitHub HMAC-SHA256 + Bearer token auth
- `src/scheduler/` -- APScheduler cron jobs, persistent storage in SQLite
- `src/notifications/` -- NotificationService, rate-limited Telegram delivery

### Security Model

4-layer defense: authentication (whitelist/token) -> directory isolation (APPROVED_DIRECTORY + path traversal prevention) -> input validation (blocks `..`, `;`, `&&`, `$()`, etc.) -> audit logging.

`SecurityValidator` blocks access to secrets (`.env`, `.ssh`, `id_rsa`, `.pem`) and dangerous shell patterns. Can be relaxed with `DISABLE_SECURITY_PATTERNS=true` (trusted environments only).

`ToolMonitor` validates Claude's tool calls against allowlist/disallowlist, file path boundaries, and dangerous bash patterns. Tool name validation can be bypassed with `DISABLE_TOOL_VALIDATION=true`.

Webhook authentication: GitHub HMAC-SHA256 signature verification, generic Bearer token for other providers, atomic deduplication via `webhook_events` table.

### Configuration

Settings loaded from environment variables via Pydantic Settings. Required: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME`, `APPROVED_DIRECTORY`. Key optional: `ALLOWED_USERS` (comma-separated Telegram IDs), `ANTHROPIC_API_KEY`, `ENABLE_MCP`, `MCP_CONFIG_PATH`.

Agentic platform settings: `AGENTIC_MODE` (default true), `ENABLE_API_SERVER`, `API_SERVER_PORT` (default 8080), `GITHUB_WEBHOOK_SECRET`, `WEBHOOK_API_SECRET`, `ENABLE_SCHEDULER`, `NOTIFICATION_CHAT_IDS`.

Security relaxation (trusted environments only): `DISABLE_SECURITY_PATTERNS` (default false), `DISABLE_TOOL_VALIDATION` (default false).

Multi-project concurrent sessions: Topic routing is always active and auto-detected for Telegram supergroups with Topics enabled. Each forum topic is bound to exactly one session (1:1 model). Same directory can have multiple topics for different sessions. Private DMs use `(user_id, 0)`. No feature flag required — the bot detects the chat type automatically. Use `/start` in the General topic to create a project topic (wizard: pick dir → pick session → topic created with session bound). `/new` inside a topic creates a new topic for the same directory with a fresh session. `/remove` inside a topic permanently deletes it (requires double-confirmation; calls `delete_forum_topic`). `/status` in General shows a dashboard of active sessions.

Topic lifecycle (managed by `TopicLifecycleManager` in `src/projects/lifecycle.py`):
- Topics auto-close on idle timeout via `close_forum_topic`, and reopen automatically on the next message via `reopen_forum_topic`
- Topics are named `dir_name — session_id[:8]` at creation (Haiku auto-naming disabled)
- `/history [N]` shows a condensed session transcript in the topic (last N exchanges, default all)
- `/remove` requires double-confirmation and permanently deletes the topic via `delete_forum_topic`
- `/resume` is rejected inside topics (session is fixed per topic; use `/new` for a fresh topic)

Feature flags in `src/config/features.py` control: MCP, git integration, file uploads, quick actions, session export, image uploads, conversation mode, agentic mode, API server, scheduler.

### DateTime Convention

All datetimes use timezone-aware UTC: `datetime.now(UTC)` (not `datetime.utcnow()`). SQLite adapters auto-convert TIMESTAMP/DATETIME columns to `datetime` objects via `detect_types=PARSE_DECLTYPES`. Model `from_row()` methods must guard `fromisoformat()` calls with `isinstance(val, str)` checks.

## Code Style

- Black (88 char line length), isort (black profile), flake8, mypy strict, autoflake for unused imports
- pytest-asyncio with `asyncio_mode = "auto"`
- structlog for all logging (JSON in prod, console in dev)
- Type hints required on all functions (`disallow_untyped_defs = true`)
- Use `datetime.now(UTC)` not `datetime.utcnow()` (deprecated)

## Adding a New Bot Command

### Agentic mode

Agentic mode commands: `/start`, `/new`, `/interrupt`, `/status`, `/compact`, `/model`, `/repo`, `/resume`, `/commands`. In supergroups with topics (auto-detected): `/start` (wizard: pick dir → session → creates topic), `/new` (creates new topic), `/remove`, `/history`, `/status` (dashboard). `/resume` is only available in DM and General topic. Unrecognized `/commands` are passed through to Claude as skill invocations. To add a new command:

1. Add handler function in `src/bot/orchestrator.py`
2. Register in `MessageOrchestrator._register_agentic_handlers()`
3. Add to `MessageOrchestrator.get_bot_commands()` for Telegram's command menu
4. Add audit logging for the command

### Classic mode

1. Add handler function in `src/bot/handlers/command.py`
2. Register in `MessageOrchestrator._register_classic_handlers()`
3. Add to `MessageOrchestrator.get_bot_commands()` for Telegram's command menu
4. Add audit logging for the command
