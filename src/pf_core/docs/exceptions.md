# Exceptions

pf_core provides a two-branch exception hierarchy that separates expected domain failures from actual errors. This distinction drives logging behavior, HTTP status codes, and error page rendering.

## Hierarchy

Services raise domain exceptions. The HTTP layer translates them automatically.

```
Exception
├── FlowException              — expected domain failures (not bugs)
│   ├── NotFoundError           → 404  entity does not exist
│   ├── InvalidInputError       → 422  bad data from caller   (also a ValueError)
│   ├── PreconditionError       → 409  state conflict         (also a RuntimeError)
│   ├── ActionNotAllowedError   → 403  business rule says no
│   ├── ConfigurationError      → 500  missing config = broken app
│   │   └── PipelineNotRegisteredError → 500  validator pipeline not registered
│   └── CostBudgetExceeded      → 429  spend cap hit (lives in pf_core.budget)
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
    NotFoundError,
    InvalidInputError,
    PreconditionError,
    ActionNotAllowedError,
    ConfigurationError,
)

# Entity doesn't exist → 404
raise NotFoundError("Item", item_id)

# Bad user input → 422
raise InvalidInputError("Date must be in YYYY-MM-DD format")

# State conflict → 409
raise PreconditionError("Task 42 is already completed")

# Business rule says no → 403
raise ActionNotAllowedError("Invoice is locked for editing")

# Missing config → 500 (broken app, not user's fault)
raise ConfigurationError("DATABASE_URL not set")
```

**Logging**: Flow exceptions log at `WARNING` level, no traceback (these are expected). This includes `ConfigurationError` — `log_exception()` keys off the `FlowException` base class, so every subclass logs at `WARNING` without a traceback.

**HTTP**: Each named subclass above has its own `app_factory` handler mapping it to the status code shown. Any other `FlowException` subclass falls through to a catch-all handler that returns **400**.

### Builtin bases: `ValueError` and `RuntimeError`

`InvalidInputError` also subclasses `ValueError`, and `PreconditionError` subclasses `RuntimeError` — the builtins they replace. Swapping a builtin raise for one of them is therefore not a breaking change:

- An existing `except ValueError` / `except RuntimeError`, or `pytest.raises(ValueError)`, still catches it.
- pydantic converts an `InvalidInputError` raised inside a validator into a `ValidationError`, as it does any `ValueError`; argparse turns one raised by a `type=` converter into a usage error, and click one raised by a typer `parser=` (`run_cli` answers that one as the `InvalidInputError`: its message, exit 1).
- `FlowException` comes before the builtin in the MRO, and Starlette picks a handler by walking the MRO, so the 422 / 409 mapping (or a consumer's own `FlowException` handler) wins over any `ValueError` / `RuntimeError` handler an app registers.

The converse also holds: a broad `except ValueError` now catches `InvalidInputError` raised anywhere in its `try` block. Put an `except InvalidInputError` (or `FlowException`) clause first where the two must be told apart, and test for the class itself rather than `not isinstance(exc, ValueError)`.

A subclass that also names the builtin must list the pf-core class first: `class BadDate(InvalidInputError, ValueError)` defines, while `class BadDate(ValueError, InvalidInputError)` raises `TypeError` (no consistent method resolution order). The same holds for `PreconditionError` and `RuntimeError`.

**A validator's `InvalidInputError` arrives wrapped.** Because pydantic converts it, a model built with bad data raises a `ValidationError` — which is not a `FlowException` — where the `InvalidInputError` used to escape on its own, ending validation. `unwrap_flow_exception(exc)` gives back the one that would have escaped: the first `InvalidInputError` among the errors, whatever else is wrong beside it (before, it escaped regardless), or `None` when there is none. A `FlowException` class that is a `ValueError` or `AssertionError` in its own right was always wrapped, so it is not given back. pf-core's boundaries unwrap it: the web app factory answers it — in pydantic's `ValidationError` or FastAPI's request, websocket or response validation error — with the handler for its class (422 for `InvalidInputError`, or whatever the app registered for its class); `pf_core.cli.run_cli` prints its message and exits 1; `log_exception` logs it as the `InvalidInputError` (WARNING, `APP-InvalidInputError`); `parallel.resilient` matches `catch` against it and records its message; `llm.router.call_with_fallback` does not fall back on it. The two places that turn a `ValidationError` into something else let it escape as itself instead, as it did: a job kind's `validate_inputs` / `validate_outputs`, and `PydanticValidator.validate_shape`. A validation error with no such error is handled as before: FastAPI's own answer, the app's `ValueError` handler, a 500 or a traceback, whichever answered it without pf-core ([web.md](web.md#error-handling)). Your own `except FlowException` around a model construction does not see it; call `unwrap_flow_exception(exc)` there:

```python
from pydantic import ValidationError
from pf_core.exceptions import unwrap_flow_exception

try:
    window = Window(start=raw)
except ValidationError as exc:
    flow = unwrap_flow_exception(exc)  # the validator's InvalidInputError, or None
    ...
```

Validation also no longer stops at it: pydantic runs the validators after it, and in a union field (`A | B`) it tries `B` when `A`'s validator raised — which can succeed where the error used to escape. Keep side effects out of validators, and raise `InvalidInputError` in one only where a failed branch should fail the field. Recorders that store an exception's class and message — a job's `error_class`, a failed LLM run's — store the `ValidationError`.

`CostBudgetExceeded` is defined in `pf_core.budget` (raised by `check_budget()` — see [cost-budget.md](cost-budget.md)), not `pf_core.exceptions`, but it is a `FlowException` and the web layer maps it to **429**.

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

Every shipped backend client error — `OpenRouterError`, `AnthropicError`, `ClaudeCodeError` — subclasses `ClientError`, so `except ClientError` catches a transport failure from any backend.

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
