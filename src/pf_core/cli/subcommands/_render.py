"""Shared helpers for the subcommand factories.

Private to ``pf_core.cli.subcommands``. All helpers are pure functions
of their args + stdout (via ``typer.echo``); they take no state from
the caller. Split out of the factory modules so each factory module
stays under the file budget and so the helpers can be tested
independently if needed.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from pf_core.pipeline.baseline import BaselineConfig, BaselineRecord
from pf_core.pipeline.baseline_diff import DiffReport


def _infer_source_stem(output_dir: Path, run_record_filename: str) -> str:
    """Read the source filename stem from the run record.

    Raises:
        typer.BadParameter: when no run record exists, the run record is
            unreadable, or it lacks an ``input`` field.
    """
    run_record = output_dir / run_record_filename
    if not run_record.exists():
        raise typer.BadParameter(
            f"no {run_record_filename} in {output_dir}; pass --source-stem explicitly"
        )
    try:
        data = json.loads(run_record.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise typer.BadParameter(f"could not read run record: {e}") from e
    input_name = data.get("input")
    if not input_name:
        raise typer.BadParameter(f"run record at {run_record} missing `input` field")
    return Path(input_name).stem


def _print_diff_summary(report: DiffReport, *, default_top: int = 30) -> None:
    """Default `baseline diff` output — structured summary."""
    typer.echo(f"baseline: {report.baseline_label}   path: {report.baseline_path}")
    typer.echo(f"current:  {report.current_path}")
    typer.echo("")

    # Run record.
    if not report.run_record.changed_fields:
        typer.echo("Run record: unchanged")
    else:
        typer.echo("Run record:")
        for field, (bv, cv) in report.run_record.changed_fields.items():
            typer.echo(f"  {field:30}  {bv}  →  {cv}")
    typer.echo("")

    # Sections.
    sd = report.sections
    if not (sd.added or sd.removed or sd.renamed):
        typer.echo("Sections: unchanged")
    else:
        typer.echo("Sections:")
        if sd.added:
            typer.echo(f"  added ({len(sd.added)}):")
            for p in sd.added:
                typer.echo(f"    {p}")
        if sd.removed:
            typer.echo(f"  removed ({len(sd.removed)}):")
            for p in sd.removed:
                typer.echo(f"    {p}")
        if sd.renamed:
            typer.echo(f"  renamed ({len(sd.renamed)}):")
            for r in sd.renamed:
                typer.echo(
                    f"    {r.old_path}  →  {r.new_path}  (similarity={r.similarity:.2f})"
                )
    typer.echo("")

    # Body changes.
    if not report.body_changes:
        typer.echo("Body changes: none")
        return
    top = report.body_changes[:default_top]
    suffix = (
        f" (top {default_top} of {len(report.body_changes)})"
        if len(report.body_changes) > default_top
        else ""
    )
    typer.echo(f"Body changes{suffix}:")
    for c in top:
        typer.echo(f"  +{c.plus:>4} -{c.minus:<4}  {c.path}")


def _emit_section_unified_diff(
    report: DiffReport,
    rel_path: str,
    *,
    config: BaselineConfig,
) -> None:
    """Print a unified diff for one section file (relative to sections/)."""
    import difflib

    base = report.baseline_path / config.sections_dir_name / rel_path
    curr = report.current_path / config.sections_dir_name / rel_path
    if not base.exists() or not curr.exists():
        raise typer.BadParameter(
            f"section {rel_path!r} missing from baseline or current."
        )
    base_lines = base.read_text(encoding="utf-8").splitlines(keepends=True)
    curr_lines = curr.read_text(encoding="utf-8").splitlines(keepends=True)
    for line in difflib.unified_diff(
        base_lines,
        curr_lines,
        fromfile=f"baseline:{rel_path}",
        tofile=f"current:{rel_path}",
    ):
        typer.echo(line, nl=False)


def _emit_consolidated_unified_diff(
    report: DiffReport,
    *,
    config: BaselineConfig,
) -> None:
    """Print a unified diff for the consolidated md."""
    import difflib

    base_record = report.baseline_path / config.run_record_filename
    data = json.loads(base_record.read_text(encoding="utf-8"))
    input_name = data.get("input")
    if not input_name:
        raise typer.BadParameter(
            f"run record at {base_record} missing `input` field; cannot resolve consolidated md."
        )
    stem = Path(input_name).stem
    consolidated_name = config.consolidated_md_pattern.format(stem=stem)
    base = report.baseline_path / consolidated_name
    curr = report.current_path / consolidated_name
    if not base.exists() or not curr.exists():
        raise typer.BadParameter("consolidated md missing from baseline or current.")
    base_lines = base.read_text(encoding="utf-8").splitlines(keepends=True)
    curr_lines = curr.read_text(encoding="utf-8").splitlines(keepends=True)
    for line in difflib.unified_diff(
        base_lines,
        curr_lines,
        fromfile=f"baseline:{consolidated_name}",
        tofile=f"current:{consolidated_name}",
    ):
        typer.echo(line, nl=False)


def _print_table(records: list[BaselineRecord]) -> None:
    """Plain-text fixed-width table; no jinja / rich. Cheap and grep-friendly."""
    headers = ("LABEL", "VERSION", "PRESET", "SAVED-AT", "SECTIONS", "IMAGES")
    rows = [
        (
            r.label,
            r.version,
            r.preset or "-",
            r.saved_at or "-",
            str(r.section_count) if r.section_count is not None else "-",
            str(r.image_count) if r.image_count is not None else "-",
        )
        for r in records
    ]
    widths = [
        max(len(h), max((len(row[i]) for row in rows), default=0))
        for i, h in enumerate(headers)
    ]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    typer.echo(fmt.format(*headers))
    for row in rows:
        typer.echo(fmt.format(*row))
