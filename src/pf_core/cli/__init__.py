"""
CLI framework for pf-core projects.

Provides a pre-configured Typer app with structured logging setup,
verbose flag, and standardized exception handling.

Usage::

    from pf_core.cli import create_cli, run_cli

    app = create_cli("myapp", help="My application CLI.")

    @app.command()
    def greet(name: str):
        print(f"Hello, {name}!")

    def main():
        run_cli(app)
"""

from __future__ import annotations

import importlib
import sys
from typing import Any

import typer
from rich.console import Console

from pf_core.exceptions import AppError, FlowException
from pf_core.log import get_logger, log_exception, setup_logging

logger = get_logger(__name__)

_stderr = Console(stderr=True)


def _exc(module: str, name: str) -> tuple[type[BaseException], ...]:
    try:
        return (getattr(importlib.import_module(module), name),)
    except (ImportError, AttributeError):
        return ()


def _merge(*groups: tuple[type[BaseException], ...]) -> tuple[type[BaseException], ...]:
    return tuple(dict.fromkeys(exc for group in groups for exc in group))


# typer >= 0.26 vendors its own click, so typer.Abort/BadParameter are NOT the
# installed click's classes. Both hierarchies are live across the typer pin.
_ABORT = _merge(_exc("typer", "Abort"), _exc("click.exceptions", "Abort"))
_USAGE = _merge(
    _exc("typer._click.exceptions", "ClickException"),
    _exc("click.exceptions", "ClickException"),
)


def create_cli(name: str, *, help: str = "", **kwargs: Any) -> typer.Typer:
    """Create a Typer app with standard framework configuration.

    Args:
        name: Application name.
        help: Help text shown in ``--help``.
        **kwargs: Additional kwargs passed to ``typer.Typer()``.

    Returns:
        A configured Typer application.
    """
    app = typer.Typer(
        name=name,
        help=help,
        add_completion=False,
        **kwargs,
    )

    @app.callback()
    def _setup(
        verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
    ) -> None:
        level = "DEBUG" if verbose else None
        setup_logging(level=level)

    return app


def run_cli(app: typer.Typer, *, args: list[str] | None = None) -> None:
    """Run a Typer app with standardized exception handling.

    Catches framework exceptions and exits cleanly:
    - ``typer.Exit(N)`` — exits with N (see below).
    - ``ClickException`` (bad option, missing argument, ``typer.BadParameter``)
      — shows the usage message, exits with the exception's own ``exit_code``
      (2 for usage errors, 1 otherwise).
    - ``FlowException`` — prints message to stderr, exits 1.
    - ``AppError`` — logs with traceback, prints message to stderr, exits 1.
    - ``typer.Abort`` — prints "Interrupted.", exits 130.
    - ``KeyboardInterrupt`` — exits 130 silently: typer converts it to
      ``Exit(130)`` before ``run_cli`` sees it, so it arrives as an int return.

    With ``standalone_mode=False`` click does not raise for ``typer.Exit`` —
    it RETURNS the exit code from ``app()``. Dropping that return value made
    every consumer's ``raise typer.Exit(N)`` exit 0 in the real process, so a
    non-zero int return is converted to ``sys.exit`` here. A command callback
    that legitimately returns a non-zero int is indistinguishable from an exit
    code in this mode (click's API conflates them) — pf-core commands report
    via echo/log, not return values. ``bool`` returns are exempt (``True`` is
    an ``int`` but not an exit code).

    Args:
        app: The Typer application to run.
        args: Optional CLI args (defaults to sys.argv). Useful for testing.
    """
    try:
        rv = app(standalone_mode=False, args=args)
        if isinstance(rv, int) and not isinstance(rv, bool) and rv != 0:
            sys.exit(rv)
    except SystemExit:
        raise
    except (KeyboardInterrupt, *_ABORT):
        _stderr.print("\nInterrupted.")
        sys.exit(130)
    except _USAGE as exc:
        exc.show()
        sys.exit(exc.exit_code)
    except FlowException as exc:
        _stderr.print(f"[red]{exc}[/red]")
        sys.exit(1)
    except AppError as exc:
        log_exception(exc, message_prepend="cli error")
        _stderr.print(f"[red]{exc}[/red]")
        sys.exit(1)
