# Design: Directory Navigation, Session Resume & Dynamic Command Loading

**Date:** 2026-02-25
**Status:** Draft

## Problem Statement

The Telegram bot currently locks users to a single `APPROVED_DIRECTORY`, auto-resumes only the most recent session (with no choice), and has zero awareness of Claude Code's native skills/commands. This makes the bot feel like a limited wrapper rather than a remote CLI experience.

## Goals

1. Navigate between multiple workspace roots and their subdirectories
2. Choose which session to resume when switching directories (not just the latest)
3. Dynamically discover and execute Claude Code skills from Telegram
4. Map useful built-in CLI commands to bot equivalents where gaps exist

## Non-Goals

- Full parity with every Claude Code CLI feature
- Replacing the CLI for local development
- Supporting enterprise-managed skills (can be added later)

---

## 1. Multi-Root Directory Navigation

### Current State

- Single `APPROVED_DIRECTORY` env var — one root
- `/repo` lists immediate subdirectories of that root
- Working directory stored in Telegram's volatile `context.user_data` — lost on restart

### Design

**New config: `APPROVED_DIRECTORIES`**

Comma-separated list of absolute paths. Backward-compatible with existing `APPROVED_DIRECTORY` (treated as a single-item list).

```env
# Before
APPROVED_DIRECTORY=/home/user/projects/myapp

# After (supports multiple)
APPROVED_DIRECTORIES=/home/user/projects/myapp,/home/user/projects/infra,/home/user/work
```

**Settings changes (`src/config/settings.py`):**

```python
approved_directories: list[Path]  # New field, parsed from comma-separated env var
approved_directory: Path          # Kept for backward compat, returns first item
```

Validation at startup:
- All paths must exist and be absolute
- Reject overlapping paths (e.g., `/home/user/projects` and `/home/user/projects/myapp`)
- Warn if any path is not a git repository

**`/repo` command enhancement:**

When called with no arguments, shows inline keyboard buttons:

```
📁 Workspaces
[myapp]  [infra]  [work]

📂 myapp/
[backend]  [frontend]  [shared]
```

- Top row: workspace roots (from `APPROVED_DIRECTORIES`)
- Below each: immediate subdirectories (expandable on tap)
- Tapping a directory switches `current_directory` and triggers session lookup

**Working directory persistence:**

Add `current_directory` column to the `users` table (or a new `user_state` table) in SQLite. Survives bot restarts.

```sql
ALTER TABLE users ADD COLUMN current_directory TEXT;
```

On bot startup, restore each user's last directory from the database.

**Security:**

- Each approved directory validated independently
- Path traversal checks (`..`, symlinks) enforce staying within one of the approved roots
- `SecurityValidator.validate_path()` updated to check against the full list
- `can_use_tool` callback checks all approved roots, not just one

---

## 2. Session Picker (`/sessions`)

### Current State

- Sessions keyed by `user_id + project_path` in SQLite
- Auto-resumes most recent session — no user choice
- Session IDs come from Claude SDK's `ResultMessage`

### Design

**Session discovery — hybrid approach:**

Two sources of truth, combined:

1. **`~/.claude/history.jsonl`** — Claude Code's native session history. Each line:
   ```json
   {
     "display": "fix the auth middleware",
     "timestamp": 1768768959714,
     "project": "/home/user/projects/myapp",
     "sessionId": "14071859-2f11-433d-a454-468b4bfd3ac1"
   }
   ```
2. **`bot_sessions` SQLite table** — Thin registry for bot-specific metadata:
   ```sql
   CREATE TABLE bot_sessions (
     session_id TEXT PRIMARY KEY,
     user_id INTEGER NOT NULL,
     directory TEXT NOT NULL,
     display_name TEXT,
     first_seen_at TIMESTAMP NOT NULL,
     last_used_at TIMESTAMP NOT NULL,
     source TEXT DEFAULT 'bot',  -- 'bot' or 'cli'
     FOREIGN KEY (user_id) REFERENCES users(user_id)
   );
   CREATE INDEX idx_bot_sessions_user_dir
     ON bot_sessions(user_id, directory);
   ```

**Sync logic:**

On `/sessions` or `/repo` switch, the bot:
1. Reads `~/.claude/history.jsonl`, filters entries matching `current_directory`
2. Merges with `bot_sessions` table (upsert by `session_id`)
3. Sessions from CLI that the bot hasn't seen get `source='cli'`
4. Returns combined list, ordered by `last_used_at` descending

**`/sessions` command:**

Shows inline keyboard buttons:

```
📋 Sessions in /home/user/projects/myapp

[Feb 24 — fix the auth middleware]
[Feb 22 — add user registration]
[Feb 20 — refactor database layer]

[+ New Session]
```

- Label format: `{date} — {display_name}` (from `history.jsonl` `display` field or first message snippet)
- "New Session" button always present at the bottom
- Tapping a session sets `session_id` in the session manager; next message resumes via SDK

**Auto-resume behavior change:**

- 1 active session for the directory -> auto-resume silently (no change)
- 2+ active sessions -> show the session picker instead of guessing
- 0 sessions -> start a new one automatically

**Error handling:**

- `history.jsonl` missing/empty: show only bot-tracked sessions, or just "New Session"
- `history.jsonl` format changed: log warning AND send Telegram message to user: "Warning: Claude Code session history format may have changed. `/sessions` may be incomplete. Check for updates."
- Malformed lines: skip with warning, don't crash
- Expired/invalid session ID on resume: catch SDK error, notify user ("Session expired, starting fresh"), create new session

---

## 3. Dynamic Skill/Command Loading (`/commands`)

### Current State

- Bot has no awareness of Claude Code skills or commands
- User text goes directly to SDK as natural language
- No command passthrough or discovery

### Background

Claude Code uses the [Agent Skills](https://agentskills.io) standard:

- **Project skills**: `{project}/.claude/skills/{name}/SKILL.md`
- **Personal skills**: `~/.claude/skills/{name}/SKILL.md`
- **Legacy commands**: `.claude/commands/{name}.md` (deprecated, still supported)
- **Built-in commands**: Hardcoded in CLI binary (`/clear`, `/compact`, `/cost`, etc.)

Each `SKILL.md` has YAML frontmatter:

```yaml
---
name: review-pr
description: Review a pull request for issues
argument-hint: "[PR number]"
user-invocable: true
disable-model-invocation: true
allowed-tools: Read, Grep, Bash(git *)
---
```

### Design

**`/commands` command:**

Scans filesystem for available skills, displays as inline keyboard buttons:

```
📁 Project Skills
[fix-tests]  [add-feature]  [review-pr ...]

🌐 Personal Skills
[commit]  [explain]
```

**Discovery order** (matches CLI priority):
1. `{current_directory}/.claude/skills/*/SKILL.md` (project)
2. `~/.claude/skills/*/SKILL.md` (personal)
3. `{current_directory}/.claude/commands/*.md` (legacy project)
4. `~/.claude/commands/*.md` (legacy personal)

**Filtering:**
- Only show skills with `user-invocable: true` (or field absent, defaults to true)
- Skip skills with `user-invocable: false`

**Button behavior — two types based on arguments:**

| Has `argument-hint`? | Button type | Behavior |
|---------------------|-------------|----------|
| No | `callback_data` | Tap executes immediately |
| Yes | `switch_inline_query_current_chat` | Tap pre-fills `/{skill-name} ` in input box, user types args and sends |

Skills with arguments show `...` suffix on the button label (e.g., `[review-pr ...]`) to indicate input is needed.

**Execution flow:**

1. User taps button (or sends pre-filled text with args)
2. Bot reads full `SKILL.md` body
3. Substitutes variables:
   - `$ARGUMENTS` -> user-provided arguments (full string)
   - `$ARGUMENTS[0]`, `$0` -> first argument
   - `$ARGUMENTS[1]`, `$1` -> second argument, etc.
   - `${CLAUDE_SESSION_ID}` -> current session ID
4. Sends the resolved prompt to Claude via SDK
5. Respects `allowed-tools` if SDK supports restricting tools per-query

**Fresh scan:**
Each `/commands` invocation re-scans the filesystem. No caching. New skills added via CLI or manually are picked up immediately.

**Skill metadata parsing:**

New utility module `src/skills/loader.py`:

```python
@dataclass(frozen=True)
class SkillMetadata:
    name: str
    description: str
    argument_hint: str | None
    user_invocable: bool
    allowed_tools: list[str]
    source: str          # 'project' | 'personal' | 'legacy_project' | 'legacy_personal'
    file_path: Path

def discover_skills(project_dir: Path) -> list[SkillMetadata]:
    """Scan filesystem for skills, return metadata only (not full content)."""
    ...

def load_skill_body(skill: SkillMetadata) -> str:
    """Load full SKILL.md body on demand."""
    ...

def resolve_skill_prompt(body: str, arguments: str, session_id: str) -> str:
    """Substitute $ARGUMENTS, $0, ${CLAUDE_SESSION_ID}, etc."""
    ...
```

---

## 4. Built-in Command Mapping

Only add commands that fill gaps — don't duplicate what exists.

| CLI Command | Bot Equivalent | Action |
|-------------|---------------|--------|
| `/clear` | `/new` (exists) | No change needed |
| `/cost` | `/status` (exists) | Verify cost info is shown; add if missing |
| `/resume` | `/sessions` (new) | Covered by Section 2 |
| `/compact` | `/compact` (new) | See below |
| `/help` | `/start` (exists) | Update to list all commands including new ones |
| `/model` | Not needed | Model selection is a deployment concern for the bot |
| `/permissions` | Not needed | Permissions managed by bot config |

**`/compact` implementation:**

The SDK doesn't expose a native compact operation. Simulated approach:

1. Collect current session's conversation summary (ask Claude to summarize the key context and decisions so far)
2. Store the summary
3. Start a new SDK session in the same directory
4. Seed the new session with a system-level context message containing the summary
5. Update `bot_sessions` to track the new session ID, link it to the old one
6. Notify user: "Context compacted. Session continues."

This preserves continuity (Claude knows what was discussed) without carrying full history.

---

## 5. Error Handling Summary

| Scenario | Behavior |
|----------|----------|
| `history.jsonl` missing/empty | `/sessions` shows bot-tracked sessions only, or just "New Session" |
| `history.jsonl` format changed | Log warning + send Telegram notification to user |
| Malformed `history.jsonl` lines | Skip with warning, continue parsing |
| Session expired on resume | Catch error, notify user, start new session |
| No skills directory in project | `/commands` shows personal skills only, or "No skills found" with hint |
| Malformed `SKILL.md` frontmatter | Skip skill with warning, don't crash |
| Directory switch during streaming response | Queue switch, apply after response completes |
| Overlapping approved directories | Reject at startup with clear error message |
| `switch_inline_query_current_chat` fails | Fall back to two-step flow (bot asks for args) |

---

## 6. New/Modified Files

| File | Change |
|------|--------|
| `src/config/settings.py` | Add `APPROVED_DIRECTORIES`, backward compat |
| `src/security/validator.py` | Multi-root path validation |
| `src/claude/session.py` | Session picker logic, `history.jsonl` reader |
| `src/storage/database.py` | `bot_sessions` table, `users.current_directory` column |
| `src/storage/session_storage.py` | New queries for session discovery |
| `src/bot/orchestrator.py` | `/sessions`, `/commands`, `/compact` handlers, `/repo` enhancement |
| `src/skills/` (new) | `loader.py` — skill discovery, parsing, prompt resolution |
| `src/skills/__init__.py` (new) | Module init |

---

## 7. Configuration Summary

| Env Var | Default | Description |
|---------|---------|-------------|
| `APPROVED_DIRECTORIES` | (none) | Comma-separated workspace roots |
| `APPROVED_DIRECTORY` | (none) | Backward compat, single root |
| `CLAUDE_HISTORY_PATH` | `~/.claude/history.jsonl` | Override path to session history |
| `SESSION_PICKER_THRESHOLD` | `2` | Show picker when this many sessions exist |

---

## Open Questions

1. **Compact quality** — How good will the summarize-and-reseed approach be? May need iteration on the summary prompt.
2. **`history.jsonl` stability** — This is an internal CLI format. Worth monitoring for changes across Claude Code updates.
3. **Inline bot mode** — `switch_inline_query_current_chat` should work without enabling full inline mode via BotFather. Needs verification during implementation.
4. **Skill `context: fork` and `agent` fields** — Should the bot respect these? Probably not in v1, but worth considering for v2.
