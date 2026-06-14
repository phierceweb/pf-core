"""`make_baseline_subcommand_group` factory.

Registers a Typer subcommand group on the caller's app with three
subcommands — ``save`` / ``list`` / ``diff`` — that wrap
:func:`pf_core.pipeline.baseline.save_baseline`,
:func:`pf_core.pipeline.baseline.list_baselines`, and
:func:`pf_core.pipeline.baseline_diff.diff_baseline` respectively.
``diff`` supports ``--show-section`` and ``--show-consolidated`` flags
for drill-in unified-diff inspection.
"""

from __future__ import annotations

from pathlib import Path

import typer

from pf_core.pipeline.baseline import (
    DEFAULT_CONFIG,
    BaselineConfig,
    list_baselines,
    save_baseline,
)
from pf_core.pipeline.baseline_diff import diff_baseline

from ._render import (
    _emit_consolidated_unified_diff,
    _emit_section_unified_diff,
    _print_diff_summary,
    _print_table,
)


def make_baseline_subcommand_group(
    app: typer.Typer,
    *,
    config: BaselineConfig = DEFAULT_CONFIG,
    command_name: str = "baseline",
) -> None:
    """Register a `baseline` subcommand group on ``app``.

    The group contains three subcommands:

      - ``save`` — :func:`pf_core.pipeline.baseline.save_baseline`
      - ``list`` — :func:`pf_core.pipeline.baseline.list_baselines`
      - ``diff`` — :func:`pf_core.pipeline.baseline_diff.diff_baseline`
        (with optional ``--show-section`` / ``--show-consolidated``
        unified-diff drill-ins)

    Args:
        app: Typer app to register on.
        config: Filename / directory conventions for the underlying
            pipeline calls. Defaults to
            :data:`pf_core.pipeline.baseline.DEFAULT_CONFIG`.
        command_name: Subcommand group name. Defaults to ``baseline``.
    """
    baseline_app = typer.Typer(
        name=command_name,
        help="Snapshot live output for later comparison. Save / list / diff.",
        no_args_is_help=True,
    )

    @baseline_app.command("save")
    def save(
        output_dir: Path = typer.Argument(
            ...,
            exists=True,
            file_okay=False,
            dir_okay=True,
            help="Output directory whose result should be snapshotted.",
        ),
        label: str | None = typer.Option(
            None,
            "--label",
            help="Label for the baseline. Default: `<version>-<YYYYMMDD-HHMMSS>`.",
        ),
    ) -> None:
        """Snapshot the current live output into `<baselines_dir>/<label>/`."""
        try:
            record = save_baseline(output_dir, label=label, config=config)
        except ValueError as e:
            raise typer.BadParameter(str(e)) from e

        typer.echo(f"saved baseline label={record.label} path={record.path}")
        typer.echo(f"  version       : {record.version}")
        typer.echo(f"  preset        : {record.preset}")
        typer.echo(f"  sections      : {record.section_count}")
        typer.echo(f"  images        : {record.image_count}")

    @baseline_app.command("list")
    def list_(
        output_dir: Path = typer.Argument(
            ...,
            exists=True,
            file_okay=False,
            dir_okay=True,
            help="Output directory whose baselines should be listed.",
        ),
    ) -> None:
        """List all baselines in `<output_dir>/<baselines_dir>/`."""
        records = list_baselines(output_dir, config=config)
        if not records:
            typer.echo("no baselines saved", err=True)
            return

        _print_table(records)

    @baseline_app.command("diff")
    def diff(
        output_dir: Path = typer.Argument(
            ...,
            exists=True,
            file_okay=False,
            dir_okay=True,
            help="Output directory containing both the baseline and the live result.",
        ),
        label: str = typer.Argument(
            ...,
            help="Baseline label to compare against the current live output.",
        ),
        show_section: str | None = typer.Option(
            None,
            "--show-section",
            help=(
                "Emit a full unified diff for one section file (path relative to "
                "sections/). Mutually exclusive with --show-consolidated."
            ),
        ),
        show_consolidated: bool = typer.Option(
            False,
            "--show-consolidated",
            help="Emit a full unified diff for the consolidated md.",
        ),
    ) -> None:
        """Compare a saved baseline to the current live output."""
        if show_section is not None and show_consolidated:
            raise typer.BadParameter(
                "--show-section and --show-consolidated are mutually exclusive."
            )

        try:
            report = diff_baseline(output_dir, label=label, config=config)
        except ValueError as e:
            raise typer.BadParameter(str(e)) from e

        if show_section is not None:
            _emit_section_unified_diff(report, show_section, config=config)
            return
        if show_consolidated:
            _emit_consolidated_unified_diff(report, config=config)
            return

        _print_diff_summary(report)

    app.add_typer(baseline_app)
