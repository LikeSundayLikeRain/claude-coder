# Remove Rate Limiting and Cost Tracking

## Context

This is a personal-use fork. The user-facing rate limiting (request throttling, cost budgets, burst protection) adds complexity without value for a single-user deployment. Telegram API rate limiting (outbound call pacing) is unrelated and stays.

## Approach

Full removal (Approach A): delete rate limiting middleware, engine, cost_tracking table, config fields, and all references. Consumers already guard with `if rate_limiter:` so removal is safe.

## Files to delete

- `src/bot/middleware/rate_limit.py` — rate_limit, cost_tracking, burst_protection middlewares
- `src/security/rate_limiter.py` — RateLimiter + RateLimitBucket classes
- `tests/unit/test_security/test_rate_limiter.py`
- `tests/unit/test_bot/test_core_rate_limiter.py`

## Files to modify

### Bot layer
- `src/bot/middleware/__init__.py` — remove rate_limit_middleware export
- `src/bot/core.py` — remove rate_limit_middleware registration (keep AIORateLimiter)
- `src/bot/orchestrator.py` — remove rate_limiter checks from /status and agentic_text
- `src/bot/handlers/command.py` — remove rate_limiter from /status
- `src/bot/handlers/message.py` — remove RateLimiter import and all checks
- `src/bot/handlers/callback.py` — remove rate_limiter from status display

### Config layer
- `src/config/settings.py` — remove rate_limit_requests, rate_limit_window, rate_limit_burst, claude_max_cost_per_user
- `src/config/environments.py` — remove rate limit overrides
- `src/config/loader.py` — remove rate limit validation

### Infrastructure
- `src/main.py` — remove RateLimiter import, instantiation, bot_data injection
- `src/security/__init__.py` — remove RateLimiter, RateLimitBucket exports
- `src/security/audit.py` — remove log_rate_limit_exceeded
- `src/storage/database.py` — remove cost_tracking table

### Tests
- `tests/unit/test_bot/test_middleware.py` — remove rate limit tests
- `tests/unit/test_environments.py` — remove rate limit assertions
- `tests/unit/test_orchestrator.py` — remove rate_limiter from mocks
- `tests/unit/test_security/test_audit.py` — remove rate limit test
- `tests/unit/test_storage/test_database.py` — remove cost_tracking assertion
- Various test fixtures — remove rate_limiter from bot_data mocks

### Documentation
- `CLAUDE.md` — remove rate limiting/cost tracking from Security Model and Configuration sections

## What stays

- `AIORateLimiter` in `src/bot/core.py` (Telegram API outbound rate limiting)
- `_rate_limited_send` in `src/notifications/service.py` (Telegram delivery pacing)
- SDK-level rate_limit_event handling in `src/claude/sdk_integration.py`
