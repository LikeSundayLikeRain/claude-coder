# Remove Rate Limiting and Cost Tracking — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Remove all user-facing rate limiting, cost tracking, and budget enforcement from the bot for personal-use deployment.

**Architecture:** Delete rate limit middleware, engine, cost_tracking table, and config fields. Remove all consumer references. Keep Telegram API rate limiting (AIORateLimiter, _rate_limited_send) intact.

**Tech Stack:** Python 3.12, python-telegram-bot, pytest, aiosqlite

---

### Task 1: Delete rate limit files

**Files:**
- Delete: `src/bot/middleware/rate_limit.py`
- Delete: `src/security/rate_limiter.py`
- Delete: `tests/unit/test_security/test_rate_limiter.py`
- Delete: `tests/unit/test_bot/test_core_rate_limiter.py`

**Step 1: Delete the files**

```bash
rm src/bot/middleware/rate_limit.py
rm src/security/rate_limiter.py
rm tests/unit/test_security/test_rate_limiter.py
rm tests/unit/test_bot/test_core_rate_limiter.py
```

**Step 2: Commit**

```bash
git add -u
git commit -m "refactor: delete rate limiter and cost tracking files"
```

---

### Task 2: Remove rate limit middleware registration

**Files:**
- Modify: `src/bot/middleware/__init__.py`
- Modify: `src/bot/core.py:97-128`

**Step 1: Update middleware __init__.py**

Remove `rate_limit_middleware` import and export. Result:

```python
"""Bot middleware for authentication and security."""

from .auth import auth_middleware
from .security import security_middleware

__all__ = ["auth_middleware", "security_middleware"]
```

**Step 2: Update core.py _add_middleware**

Remove the `rate_limit_middleware` import (line 100) and the rate limit handler registration (lines 120-126). Result:

```python
def _add_middleware(self) -> None:
    """Add middleware to application."""
    from .middleware.auth import auth_middleware
    from .middleware.security import security_middleware

    # Middleware runs in order of group numbers (lower = earlier)
    # Security middleware first (validate inputs)
    self.app.add_handler(
        MessageHandler(
            filters.ALL, self._create_middleware_handler(security_middleware)
        ),
        group=-3,
    )

    # Authentication second
    self.app.add_handler(
        MessageHandler(
            filters.ALL, self._create_middleware_handler(auth_middleware)
        ),
        group=-2,
    )

    logger.info("Middleware added to bot")
```

**Step 3: Run tests to verify no import errors**

```bash
uv run pytest tests/unit/test_bot/ -v --tb=short -q
```

**Step 4: Commit**

```bash
git add src/bot/middleware/__init__.py src/bot/core.py
git commit -m "refactor: remove rate limit middleware registration"
```

---

### Task 3: Remove RateLimiter from main.py and security package

**Files:**
- Modify: `src/main.py:35,131,180`
- Modify: `src/security/__init__.py`

**Step 1: Update main.py**

Remove line 35 (`from src.security.rate_limiter import RateLimiter`).
Remove line 131 (`rate_limiter = RateLimiter(config)`).
Remove line 180 (`"rate_limiter": rate_limiter,` from dependencies dict).

**Step 2: Update security/__init__.py**

Remove `from .rate_limiter import RateLimitBucket, RateLimiter` import.
Remove `"RateLimiter"` and `"RateLimitBucket"` from `__all__`.
Update module docstring to remove rate limiting mention.

Result:

```python
"""Security framework for Claude Code Telegram Bot.

This module provides comprehensive security features including:
- Multi-layer authentication (whitelist and token-based)
- Path traversal and injection prevention
- Input validation and sanitization
- Security audit logging

Key Components:
- AuthenticationManager: Main authentication system
- SecurityValidator: Input validation and path security
- AuditLogger: Security event logging
"""

from .audit import AuditEvent, AuditLogger
from .auth import (
    AuthenticationManager,
    AuthProvider,
    TokenAuthProvider,
    UserSession,
    WhitelistAuthProvider,
)
from .validators import SecurityValidator

__all__ = [
    "AuthProvider",
    "WhitelistAuthProvider",
    "TokenAuthProvider",
    "AuthenticationManager",
    "UserSession",
    "SecurityValidator",
    "AuditLogger",
    "AuditEvent",
]
```

**Step 3: Run tests**

```bash
uv run pytest tests/ -v --tb=short -q 2>&1 | tail -10
```

**Step 4: Commit**

```bash
git add src/main.py src/security/__init__.py
git commit -m "refactor: remove RateLimiter from main and security package"
```

---

### Task 4: Remove rate limit config fields

**Files:**
- Modify: `src/config/settings.py:89-91,133-142`
- Modify: `src/config/environments.py:36,59-60`
- Modify: `src/config/loader.py:146-158`

**Step 1: Update settings.py**

Remove `claude_max_cost_per_user` field (lines 89-91).
Remove `rate_limit_requests`, `rate_limit_window`, `rate_limit_burst` fields (lines 133-142).
Also check for and remove any DEFAULT_* constants for these fields.

**Step 2: Update environments.py**

Remove `rate_limit_requests: int = 1000` from TestingConfig (line 36).
Remove `claude_max_cost_per_user: float = 5.0` and `rate_limit_requests: int = 5` from ProductionConfig (lines 59-60).

**Step 3: Update loader.py**

Remove the rate limit validation block (lines 146-151):
```python
if settings.rate_limit_requests < 0:
    raise InvalidConfigError("rate_limit_requests must be non-negative")
if settings.rate_limit_window <= 0:
    raise InvalidConfigError("rate_limit_window must be positive")
```

Remove cost limit validation block (lines 156-158):
```python
if settings.claude_max_cost_per_user < 0:
    raise InvalidConfigError("claude_max_cost_per_user must be non-negative")
```

**Step 4: Run config tests**

```bash
uv run pytest tests/unit/test_config.py tests/unit/test_environments.py -v --tb=short
```

**Step 5: Commit**

```bash
git add src/config/settings.py src/config/environments.py src/config/loader.py
git commit -m "refactor: remove rate limit and cost tracking config fields"
```

---

### Task 5: Remove rate_limiter from bot handlers

**Files:**
- Modify: `src/bot/orchestrator.py:681-684,1213-1215`
- Modify: `src/bot/handlers/command.py:870-877`
- Modify: `src/bot/handlers/message.py:20,300-312,542-585`
- Modify: `src/bot/handlers/callback.py:647-654`

**Step 1: Update orchestrator.py**

Remove `rate_limiter` check from the `/status` handler (lines 681-684).
Remove `rate_limiter` check from `agentic_text` (lines 1213-1215).

**Step 2: Update command.py**

Remove `rate_limiter` usage from the classic `/status` command (lines 870-877).

**Step 3: Update message.py**

Remove `from ...security.rate_limiter import RateLimiter` import (line 20).
Remove `rate_limiter` checks from text message handler (lines 300-312).
Remove `rate_limiter` checks from file handler (lines 542-585).

**Step 4: Update callback.py**

Remove `rate_limiter` from status display callback (lines 647-654).

**Step 5: Run handler tests**

```bash
uv run pytest tests/unit/test_bot/ tests/unit/test_orchestrator.py -v --tb=short -q
```

**Step 6: Commit**

```bash
git add src/bot/orchestrator.py src/bot/handlers/command.py src/bot/handlers/message.py src/bot/handlers/callback.py
git commit -m "refactor: remove rate_limiter references from bot handlers"
```

---

### Task 6: Remove cost_tracking table and audit method

**Files:**
- Modify: `src/storage/database.py:123-140,352`
- Modify: `src/security/audit.py:289-300`

**Step 1: Update database.py**

Remove the `cost_tracking` CREATE TABLE statement (lines 123-140).
Remove the `DROP TABLE IF EXISTS cost_tracking` from migration/reset (line 352).

**Step 2: Update audit.py**

Remove the `log_rate_limit_exceeded` method (lines 289-300).

**Step 3: Run storage and audit tests**

```bash
uv run pytest tests/unit/test_storage/ tests/unit/test_security/test_audit.py -v --tb=short
```

**Step 4: Commit**

```bash
git add src/storage/database.py src/security/audit.py
git commit -m "refactor: remove cost_tracking table and rate limit audit method"
```

---

### Task 7: Update tests

**Files:**
- Modify: `tests/unit/test_bot/test_middleware.py` — remove rate limit middleware tests
- Modify: `tests/unit/test_environments.py` — remove rate limit field assertions
- Modify: `tests/unit/test_orchestrator.py` — remove rate_limiter from mock bot_data
- Modify: `tests/unit/test_security/test_audit.py` — remove log_rate_limit_exceeded test
- Modify: `tests/unit/test_storage/test_database.py` — remove cost_tracking table assertion
- Modify: any other test fixtures referencing rate_limiter in bot_data

**Step 1: Update each test file**

Remove rate_limiter from all mock `bot_data` dicts (search for `"rate_limiter"`).
Remove test methods/classes that test rate limiting behavior.
Remove assertions about `cost_tracking` table, `rate_limit_*` config fields.

**Step 2: Run full test suite**

```bash
uv run pytest tests/ -v --tb=short -q
```

Expected: All tests pass with no rate limiting references.

**Step 3: Commit**

```bash
git add tests/
git commit -m "test: remove rate limiting and cost tracking test references"
```

---

### Task 8: Update CLAUDE.md and verify

**Files:**
- Modify: `CLAUDE.md`

**Step 1: Update CLAUDE.md**

Remove rate limiting from the Security Model description (5-layer → 4-layer, remove "rate limiting (token bucket)").
Remove `rate_limit_requests`, `rate_limit_window`, `rate_limit_burst`, `claude_max_cost_per_user` from Configuration section.
Remove `RateLimiter` from the security module description.

**Step 2: Run full test suite + lint**

```bash
uv run pytest tests/ -q --tb=short
make lint
```

Expected: All tests pass, no lint errors.

**Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: remove rate limiting from CLAUDE.md"
```
