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

import sys
from typing import Any

import click
import typer
from rich.console import Console

from pf_core.exceptions import AppError, FlowException
from pf_core.log import get_logger, log_exception, setup_logging

logger = get_logger(__name__)

_stderr = Console(stderr=True)


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
    - ``FlowException`` — prints message to stderr, exits 1.
    - ``AppError`` — logs with traceback, prints message to stderr, exits 1.
    - ``KeyboardInterrupt`` — exits silently.

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
    except (KeyboardInterrupt, click.exceptions.Abort):
        _stderr.print("\nInterrupted.")
        sys.exit(130)
    except FlowException as exc:
        _stderr.print(f"[red]{exc}[/red]")
        sys.exit(1)
    except AppError as exc:
        log_exception(exc, message_prepend="cli error")
        _stderr.print(f"[red]{exc}[/red]")
        sys.exit(1)
