# Native Skill Pass-Through Design

**Date:** 2026-02-28
**Status:** Approved

## Problem

The bot manually discovers skills from the filesystem, reads SKILL.md bodies, resolves
`$ARGUMENTS` placeholders, and wraps everything in `<skill-invocation>/<skill-body>` XML
tags before sending to the Claude SDK. This duplicates work the CLI subprocess already
handles natively — the CLI intercepts `/skill_name` prompts, loads skill bodies, and
injects them into the conversation without any help from the bot.

## Evidence

Experiments confirmed two key behaviors:

1. **`get_server_info()`** returns all available commands/skills (87 entries across
   bundled, project, plugin, and MCP prompt sources) with name, description, and
   argument hints — immediately after `connect()`, no query needed.

2. **The CLI intercepts `/skill_name` prompts natively:**
   - Known skills: CLI loads the body, injects it, Claude executes a full agentic turn.
   - Unknown skills: CLI returns `"Unknown skill: X"` with zero tokens/cost.
   - Plain text: Normal LLM inference, no interception.

## Design

### Approach: Full Pass-Through

Replace all custom skill discovery/injection with SDK native handling.

### Skill Invocation

**Before:**
```
/brainstorm topic → discover_skills() → load_skill_body() → resolve_skill_prompt()
→ wrap in <skill-invocation> XML → query(wrapped_prompt)
```

**After:**
```
/brainstorm topic → check cached skill list → query("/brainstorm topic")
```

The orchestrator checks the message against a cached skill list (from
`get_server_info()`). If it's a known skill, the raw `/command args` message is passed
verbatim to `UserClient.submit()`. The CLI handles body loading, placeholder resolution,
and prompt injection.

If the CLI returns `"Unknown skill: X"` in the `ResultMessage.result`, the orchestrator
surfaces a friendly error to the user.

### Skill Discovery (`/commands` Menu)

**Before:** `discover_skills()` scans 5 filesystem directories on every `/commands` call.

**After:** `UserClient` calls `get_server_info()` once after `connect()` and caches the
commands list. The `/commands` handler reads from this cache.

Cache lifecycle:
- Populated on `connect()`
- Cleared on `disconnect()`/idle timeout
- If no active connection, return a "start a session first" message

This gives richer data than before — MCP prompts and bundled CLI commands that
`discover_skills()` never returned.

### File Changes

**Modified:**

1. `src/claude/user_client.py` — Call `get_server_info()` after `connect()`, cache
   commands, expose `get_available_skills()` method.
2. `src/bot/orchestrator.py` — Simplify `agentic_text` skill handling to pass-through.
   Simplify `_agentic_callback` `skill:` handler. Update `/commands` to use cached list.
   Handle "Unknown skill" error.
3. `src/claude/client_manager.py` — Expose skill listing through the manager.

**Deleted/gutted:**

4. `src/skills/loader.py` — Remove `discover_skills()`, `load_skill_body()`,
   `resolve_skill_prompt()`, `_parse_frontmatter()`. Delete if nothing else imports.

**Tests:**

5. Remove tests for old skill loading logic.
6. Add tests for: cached skill list, pass-through invocation, "Unknown skill" handling.

**Unchanged:**

- `src/claude/options.py` — `tools` and `setting_sources` stay as-is.
- `src/claude/stream_handler.py` — No changes.
- Security middleware — Still validates input before reaching the orchestrator.
