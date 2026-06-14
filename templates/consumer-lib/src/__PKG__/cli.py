"""__NAME__ command-line entry point.

The day-1 vertical slice: a ``hello`` command, wired through pf-core's CLI
scaffold so ``--verbose`` logging and exception handling work out of the box.
Replace it with your real commands.
"""

from __future__ import annotations

import typer

from pf_core.cli import create_cli, run_cli
from pf_core.log import get_logger

from __PKG__.config import cfg

logger = get_logger(__name__)

app = create_cli("__NAME__", help="__NAME__ — a pf-core tool.")


@app.command()
def hello(name: str = typer.Argument("world")) -> None:
    """Greet someone — proves the install + wiring works."""
    logger.info("hello_invoked", name=name)
    typer.echo(f"hello, {name} (log level: {cfg.LOG_LEVEL})")


def main() -> None:
    run_cli(app)


if __name__ == "__main__":
    main()
