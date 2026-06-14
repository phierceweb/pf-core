"""`make_invalidate_subcommand` factory.

Registers a Typer command that wraps
:func:`pf_core.pipeline.cache.invalidate_caches` — bust pipeline caches
without re-running. The cascade rule (target stage's own content-keyed
cache + every downstream stage's structural files) lives in
``pf_core.pipeline.cache``; this module is the CLI surface.
"""

from __future__ import annotations

from pathlib import Path

import typer

from pf_core.pipeline.cache import StageRegistry, invalidate_caches

from ._render import _infer_source_stem


def make_invalidate_subcommand(
    app: typer.Typer,
    *,
    registry: StageRegistry,
    run_record_filename: str = "run.json",
    command_name: str = "invalidate",
) -> None:
    """Register a cache-invalidate command on ``app``.

    The registered command takes ``output_dir`` (Path) + ``stage`` (str)
    positional args and an optional ``--source-stem`` option. When
    ``--source-stem`` is omitted, it's inferred from
    ``<output_dir>/<run_record_filename>``'s ``input`` field.

    Args:
        app: Typer app to register on.
        registry: Stage registry describing the pipeline's stages.
        run_record_filename: Name of the run-record file used to infer
            ``source_stem`` when omitted. Defaults to ``run.json``.
        command_name: CLI command name. Defaults to ``invalidate``.
    """
    stage_names = registry.stage_names

    @app.command(command_name)
    def invalidate(
        output_dir: Path = typer.Argument(
            ...,
            exists=True,
            file_okay=False,
            dir_okay=True,
            help="Output directory whose caches should be invalidated.",
        ),
        stage: str = typer.Argument(
            ...,
            help=(
                f"Pipeline stage. One of: {' | '.join(stage_names)}. "
                "Selecting a stage deletes that stage's cache plus every "
                "downstream stage's cache."
            ),
        ),
        source_stem: str | None = typer.Option(
            None,
            "--source-stem",
            help=(
                f"Source filename stem (without extension). Inferred from "
                f"<output_dir>/{run_record_filename} if omitted."
            ),
        ),
    ) -> None:
        """Bust pipeline caches without re-running."""
        if stage not in stage_names:
            raise typer.BadParameter(
                f"stage must be one of {stage_names}; got {stage!r}"
            )

        if source_stem is None:
            source_stem = _infer_source_stem(output_dir, run_record_filename)

        deleted = invalidate_caches(
            output_dir,
            stage=stage,
            registry=registry,
            source_stem=source_stem,
        )

        if not deleted:
            typer.echo(f"no caches to invalidate at stage={stage}", err=True)
            return
        for p in deleted:
            typer.echo(str(p))
