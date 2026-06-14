# Logging

All operational logging uses `pf_core.log` (structlog). Never use raw `print()` for operational output in services, repos, or orchestrators — `print()` is acceptable only in CLI entry points for user-facing messages.

- **Get a logger:** `logger = get_logger(__name__)`. Log structured events — `logger.info("event_key", field=value)` — not interpolated strings.
- **Log exceptions** with `log_exception(exc, message_prepend=..., additional_context=...)` — never `logger.exception()` directly.
- **Bind context** for a scope with `log_context(...)` so every record in the block carries the same fields.

## Log levels (by intent)

| Level | Use for |
|-------|---------|
| DEBUG | Verbose diagnostics (file only). |
| INFO | Normal operational events (task started, step complete, cache hit). |
| WARNING | Expected domain failures (`FlowException`) — not bugs, but worth noting. |
| ERROR | Actual errors (`AppError`) — LLM/DB failures, unexpected states. |

Setup, env vars (`LOG_LEVEL`, `LOG_FILE`), the `app_logger_name` option, and the console / JSON-lines output formats are documented in `docs/logging.md`.
