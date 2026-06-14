# Services

A **Service** is a single-domain business logic unit. It owns one slice of domain logic — text summarization, entry export, catalog scanning, etc.

## Why use the Service base class?

The `Service` base class solves recurring problems across projects:

1. **Config via constructor, not globals.** Services receive an `AppConfig` instance instead of reading `os.environ` directly. This makes dependencies explicit and testing straightforward.

2. **Built-in structured logging.** Every service gets `self._log` — a structlog logger named after the concrete subclass module. No more `print()` to stderr.

3. **Connection sharing via `_repo()`.** When an orchestrator passes a connection, every repository instantiated by that service shares it. This keeps multi-step workflows inside a single transaction without the service calling `transaction()` itself.

## Quick start

```python
from pf_core.services import Service
from myproject.repos.summary import SummaryRepo

class SummaryService(Service):
    def active_summaries(self) -> list[dict]:
        repo = self._repo(SummaryRepo)
        return repo.list_active()

    def length_limit(self, text: str) -> int:
        threshold = self._config.MAX_WORDS
        # ... summarization logic ...
```

## Constructor parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `config` | `AppConfig \| None` | `None` | Project configuration instance |
| `conn` | `Connection \| None` | `None` | Shared SQLAlchemy connection for transaction participation |

Both are keyword-only.

## Built-in attributes

| Attribute | Description |
|-----------|-------------|
| `self._config` | The AppConfig instance (or None) |
| `self._conn` | The shared connection (or None) |
| `self._log` | structlog logger named after the concrete subclass module |

## Using `_repo()`

`_repo()` instantiates a `Repository` subclass, forwarding the service's connection:

```python
class ExportService(Service):
    def export_entries(self, ids: list[str]) -> list[dict]:
        entry_repo = self._repo(EntryRepo)
        tag_repo = self._repo(TagRepo)
        # Both repos share the same connection if one was provided
        entries = [entry_repo.get_by_id(i) for i in ids]
        ...
```

## Standalone vs. orchestrated

```python
# Standalone — service creates its own transactions per repo call:
svc = SummaryService(config=cfg)
summaries = svc.active_summaries()

# Inside an orchestrator — shared transaction:
from pf_core.db import transaction

with transaction() as conn:
    svc = SummaryService(config=cfg, conn=conn)
    summaries = svc.active_summaries()
    # all repo operations share this transaction
```

## Rules for services

Services **must not**:
- Import from orchestrators or entry points (dependency inversion)
- Call `transaction()` directly — use `self._repo()` instead
- Read `os.environ` — use `self._config`
- Use `print()` — use `self._log`
