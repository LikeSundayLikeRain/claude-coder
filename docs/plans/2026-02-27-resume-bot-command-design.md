# Resume Bot Command Design

**Date**: 2026-02-27
**Status**: Approved

## Problem

When working in Claude Code CLI, there's no way to hand off a session to the Telegram bot. You have to manually find the session in Telegram using `/sessions` and select it. This breaks flow when you want to continue a conversation on mobile or from Telegram.

## Solution

A Claude Code slash command (`/resume-bot`) that sends the current session ID to the Telegram bot via the Telegram Bot API. The bot resumes that session and shows a transcript preview.

## Architecture

### Component 1: Claude Code Skill

**Location**: `.claude/skills/resume-bot/SKILL.md`

A standard Claude Code skill that:
1. Reads config from `.claude/resume-bot.json`
2. Uses the `${CLAUDE_SESSION_ID}` placeholder for the current session ID
3. Sends a Telegram Bot API message via `curl`

**Config file**: `.claude/resume-bot.json`
```json
{
  "bot_token": "<telegram-bot-token>",
  "chat_id": "<your-telegram-chat-id>"
}
```

**Message sent**: `/resume <session_id>`

### Component 2: Bot `/resume` Command Handler

**Location**: `src/bot/orchestrator.py`

New handler `agentic_resume` registered in `_register_agentic_handlers`:

1. Parse `session_id` from `/resume <session_id>`
2. Look up session in `~/.claude/history.jsonl` to get the `project` directory
3. Set `context.user_data["claude_session_id"]` and `context.user_data["current_directory"]`
4. Read last 3 message pairs via `read_session_transcript(session_id, project_dir, limit=3)`
5. Send transcript preview + "Session resumed" confirmation
6. Next user message flows through `agentic_text()` → `ClientManager.get_or_connect(session_id=...)` as normal

**Error cases**:
- Missing session ID → reply with usage hint
- Session not found in history → "Session not found"

**No directory validation** — the command originates from the user's own CLI, so it's a trusted source. Directory is inferred from the history entry's `project` field.

## Data Flow

```
Claude Code CLI
  │
  ├─ User types: /resume-bot
  ├─ Claude reads SKILL.md
  │  ├─ Reads .claude/resume-bot.json → {bot_token, chat_id}
  │  ├─ Session ID from ${CLAUDE_SESSION_ID}
  │  └─ curl POST https://api.telegram.org/bot<token>/sendMessage
  │       -d chat_id=<chat_id> -d text="/resume <session_id>"
  ├─ Claude reports: "Session handed off to Telegram bot"
  │
  ▼
Telegram Bot
  │
  ├─ Auth middleware validates user
  ├─ agentic_resume handler:
  │  ├─ Parses session_id
  │  ├─ Looks up project dir from history.jsonl
  │  ├─ Sets session + directory in user context
  │  ├─ Reads transcript (last 3 exchanges)
  │  └─ Sends preview + confirmation
  │
  ▼
User continues conversation in Telegram
  ├─ Next message → agentic_text()
  │  └─ ClientManager.get_or_connect(session_id=<resumed>)
  └─ Claude picks up where it left off
```

## Reused Infrastructure

- `read_session_transcript()` from `src/claude/history.py` — already used by `/sessions` callback
- `SessionResolver` / `read_claude_history()` — for session lookup by ID
- `ClientManager.get_or_connect()` — existing session resume path
- Skill placeholder `${CLAUDE_SESSION_ID}` — built into skill system

## Scope

**In scope**:
- Skill file (SKILL.md)
- Config file format
- Bot-side `/resume` command handler
- Transcript preview on resume

**Out of scope**:
- MCP tool integration (can upgrade later)
- Bidirectional sync (bot → CLI)
- Multi-user support for the skill (single-user config)

## Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Communication channel | Telegram Bot API (sendMessage) | Uniform code path through existing bot handlers, no API server dependency |
| Approach | Pure skill (no scripts) | Simplest, zero new deps, upgradeable to script later |
| Directory handling | Inferred from history.jsonl | Bot looks up `project` field by session ID, no need to send directory |
| Resume UX | Show last 2-3 transcript exchanges | Gives context to pick up where you left off |
| Directory validation | Skipped | Trusted source (user's own CLI) |
| Config location | `.claude/resume-bot.json` | Natural location for Claude Code skill config |
