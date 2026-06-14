# Exceptions

pf_core provides a two-branch exception hierarchy that separates expected domain failures from actual errors. This distinction drives logging behavior, HTTP status codes, and error page rendering.

## Hierarchy

Services raise domain exceptions. The HTTP layer translates them automatically.

```
Exception
├── FlowException              — expected domain failures (not bugs)
│   ├── NotFoundError           → 404  entity does not exist
│   ├── InvalidInputError       → 422  bad data from caller
│   ├── PreconditionError       → 409  state conflict
│   ├── ActionNotAllowedError   → 403  business rule says no
│   └── ConfigurationError      → 500  missing config = broken app
│       └── PipelineNotRegisteredError → 500  validator pipeline not registered
│
└── AppError                   — actual errors (unexpected failures)
    ├── ClientError             → 500  external API call failed
    ├── DataError               → 500  database read/write failure
    └── TaskError               → 500  task-level failure (carries running_log)
```

All `AppError` subclasses map to **500** — the `app_factory` registers one handler on `AppError`, not per subclass. (The arrows above name the *intent* of each error, not distinct status codes.)

## FlowException — expected failures

These are **not bugs**. They represent known conditions where an operation cannot proceed. The web framework maps most of them to 4xx responses (`ConfigurationError` is the exception — it's a 500, since missing config is a server problem, not the caller's).

```python
from pf_core.exceptions import (
    NotFoundError, InvalidInputError, PreconditionError,
    ActionNotAllowedError, ConfigurationError,
)

# Entity doesn't exist → 404
raise NotFoundError("Item", item_id)

# Bad user input → 422
raise InvalidInputError("Date must be in YYYY-MM-DD format")

# State conflict → 409
raise PreconditionError("Task 42 is already completed")

# Business rule says no → 403
raise ActionNotAllowedError("Section is locked for editing")

# Missing config → 500 (broken app, not user's fault)
raise ConfigurationError("DATABASE_URL not set")
```

**Logging**: Flow exceptions log at `WARNING` level, no traceback (these are expected). This includes `ConfigurationError` — `log_exception()` keys off the `FlowException` base class, so every subclass logs at `WARNING` without a traceback.

**HTTP**: Each named subclass above has its own `app_factory` handler mapping it to the status code shown. Any other `FlowException` subclass falls through to a catch-all handler that returns **400**.

## AppError — actual errors

These are **unexpected failures** that need investigation. They carry a structured `context` dict for log enrichment and support exception chaining via `cause`.

```python
from pf_core.exceptions import AppError, ClientError, DataError

raise AppError(
    "OpenRouter timed out",
    context={"task_id": 42, "model": "gpt-4o"},
    cause=original_exception,
)

raise DataError(
    "Failed to insert entry",
    context={"entry_id": "item_001", "section_id": 3},
)
```

**Logging**: `ERROR` level with full traceback and merged context chain.

**HTTP**: `500 Internal Server Error`. The actual error message is logged but not shown to the user.

## TaskError — with running log

For pipeline tasks that accumulate work before failing:

```python
from pf_core.exceptions import TaskError

raise TaskError(
    "Search timed out after 3 retries",
    context={"task_id": task.id, "model": "sonar-pro"},
    running_log=notes_collected_so_far,
    cause=e,
)
```

The `running_log` field preserves partial work so the caller can save progress before the error propagates.

## Project-specific subclasses

Projects define their own error types:

```python
from pf_core.exceptions import AppError, FlowException

class SearchError(AppError):
    """LLM search call failed."""

class ExtractError(AppError):
    """Extraction pipeline error."""

class DataNotFoundError(FlowException):
    """Required data not loaded."""
```

## Context chain merging

When exceptions are chained (`cause=e`), `log_exception()` merges context from the entire chain:

```python
try:
    resp = client.chat(messages, model=model)
except ClientError as e:
    raise SearchError(
        "Search failed",
        context={"task_id": 42},
        cause=e,  # e.context has {"model": "sonar-pro", "timeout": 300}
    )
```

The logged context will contain both `task_id` and `model` — inner context fills gaps, outer context wins on duplicates.

## Usage with log_exception

```python
from pf_core.log import log_exception

try:
    do_something()
except AppError as e:
    log_exception(e, message_prepend="search failed", event_prefix="COMP")
    # Logs: COMP-SearchError with full traceback + merged context
```

See [logging.md](logging.md) for details on `log_exception()`.
