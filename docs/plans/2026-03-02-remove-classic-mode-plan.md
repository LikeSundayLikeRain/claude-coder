# Remove Classic Mode — Implementation Plan

Reference: `docs/plans/2026-03-02-remove-classic-mode-design.md`

## Phase 1: Delete Classic-Only Files

### Task 1.1: Delete classic handler files
- [ ] Delete `src/bot/handlers/command.py`
- [ ] Delete `src/bot/handlers/message.py`
- [ ] Delete `src/bot/handlers/callback.py`
- [ ] Clean up `src/bot/handlers/__init__.py` (remove any exports referencing deleted modules)

### Task 1.2: Delete classic facade
- [ ] Delete `src/claude/facade.py`
- [ ] Clean up `src/claude/__init__.py` (remove `ClaudeIntegration` export)

### Task 1.3: Delete classic tests
- [ ] Delete `tests/unit/test_claude/test_facade.py`

## Phase 2: Remove Mode Branching

### Task 2.1: Clean up orchestrator (`src/bot/orchestrator.py`)
- [ ] Update module docstring — remove "agentic vs classic" framing
- [ ] `register_handlers()`: remove if/else, call `_register_agentic_handlers()` directly
- [ ] Delete `_register_classic_handlers()` method entirely (lines 298-343)
- [ ] `get_bot_commands()`: remove `if not self.settings.agentic_mode` block (lines 347-365)
- [ ] Remove `ClaudeIntegration` fallback in `_run_claude_query()` (~line 958-966)

### Task 2.2: Remove config flag (`src/config/settings.py`)
- [ ] Delete `agentic_mode` field from `Settings`

### Task 2.3: Remove feature flag (`src/config/features.py`)
- [ ] Delete `agentic_mode_enabled` property
- [ ] Remove `"agentic_mode"` entry from features dict

### Task 2.4: Clean up security middleware (`src/bot/middleware/security.py`)
- [ ] Remove `agentic_mode` variable and classic-only validation block (lines 46-50)

### Task 2.5: Clean up feature registry (`src/bot/features/registry.py`)
- [ ] Remove 3 `agentic_mode` guard blocks (lines 46, 54, 62)
- [ ] Remove any now-dead classic-only feature registrations

### Task 2.6: Clean up main.py (`src/main.py`)
- [ ] Remove `from src.claude import ClaudeIntegration` import
- [ ] Remove `claude_integration = ClaudeIntegration(...)` instantiation
- [ ] Remove `"claude_integration"` from deps dicts
- [ ] Remove `claude_integration.shutdown()` call

## Phase 3: Migrate AgentHandler

### Task 3.1: Update AgentHandler (`src/events/handlers.py`)
- [ ] Change import: `ClaudeSDKManager` instead of `ClaudeIntegration`
- [ ] Update constructor: accept `sdk_manager: ClaudeSDKManager` instead of `claude_integration`
- [ ] Update `handle_webhook()`: call `sdk_manager.execute_command()` instead of `claude.run_command()`
- [ ] Update `handle_scheduled()`: same migration
- [ ] Update docstrings

### Task 3.2: Update AgentHandler wiring (`src/main.py`)
- [ ] Pass `sdk_manager` to `AgentHandler()` instead of `claude_integration`

## Phase 4: Rename agentic_* Methods

### Task 4.1: Rename methods in orchestrator (`src/bot/orchestrator.py`)
- [ ] `_register_agentic_handlers` → `_register_handlers`
- [ ] `agentic_start` → `handle_start`
- [ ] `agentic_new` → `handle_new`
- [ ] `agentic_status` → `handle_status`
- [ ] `agentic_compact` → `handle_compact`
- [ ] `agentic_text` → `handle_text`
- [ ] `agentic_attachment` → `handle_attachment`
- [ ] `agentic_repo` → `handle_repo`
- [ ] `agentic_commands` → `handle_commands`
- [ ] `agentic_remove` → `handle_remove`
- [ ] `agentic_history` → `handle_history`
- [ ] `agentic_resume` → `handle_resume`
- [ ] `_agentic_callback` → `_handle_callback`

### Task 4.2: Update handler.__name__ references
- [ ] Line 74: update `"agentic_remove"` → `"handle_remove"` in management bypass check
- [ ] Line 75: update `"agentic_start"` → `"handle_start"` in start bypass check

## Phase 5: Clean Up Tests

### Task 5.1: Fix test_orchestrator.py
- [ ] Remove `classic_settings` fixture
- [ ] Remove `test_classic_registers_14_commands` and any other classic-specific tests
- [ ] Update remaining tests that reference `agentic_mode`

### Task 5.2: Remove `agentic_mode=True` from all test configs
- [ ] `tests/unit/test_bot/test_repo_browser_integration.py`
- [ ] `tests/unit/test_bot/test_sessions_command.py`
- [ ] `tests/unit/test_bot/test_compact_command.py`
- [ ] `tests/unit/test_bot/test_commands_command.py`
- [ ] `tests/unit/test_bot/test_middleware.py`
- [ ] `tests/unit/test_directory_persistence.py` (~15 occurrences)
- [ ] `tests/unit/test_orchestrator.py`
- [ ] Any test helpers/conftest with `agentic_mode` parameter

### Task 5.3: Update tests referencing renamed methods
- [ ] Update any test mocking/asserting `agentic_*` method names

## Phase 6: Verify

### Task 6.1: Run full verification
- [ ] `uv run pytest tests/ -x -q` — all tests pass
- [ ] `uv run mypy src` — no type errors
- [ ] `uv run black --check src tests` — formatting OK
- [ ] `uv run isort --check src tests` — imports OK

### Task 6.2: Sanity check
- [ ] Grep for any remaining `classic` references in src/
- [ ] Grep for any remaining `agentic_mode` references in src/ and tests/
- [ ] Grep for any remaining `ClaudeIntegration` references
- [ ] Verify `src/bot/handlers/` only contains `__init__.py`

## Execution Notes

- Phases 1-3 can be done in parallel (independent deletions/edits)
- Phase 4 (rename) should come after Phase 2 (mode removal) to avoid merge conflicts
- Phase 5 (tests) depends on all prior phases
- Phase 6 (verify) is the final gate
