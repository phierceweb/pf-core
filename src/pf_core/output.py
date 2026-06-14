"""
Progress reporting for service layers.

Services accept an optional ``Reporter`` to emit progress messages without
coupling to a specific output mechanism. CLI entry points pass a
``ConsoleReporter``; background jobs use ``LogReporter``; tests use
``NullReporter`` (the default).

Usage::

    from pf_core.output import ConsoleReporter, Reporter

    def process_items(items: list, *, reporter: Reporter | None = None):
        reporter = reporter or NullReporter()
        reporter.info("Processing {n} items", n=len(items))
        for item in items:
            reporter.step("Item {id}", id=item["id"])
        reporter.done("Finished {n} items", n=len(items))
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import structlog
from rich.console import Console


def _fmt(msg: str, kw: dict) -> str:
    """Format *msg* with *kw*, returning raw *msg* on any format error."""
    try:
        return msg.format(**kw)
    except (KeyError, IndexError, ValueError):
        return msg


@runtime_checkable
class Reporter(Protocol):
    """Protocol for progress reporters used by service layers."""

    def info(self, msg: str, **kw: object) -> None: ...
    def warning(self, msg: str, **kw: object) -> None: ...
    def error(self, msg: str, **kw: object) -> None: ...
    def step(self, msg: str, **kw: object) -> None: ...
    def done(self, msg: str, **kw: object) -> None: ...


class NullReporter:
    """No-op reporter — the default when no reporter is provided."""

    def info(self, msg: str, **kw: object) -> None:
        pass

    def warning(self, msg: str, **kw: object) -> None:
        pass

    def error(self, msg: str, **kw: object) -> None:
        pass

    def step(self, msg: str, **kw: object) -> None:
        pass

    def done(self, msg: str, **kw: object) -> None:
        pass


class ConsoleReporter:
    """Rich-based reporter that writes to stderr."""

    def __init__(self, *, console: Console | None = None):
        self._console = console or Console(stderr=True)

    def info(self, msg: str, **kw: object) -> None:
        self._console.print(_fmt(msg, kw))

    def warning(self, msg: str, **kw: object) -> None:
        self._console.print(f"[yellow]{_fmt(msg, kw)}[/yellow]")

    def error(self, msg: str, **kw: object) -> None:
        self._console.print(f"[red]{_fmt(msg, kw)}[/red]")

    def step(self, msg: str, **kw: object) -> None:
        self._console.print(f"  [dim]{_fmt(msg, kw)}[/dim]")

    def done(self, msg: str, **kw: object) -> None:
        self._console.print(f"[bold green]{_fmt(msg, kw)}[/bold green]")


class LogReporter:
    """Reporter that delegates to a structlog ``BoundLogger``."""

    def __init__(self, logger: structlog.stdlib.BoundLogger):
        self._logger = logger

    def info(self, msg: str, **kw: object) -> None:
        self._logger.info(_fmt(msg, kw), **kw)

    def warning(self, msg: str, **kw: object) -> None:
        self._logger.warning(_fmt(msg, kw), **kw)

    def error(self, msg: str, **kw: object) -> None:
        self._logger.error(_fmt(msg, kw), **kw)

    def step(self, msg: str, **kw: object) -> None:
        self._logger.debug(_fmt(msg, kw), **kw)

    def done(self, msg: str, **kw: object) -> None:
        self._logger.info(_fmt(msg, kw), **kw)
