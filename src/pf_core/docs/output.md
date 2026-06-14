# Progress Reporting

Decouples service-layer progress messages from output mechanism. Services accept an optional `Reporter`; the caller picks the implementation.

## Quick usage

```python
from pf_core.output import ConsoleReporter, NullReporter, Reporter

def process_items(items: list, *, reporter: Reporter | None = None):
    reporter = reporter or NullReporter()
    reporter.info("Processing {n} items", n=len(items))
    for item in items:
        reporter.step("Item {id}", id=item["id"])
    reporter.done("Finished {n} items", n=len(items))

# CLI entry point:
process_items(items, reporter=ConsoleReporter())
```

## Reporter Protocol

All reporters implement these methods:

| Method | Purpose | ConsoleReporter style | LogReporter level |
|--------|---------|----------------------|-------------------|
| `info(msg, **kw)` | General progress | plain text | INFO |
| `warning(msg, **kw)` | Non-fatal issues | yellow | WARNING |
| `error(msg, **kw)` | Errors | red | ERROR |
| `step(msg, **kw)` | Per-item progress | indented, dim | DEBUG |
| `done(msg, **kw)` | Summary | bold green | INFO |

All methods accept `msg` with `{key}` format placeholders and `**kw` for values.

## Implementations

### NullReporter

No-op. All methods do nothing. This is the default when no reporter is provided.

### ConsoleReporter

Writes to stderr via Rich Console with color styling.

```python
from pf_core.output import ConsoleReporter

reporter = ConsoleReporter()
reporter.info("Starting batch")           # plain
reporter.warning("Config outdated")       # [yellow]
reporter.error("Failed: {e}", e=err)      # [red]
reporter.step("Processed {n}/10", n=5)     #   [dim]
reporter.done("All done")                 # [bold green]
```

Accepts an optional `console` parameter for testing:

```python
from io import StringIO
from rich.console import Console
buf = StringIO()
reporter = ConsoleReporter(console=Console(file=buf))
```

### LogReporter

Routes to a structlog `BoundLogger`. Keyword arguments are passed as structured log fields.

```python
from pf_core.log import get_logger
from pf_core.output import LogReporter

logger = get_logger(__name__)
reporter = LogReporter(logger)
reporter.info("Processing {n} items", n=42)
# Logs: INFO "Processing 42 items" n=42
```

## Integration with run_parallel

`pf_core.parallel.run_parallel()` accepts an optional `reporter` parameter. When provided, it uses `reporter.step()` instead of the default `print()` for progress output.

```python
from pf_core.output import ConsoleReporter
from pf_core.parallel import run_parallel

run_parallel(items, process_one, workers=4, label="Processed", reporter=ConsoleReporter())
```

## Format error handling

If a format placeholder is missing from `**kw`, the raw message is printed without crashing:

```python
reporter.info("Missing {x}")  # prints "Missing {x}" — no KeyError
```

## Migration

Replace a `verbose`-flag print pattern in services:

```python
def process_items(items, verbose=True):
    if verbose:
        print(f"Processing {len(items)} items...", file=sys.stderr)
    # ... work ...
    if verbose:
        print(f"Done. {len(items)} processed.", file=sys.stderr)
```

With:

```python
from pf_core.output import Reporter, NullReporter

def process_items(items, *, reporter: Reporter | None = None):
    reporter = reporter or NullReporter()
    reporter.info("Processing {n} items", n=len(items))
    # ... work ...
    reporter.done("{n} items processed", n=len(items))
```

CLI entry point passes `ConsoleReporter()`.
