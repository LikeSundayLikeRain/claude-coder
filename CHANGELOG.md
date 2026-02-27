# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **Claude Integration Rewrite**: Replaced layered SDK wrapper with actor-based `UserClient` pattern
  - `UserClient` owns the full SDK lifecycle (connect → query → disconnect) in a single asyncio task
  - `ClientManager` manages per-user `UserClient` instances with automatic idle timeout
  - `OptionsBuilder` constructs `ClaudeAgentOptions` with `can_use_tool` callback
  - `StreamHandler` extracts structured events from SDK messages for real-time display
  - Removed `ClaudeSDKManager`, `SessionManager`, `InMemorySessionStorage`, cleanup loop
- **Build System**: Migrated from Poetry to uv for dependency management
- **Python Version**: Minimum version bumped from 3.11 to 3.12

### Removed
- **Rate Limiting**: Removed user-facing rate limiting (token bucket algorithm, burst protection)
- **Cost Tracking**: Removed per-user cost tracking and `CLAUDE_MAX_COST_PER_USER` setting
- **CLI Subprocess Backend**: Removed `integration.py` and `parser.py` (SDK-only now)
- **ToolMonitor Class**: Replaced with SDK-native `can_use_tool` callback

### Added
- **Version Management & Distribution**:
  - Single source of truth: version read from `pyproject.toml` via `importlib.metadata`
  - GitHub Release workflow triggered by `v*` tags -- runs tests, creates Release
  - Rolling `latest` git tag updated on each stable release for `pip install git+...@latest`
  - Makefile targets: `bump-patch`, `bump-minor`, `bump-major`, `release`, `version`
  - Pre-release support (`-rc`, `-beta`, `-alpha` tags)

### Previously Added
- **Agentic Mode** (default interaction model):
  - `MessageOrchestrator` routes messages to agentic (3 commands) or classic (13 commands) handlers based on `AGENTIC_MODE` setting
  - Natural language conversation with Claude -- no terminal commands needed
  - Automatic session persistence per user/project directory
- **Event-Driven Platform**:
  - `EventBus` -- async pub/sub system with typed event subscriptions (UserMessage, Webhook, Scheduled, AgentResponse)
  - `AgentHandler` -- bridges events to `ClaudeIntegration.run_command()` for webhook and scheduled event processing
  - `EventSecurityMiddleware` -- validates events before handler processing
- **Webhook API Server** (FastAPI):
  - `POST /webhooks/{provider}` endpoint for GitHub, Notion, and generic providers
  - GitHub HMAC-SHA256 signature verification
  - Generic Bearer token authentication
  - Atomic deduplication via `webhook_events` table
  - Health check at `GET /health`
- **Job Scheduler** (APScheduler):
  - Cron-based job scheduling with persistent storage in `scheduled_jobs` table
  - Jobs publish `ScheduledEvent` to event bus on trigger
  - Add, remove, and list jobs programmatically
- **Notification Service**:
  - Subscribes to `AgentResponseEvent` for Telegram delivery
  - Per-chat rate limiting (1 msg/sec) to respect Telegram limits
  - Message splitting at 4096 char boundary
  - Broadcast to configurable default chat IDs
- **Database Migration 3**: `scheduled_jobs` and `webhook_events` tables, WAL mode enabled
- **Automatic Session Resumption**: Sessions are now automatically resumed per user+directory
  - SDK integration passes `resume` parameter to Claude Code for real session continuity
  - Session IDs extracted from Claude's `ResultMessage` instead of generated locally
  - `/cd` looks up and resumes existing sessions for the target directory
  - Auto-resume from SQLite database survives bot restarts
  - Graceful fallback to fresh session when resume fails
  - `/new` and `/end` are the only ways to explicitly clear session context

### Recently Completed

#### Storage Layer Implementation (TODO-6) - 2025-06-06
- **SQLite Database with Complete Schema**:
  - 7 core tables: users, sessions, messages, tool_usage, audit_log, user_tokens, cost_tracking
  - Foreign key relationships and proper indexing for performance
  - Migration system with schema versioning and automatic upgrades
  - Connection pooling for efficient database resource management
- **Repository Pattern Data Access Layer**:
  - UserRepository, SessionRepository, MessageRepository, ToolUsageRepository
  - AuditLogRepository, CostTrackingRepository, AnalyticsRepository
- **Persistent Session Management**:
  - SQLiteSessionStorage replacing in-memory storage
  - Session persistence across bot restarts and deployments
- **Analytics and Reporting System**:
  - User dashboards with usage statistics and cost tracking
  - Admin dashboards with system-wide analytics

#### Telegram Bot Core (TODO-4) - 2025-06-06
- Complete Telegram bot with command routing, message parsing, inline keyboards
- Navigation commands: /cd, /ls, /pwd for directory management
- Session commands: /new, /continue, /status for Claude sessions
- File upload support, progress indicators, response formatting

#### Claude Code Integration (TODO-5) - 2025-06-06
- Async process execution with timeout handling
- Session state management and cross-conversation continuity
- Streaming JSON output parsing, tool call extraction
- Cost tracking and usage monitoring

#### Authentication & Security Framework (TODO-3) - 2025-06-05
- Multi-provider authentication (whitelist + token)
- Rate limiting with token bucket algorithm
- Input validation, path traversal prevention
- Security audit logging with risk assessment
- Bot middleware framework (auth, rate limit, security, burst protection)

## [0.1.0] - 2025-06-05

### Added

#### Project Foundation (TODO-1)
- Complete project structure with Poetry dependency management
- Exception hierarchy, structured logging, testing framework
- Code quality tools: Black, isort, flake8, mypy with strict settings

#### Configuration System (TODO-2)
- Pydantic Settings v2 with environment variable loading
- Environment-specific overrides (development, testing, production)
- Feature flags system for dynamic functionality control
- Comprehensive validation with cross-field dependencies

## Development Status

- **TODO-1**: Project Structure & Core Setup -- Complete
- **TODO-2**: Configuration Management -- Complete
- **TODO-3**: Authentication & Security Framework -- Complete
- **TODO-4**: Telegram Bot Core -- Complete
- **TODO-5**: Claude Code Integration -- Complete
- **TODO-6**: Storage & Persistence -- Complete
- **TODO-7**: Advanced Features -- Complete (agentic platform, webhooks, scheduler, notifications)
- **TODO-8**: Complete Testing Suite -- In progress
- **TODO-9**: Deployment & Documentation -- In progress
