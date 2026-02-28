# Claude Code Telegram Bot -- Project Overview

## Project Description

A Telegram bot that provides remote access to Claude Code, allowing developers to interact with their projects from anywhere. The default interaction model is **agentic mode** -- a conversational interface where users chat naturally with Claude. A classic terminal-like mode with 13 commands is also available.

## Core Objectives

1. **Remote Development Access**: Enable developers to use Claude Code from any device with Telegram
2. **Security-First Design**: Implement robust security boundaries to prevent unauthorized access
3. **Conversational Interface**: Natural language interaction as the primary mode (agentic mode)
4. **Session Persistence**: Maintain Claude Code context across conversations and project switches
5. **Event-Driven Automation**: Support webhooks, scheduled jobs, and proactive notifications

## Target Users

- Developers who need coding assistance while mobile
- Teams wanting shared Claude Code access
- Users who prefer chat-based interfaces for development tasks
- Developers managing multiple projects remotely

## Key Features

### Agentic Mode (Default)
- Natural language conversation with Claude -- no commands needed
- Minimal command set: `/start`, `/new`, `/resume`, `/status`, `/repo`
- CLI-aligned session lifecycle: `/repo` switches directories, `/new` starts fresh, `/resume` picks up a previous session
- File and image upload support

### Classic Mode
- Terminal-like commands (cd, ls, pwd)
- Project quick-switching with visual selection
- Inline keyboards for common actions
- Git status integration
- Session export in multiple formats

### Event-Driven Platform
- **Event Bus**: Async pub/sub system with typed event subscriptions
- **Webhook API**: FastAPI server receiving GitHub and generic webhooks with signature verification
- **Job Scheduler**: APScheduler cron jobs with persistent storage
- **Notifications**: Rate-limited Telegram delivery for agent responses

### Claude Code Integration
- Full Claude Code SDK integration with actor-based client lifecycle
- Session management per user/project with explicit resume via `/resume` command
- Tool usage visibility with configurable verbose output

### Security & Access Control
- Approved directory boundaries
- User authentication (whitelist and token-based)
- Webhook authentication (HMAC-SHA256, Bearer token)
- Input validation and audit logging

## Technical Architecture

### Components

1. **MessageOrchestrator** (`src/bot/orchestrator.py`)
   - Routes to agentic or classic handlers based on mode
   - Dependency injection for all handlers

2. **Configuration** (`src/config/`)
   - Pydantic Settings v2 with environment variables
   - Feature flags for dynamic functionality control

3. **Authentication** (`src/security/`)
   - User verification, token management, permission checking
   - Input validation and security middleware

4. **Claude Integration** (`src/claude/`)
   - Actor-based `UserClient` with start/submit/stop lifecycle
   - `ClientManager` for per-user client management
   - `OptionsBuilder` for SDK configuration with `can_use_tool` callback

5. **Storage Layer** (`src/storage/`)
   - SQLite database with repository pattern
   - Session persistence and analytics

6. **Event Bus** (`src/events/`)
   - Async pub/sub with typed subscriptions
   - AgentHandler bridges events to Claude
   - EventSecurityMiddleware validates events

7. **Webhook API** (`src/api/`)
   - FastAPI server for external webhooks
   - GitHub HMAC-SHA256 + generic Bearer token auth

8. **Scheduler** (`src/scheduler/`)
   - APScheduler with cron triggers
   - Persistent job storage in SQLite

9. **Notifications** (`src/notifications/`)
   - Rate-limited Telegram delivery
   - Message splitting and broadcast support

### Data Flow

**Agentic mode (direct messages):**
```
User Message -> Telegram -> Middleware Chain -> MessageOrchestrator
    -> ClientManager.get_or_connect() -> UserClient.submit() -> SDK streaming
    -> StreamHandler extracts events -> Real-time progress -> Telegram
```

**External triggers (webhooks/scheduler):**
```
Webhook/Cron -> EventBus -> AgentHandler -> ClaudeIntegration
    -> AgentResponseEvent -> NotificationService -> Telegram
```

### Security Model

- **Directory Isolation**: All operations confined to approved directory tree
- **User Authentication**: Whitelist or token-based access
- **Input Validation**: Sanitize all user inputs
- **Webhook Verification**: HMAC-SHA256 and Bearer token authentication
- **Audit Trail**: Log all operations for security review

## Development Principles

1. **Security First**: Every feature must consider security implications
2. **Conversational by Default**: Agentic mode as the primary interaction model
3. **Event-Driven**: Decoupled components communicating through the event bus
4. **Testability**: Comprehensive test coverage
5. **Documentation**: Clear docs for users and contributors
