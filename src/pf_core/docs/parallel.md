# Parallel Execution

Thread-based parallel execution for batch operations with progress tracking.

## Usage

```python
from pf_core.parallel import run_parallel

def process_one(item):
    # do work with item
    ...

run_parallel(
    items=work_items,
    fn=process_one,
    workers=4,
    label="Processed",
)
```

## Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `items` | `list` | (required) | List of work items |
| `fn` | `Callable` | (required) | Function that processes one item |
| `workers` | `int` | `1` | Number of parallel threads (1 = sequential) |
| `label` | `str` | `"Processed"` | Progress label printed to stderr |
| `progress_callback` | `Callable[[int, int], Any]` | `None` | Called with `(done_count, total)` after each item |
| `reporter` | `Reporter \| None` | `None` | [Reporter](output.md) for progress output. Replaces default `print()` when provided |
| `failures` | `list[tuple[str, str]] \| None` | `None` | Caller-owned `(label, reason)` list (the same one passed to `resilient`). When provided, run_parallel logs an end-of-batch summary — see [Batch summary log](#batch-summary-log) below |

## Behavior

- Progress prints to stderr: `  Processed 3/10: item_label`
- Thread-safe progress counter
- `workers=1` runs sequentially (no thread pool overhead)
- Exceptions from `fn` propagate after all futures complete
- Item labels are auto-extracted: tuples use the second element (or first if not an int), other types use `str(item)[:60]`
- Each worker receives its own snapshot of the calling thread's `contextvars` (via `copy_context()` per task), so `ContextVar`s set in the parent — including the `Job()` context manager from `pf_core.jobs.runtime` — remain visible inside `fn`. Mutations inside one worker do not leak to siblings or the parent.

## Example with progress callback

```python
def update_job(done, total):
    db.update_task(task_id, progress=f"{done}/{total}")

run_parallel(
    items=sections,
    fn=summarize_section,
    workers=cfg.THREAD_MAX_WORKERS,
    label="Summarized",
    progress_callback=update_job,
)
```

## Batch resilience — `resilient` decorator

By default, an exception inside `fn` propagates after all in-flight futures finish, aborting the batch. For LLM-style workloads where one bad item shouldn't waste the rest of the batch, use the `resilient` decorator to absorb per-item failures into a list while siblings keep running.

```python
from pf_core.parallel import resilient, run_parallel

failures: list[tuple[str, str]] = []

@resilient(failures, label_fn=lambda i: i[0], reporter=reporter,
           log_label="summarizer failed")
def summarize_one(item):
    key, text = item
    with job.step(f"summarize_{key}") as step:
        if step.skipped:
            return key
        # ... do the work; raise on errors. The pf-core step context
        # marks the step failed via its own except path, so the
        # resilient wrapper outside catches the re-raised exception.
    return key

run_parallel(items, summarize_one, workers=4, label="Summarized")
written = len(items) - len(failures)
```

On exception the wrapper:

- logs via `pf_core.log.log_exception` with the item label as structured context
- appends `(label, reason)` to the `failures` list (thread-safe; callers own the list)
- emits `"  ✗ {item}: {reason}"` via `reporter.error(...)` if a reporter is given
- returns the label so callers that consume `fn`'s return value (outside `run_parallel`, which discards it) see a stable identifier on both success and failure paths

`reason` is `str(exc)` for `AppError` / `FlowException` (whose messages are intentional) and `f"{type(exc).__name__}: {exc}"` for everything else (so unexpected types stay diagnosable).

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `failures` | `list[tuple[str, str]]` | (required) | Caller-owned list the wrapper appends `(label, reason)` to |
| `label_fn` | `Callable[[Any], str] \| None` | `None` (uses `str(item)`) | Extracts a string label from one item |
| `reporter` | `Reporter \| None` | `None` | Optional `pf_core.output.Reporter` for user-facing error lines |
| `log_label` | `str` | `"worker failed"` | Prefix for `log_exception` — pass per-service prefix for log grepping |
| `catch` | exception type or tuple | `Exception` | Exception types to absorb; narrow to let others propagate |

## Batch summary log

When the caller hands `run_parallel` the same `failures` list it gave to `resilient`, run_parallel emits a structured summary at end-of-batch so the operator gets one log line with success / failure counts instead of having to grep per-item warnings.

```python
failures: list[tuple[str, str]] = []

@resilient(failures, label_fn=lambda i: i[0])
def summarize_one(item):
    ...

run_parallel(items, summarize_one, workers=4, label="Summarized", failures=failures)
# At end of batch, one of:
#   INFO  batch_complete_all_succeeded   label=Summarized total=120 succeeded=120
#   WARN  batch_complete_with_failures   label=Summarized total=120 succeeded=115 failed=5 failure_rate=4.17
```

The summary level distinguishes the two cases so dashboards and log filters can alert on the warning case (any failure in the batch) without false positives from the all-succeeded case.

The summary is opt-in — without `failures=`, no summary log is emitted. Empty `items` lists also skip the summary.
