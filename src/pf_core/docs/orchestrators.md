# Orchestrators

An **Orchestrator** coordinates multiple services to accomplish a multi-step workflow — exporting entries, running auto-review, scanning a catalog.

## Why use the Orchestrator base class?

1. **Structural enforcement.** Orchestrators instantiate services via `_service()`, not repos. This prevents the orchestrator from touching the database directly — all data access flows through services.

2. **Config propagation.** `_service()` automatically passes the orchestrator's config to each service it creates.

3. **Progress reporting.** `_report()` fires an optional callback and logs at INFO. Works for CLI progress bars, web job polling, or just structured logs.

## Quick start

```python
from pf_core.orchestrators import Orchestrator
from myproject.services.entry import EntryService
from myproject.services.tag import TagService

class ExportOrchestrator(Orchestrator):
    def run(self, entry_ids: list[str]) -> ExportResult:
        entry_svc = self._service(EntryService)
        tag_svc = self._service(TagService)

        self._report(1, 3, "Loading entries")
        entries = entry_svc.load_many(entry_ids)

        self._report(2, 3, "Resolving tags")
        tagged = tag_svc.attach_tags(entries)

        self._report(3, 3, "Building export")
        return ExportResult(tagged)
```

## Constructor parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `config` | `AppConfig \| None` | `None` | Project configuration instance |
| `progress` | `ProgressCallback \| None` | `None` | Callable `(step, total, message)` for progress updates |

Both are keyword-only.

## Built-in attributes

| Attribute | Description |
|-----------|-------------|
| `self._config` | The AppConfig instance (or None) |
| `self._progress` | The progress callback (or None) |
| `self._log` | structlog logger named after the concrete subclass module |

## Using `_service()`

`_service()` instantiates a `Service` subclass, forwarding config and any extra kwargs:

```python
# Basic — just config propagation:
svc = self._service(EntryService)

# The transactional-orchestration exception: open ONE shared transaction and
# thread it through `conn=` so a multi-service step commits atomically. The
# orchestrator never executes SQL on the connection itself.
from pf_core.db import transaction

with transaction() as conn:
    svc = self._service(EntryService, conn=conn)
```

## Progress reporting

```python
# CLI with Rich:
from rich.progress import Progress

with Progress() as bar:
    task = bar.add_task("Export", total=100)
    def on_progress(step, total, msg):
        bar.update(task, completed=step, description=msg)
    orch = ExportOrchestrator(config=cfg, progress=on_progress)
    orch.run(ids)

# Web job — store progress for polling:
def on_progress(step, total, msg):
    redis.set(f"job:{job_id}:progress", f"{step}/{total}: {msg}")

orch = ExportOrchestrator(config=cfg, progress=on_progress)
```

## Rules for orchestrators

Orchestrators **must not**:
- Import or instantiate repositories directly — use `_service()` to get services
- Call `transaction()` for data access — services own reads and writes. The one sanctioned use is opening a shared transaction threaded through `_service(..., conn=conn)` (above) so a multi-service step commits atomically; the orchestrator never runs SQL on it
- Contain domain logic — delegate to services
- Grow beyond the orchestrator line budget (enforced by the `pf_core.guards` gate; the limit lives in `pf_core/guards/config.py`) — split into smaller orchestrators
