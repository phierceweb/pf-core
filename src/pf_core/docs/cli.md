# CLI Framework

Pre-configured Typer app with structured logging and standardized error handling.

## Setup

```python
# app/cli/__init__.py
from pf_core.cli import create_cli, run_cli

from app.cli import research, admin

app = create_cli("myapp", help="My application CLI.")

research.register(app)
admin.register(app)

def main():
    run_cli(app)
```

## `create_cli(name, *, help, **kwargs)`

Returns a `typer.Typer` with:

- `--verbose / -v` flag that sets `LOG_LEVEL=DEBUG`
- Automatic `setup_logging()` on every invocation
- Shell completion disabled (cleaner `--help`)

All kwargs are forwarded to `typer.Typer()`.

## `run_cli(app, *, args=None)`

Wraps `app()` with standardized exception handling:

| Exception | Behavior |
|-----------|----------|
| `typer.Exit(N)` | Exits with code `N`. See note below. |
| `FlowException` | Print message to stderr (red), exit 1 |
| `AppError` | `log_exception()` + print to stderr, exit 1 |
| `KeyboardInterrupt` / `Abort` | Print "Interrupted.", exit 130 |
| `SystemExit` | Pass through unchanged |

`run_cli` invokes the app with `standalone_mode=False`, under which click does **not** raise on `typer.Exit` — it *returns* the exit code from `app()`. `run_cli` converts a non-zero int return into `sys.exit(rv)` so `raise typer.Exit(N)` actually exits with `N`. A `bool` return is exempt (`True` is an `int` but not an exit code), and a command callback that legitimately returns a non-zero int is indistinguishable from an exit code in this mode — pf-core commands report via echo/log, not return values.

The `args` parameter is for testing — pass a list of CLI args instead of reading `sys.argv`.

## Subcommand pattern

Group commands into modules with a `register(app)` function:

```python
# app/cli/research.py
from typing import Optional
import typer
from app.services import search_svc

def register(app: typer.Typer) -> None:
    @app.command()
    def research(
        topic: str = typer.Argument(..., help="Research topic"),
        model: Optional[str] = typer.Option(None, "--model", help="Override model"),
    ):
        """Run a research task."""
        search_svc.run(topic, model=model)
```

## Error handling

With `run_cli`, service exceptions are caught automatically. Instead of:

```python
# Before — manual error handling in every command
try:
    svc.process(item)
except ProcessError as e:
    print(f"Error: {e}", file=sys.stderr)
    sys.exit(1)
```

Just let the exception propagate:

```python
# After — framework handles it
svc.process(item)  # FlowException/AppError caught by run_cli
```

For validation guards that need a specific message, `typer.Exit(1)` is still appropriate:

```python
item = db.get_item(name)
if not item:
    console.print(f"[red]Item not found: {name!r}[/red]")
    raise typer.Exit(1)
```
