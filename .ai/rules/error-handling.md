# Error Handling

All exceptions thrown from service and orchestrator code must use the hierarchy in `pf_core.exceptions`. Never raise bare `Exception` — it loses structured context and produces an unsearchable log key.

---

## Exception Hierarchy

### `FlowException` — expected domain failures, not bugs

| Class | When to use |
|---|---|
| `InvalidInputError` | Caller supplied invalid parameters or data |
| `PreconditionError` | Required state not met — record not found, task already complete |
| `ConfigurationError` | Required config missing or invalid — DATABASE_URL unset, config file malformed |

`FlowException` exceptions have no `context` dict — they carry only a message.

---

### `AppError` — actual errors, always log with traceback

| Class | When to use |
|---|---|
| `ClientError` | External API call failed (OpenRouter, etc.) |
| `DataError` | Database write or read failure |
| `TaskError` | Task-level failure — carries `task_id` + optional `running_log` |

Projects subclass these for domain-specific errors:

```python
from pf_core.exceptions import ClientError

class ExportError(ClientError):
    """External export API call failed."""
```

**Constructor:** `AppError(message, context=None, *, cause=None)`

```python
raise ExportError(
    "export API timed out",
    context={"task_id": task_id, "endpoint": endpoint},
    cause=original_exc,
)
```

---

## `log_exception()` — the logging function

```python
from pf_core.log import log_exception

log_exception(
    exc,
    message_prepend="export failed",
    additional_context={"section": "intro"},
    log_level="warning",
    event_prefix="EXP",  # default "APP"
)
```

**Log event key:** `{prefix}-{ClassName}` — the search key in log files.

---

## Catch Patterns

### Pattern 1 — Let FlowException bubble (most common)
The CLI/API boundary handles it. No catch required.

### Pattern 2 — Wrap unexpected exception in domain error
```python
except Exception as e:
    log_exception(
        ExportError("unexpected failure", context={"task_id": tid}, cause=e),
        message_prepend="export step failed",
    )
```

### Pattern 3 — Log with context and re-raise
```python
except ExportError as e:
    log_exception(e, additional_context={"task_id": tid})
    raise
```

### Pattern 4 — Non-halting (loop-safe)
```python
for task in pending:
    try:
        run_task(task)
    except Exception as e:
        log_exception(TaskError("failed", context={"task_id": task.id}, cause=e))
        # continue loop
```

---

## CLI and API boundaries

**CLI:** Catch `FlowException` → print message, exit 1. Catch `AppError` → log_exception + print, exit 1.

**API:** FastAPI error handlers in `pf_core.web.app_factory` map `FlowException` → 4xx JSON/HTML; `AppError` → 500 JSON/HTML.
