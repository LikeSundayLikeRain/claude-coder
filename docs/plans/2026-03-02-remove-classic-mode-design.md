# Remove Classic Mode

## Goal

Remove the unused classic mode from the codebase, leaving only the agentic mode as the single operating mode. Rename `agentic_*` prefixes to plain names since the distinction no longer exists.

## Motivation

- Classic mode is unused — agentic mode is always enabled
- ~3,400 lines of dead code (handlers, facade, callbacks)
- The `AGENTIC_MODE` config flag and branching add unnecessary complexity
- Method names like `agentic_start` are redundant when there's only one mode

## Scope

**~3,600 lines removed, ~20 lines rewritten.**

### Phase 1: Delete Classic-Only Files

| File | Lines | Reason |
|------|-------|--------|
| `src/bot/handlers/command.py` | ~1,035 | 13 classic commands |
| `src/bot/handlers/message.py` | ~900 | Classic text/file/photo handlers |
| `src/bot/handlers/callback.py` | ~1,295 | Classic inline keyboard callbacks |
| `src/claude/facade.py` | ~136 | `ClaudeIntegration` wrapper |
| `tests/unit/test_claude/test_facade.py` | ~50 | Tests for deleted facade |

### Phase 2: Remove Mode Branching

**`src/bot/orchestrator.py`:**
- Remove `register_handlers()` mode branch (line 235-238)
- Remove `_register_classic_handlers()` method (lines 298-343)
- Remove classic command list in `get_bot_commands()` (lines 347-365)
- Remove `ClaudeIntegration` fallback (~line 958-966)
- Update module docstring

**`src/config/settings.py`:**
- Remove `agentic_mode` field

**`src/config/features.py`:**
- Remove `agentic_mode_enabled` property and its entry in the features dict

**`src/bot/middleware/security.py`:**
- Remove `agentic_mode` check and the classic-only input validation block (lines 46-50)

**`src/bot/features/registry.py`:**
- Remove 3 `agentic_mode` guard blocks (classic-only feature registration)

**`src/main.py`:**
- Remove `ClaudeIntegration` import and instantiation
- Remove from deps dicts
- Remove `shutdown()` call

### Phase 3: Migrate AgentHandler

**`src/events/handlers.py`:**
- Change constructor to accept `ClaudeSDKManager` instead of `ClaudeIntegration`
- Update `handle_webhook()` and `handle_scheduled()` to call `sdk_manager.execute_command()`

**`src/main.py`:**
- Update `AgentHandler` construction to pass `sdk_manager`

### Phase 4: Rename agentic_* Methods

**`src/bot/orchestrator.py`** — drop `agentic_` prefix:

| Old | New |
|-----|-----|
| `_register_agentic_handlers` | `_register_handlers` |
| `agentic_start` | `handle_start` |
| `agentic_new` | `handle_new` |
| `agentic_status` | `handle_status` |
| `agentic_compact` | `handle_compact` |
| `agentic_text` | `handle_text` |
| `agentic_attachment` | `handle_attachment` |
| `agentic_repo` | `handle_repo` |
| `agentic_commands` | `handle_commands` |
| `agentic_remove` | `handle_remove` |
| `agentic_history` | `handle_history` |
| `agentic_resume` | `handle_resume` |
| `_agentic_callback` | `_handle_callback` |

Update `handler.__name__` checks (lines 74-75) to match new names.

### Phase 5: Clean Up Tests

- Delete `tests/unit/test_claude/test_facade.py`
- `tests/unit/test_orchestrator.py` — remove `classic_settings` fixture, classic test cases
- All tests passing `agentic_mode=True` — remove the kwarg
- Update any test helper (`create_test_config`) to drop `agentic_mode` param

### Phase 6: Verify

- Run full test suite — all 712 tests must pass (minus deleted classic tests)
- Run `make lint` — no type errors or import failures
- Verify webhook handler still works (AgentHandler migration)

## What We Keep

- `ClaudeSDKManager` (`src/claude/sdk_integration.py`) — shared by agentic `UserClient` and migrated `AgentHandler`
- All agentic infrastructure: `ClientManager`, `UserClient`, `OptionsBuilder`, `SessionResolver`, `StreamHandler`, `ProgressMessageManager`
- All security, storage, events, API, scheduler infrastructure
- `src/bot/handlers/__init__.py` — clean up imports

## Risks

- **Low**: Clean separation between modes means deletions are safe
- **Medium**: `AgentHandler` migration needs careful testing (webhook → `ClaudeSDKManager`)
- **Low**: Test cleanup is mechanical (remove kwargs, delete fixtures)
