# Logging

Structured logging via [structlog](https://www.structlog.org/). Console output is colored for development; file output is JSON-lines for production log ingestion.

## Setup

Call once at startup, or let it auto-configure on first `get_logger()` call:

```python
from pf_core.log import setup_logging

setup_logging(level="INFO", log_file="logs/app.jsonl")
```

Or configure via environment variables (no code needed):

```bash
LOG_LEVEL=DEBUG
LOG_FILE=logs/app.jsonl
```

`setup_logging()` is idempotent — safe to call multiple times.

### Which loggers are covered

By default the handlers attach to the **root logger**, so every logger
propagates to them no matter what your top-level package is named. A project
that adopts pf-core after the fact (its package is `myproject`, not `app`)
needs no special wiring — `get_logger(__name__)` output reaches the handlers,
and `log_exception()` logs under the same tree.

To scope the handlers to one named logger instead (e.g. to isolate your app's
logs from third-party libraries), pass `app_logger_name`:

```python
setup_logging(app_logger_name="myproject")   # handlers on the "myproject" logger
```

With a named logger, only `myproject.*` records reach the handlers, and
`log_exception()` logs under `myproject.exceptions`.

## Getting a logger

```python
from pf_core.log import get_logger

logger = get_logger(__name__)

logger.info("search_started", model="sonar-pro", task_id=42)
logger.warning("retry", attempt=3, reason="timeout")
logger.error("search_failed", task_id=42)
```

All key-value pairs are included as structured fields in the log record.

## Context binding

Attach fields to all log records within a scope:

```python
from pf_core.log import log_context

with log_context(task_id=42, section_name="intro"):
    logger.info("search_started")    # includes task_id=42, section_name="intro"
    logger.info("search_complete")   # same context
# Context cleared after the block
```

Context is context-local via `structlog.contextvars`, so it works correctly with concurrent requests in FastAPI.

## Logging exceptions

`log_exception()` handles the two exception branches differently:

```python
from pf_core.log import log_exception

try:
    do_something()
except AppError as e:
    log_exception(e, message_prepend="pipeline failed", event_prefix="COMP")
```

| Exception type | Log level | Traceback | Event key |
|---------------|-----------|-----------|-----------|
| `AppError` | ERROR | Full traceback | `COMP-SearchError` |
| `FlowException` | WARNING | No traceback | `COMP-ConfigurationError` |
| Other | ERROR | No traceback | `COMP-ValueError` |

The event key format `{prefix}-{ClassName}` is designed for grep:

```bash
grep COMP-SearchError logs/app.jsonl
```

## Verbose helper

For operations with a `--verbose` flag:

```python
from pf_core.log import log_verbose

log_verbose(logger, "summarized text", verbose=verbose, tokens=1200)
# verbose=True  → logger.info(...)
# verbose=False → logger.debug(...)
```

## Output formats

**Console** (development): colored, human-readable via `structlog.dev.ConsoleRenderer`.

**File** (production): JSON-lines via `structlog.processors.JSONRenderer`. Rotates at 10MB, keeps 5 backups.

```json
{"event": "search_started", "model": "sonar-pro", "task_id": 42, "level": "info", "timestamp": "2026-04-12T14:30:00Z", "logger": "app.services.search"}
```
