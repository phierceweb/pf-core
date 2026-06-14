"""Stage-cascade cache invalidation.

Pipelines often have multiple stages, each with its own caches. When the
user wants to re-run from a specific stage (e.g., after changing
splitter logic, re-run `split` without re-doing the upstream Marker
backend conversion), they need to invalidate that stage's cache and
every downstream stage's STRUCTURAL caches — while preserving
CONTENT-KEYED caches (phash, content hashes) that self-invalidate via
their key.

This module provides:
  - `StageDefinition` — describes one stage: name + structural file
    templates + content-keyed cache templates.
  - `StageRegistry` — ordered list of stages forming a pipeline.
  - `files_to_invalidate(output_dir, stage, registry, source_stem)`
    — list paths the cascade rule says to delete.
  - `invalidate_caches(output_dir, stage, registry, source_stem)`
    — actually delete them. Returns deleted paths.

File templates use `{stem}` as the only placeholder (the source input's
filename stem). Other path components are literal — `images`, `sections`,
`.vision-cache/`, etc.

Promoted from a consumer document-extraction pipeline's
`services/_rerun.py`. The originating registry was a 5-stage pipeline;
each consumer registers its own.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from pf_core.log import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class StageDefinition:
    """One stage in a pipeline. File templates use `{stem}` as the only
    placeholder; everything else is literal.

    `structural_files` are mtime-gated outputs owned by this stage —
    they depend on upstream content being unchanged, so cascade busts
    them on every downstream re-run.

    `content_keyed_files` are caches keyed by content hash / perceptual
    hash. They self-invalidate via their key, so they're PRESERVED
    across cascade and only busted when the user explicitly targets
    this stage.
    """

    name: str
    structural_files: tuple[str, ...] = ()
    content_keyed_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class StageRegistry:
    """Ordered pipeline of stages. The cascade rule walks this list
    from a target stage forward, accumulating each stage's structural
    files (plus the target's own content-keyed cache).
    """

    stages: tuple[StageDefinition, ...]

    def __post_init__(self) -> None:
        names = [s.name for s in self.stages]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate stage name(s) in registry: {names}")

    @property
    def stage_names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.stages)

    def find(self, name: str) -> StageDefinition:
        """Return the stage with the given name. Raises ValueError if unknown."""
        for s in self.stages:
            if s.name == name:
                return s
        raise ValueError(f"unknown stage: {name!r}. Valid: {self.stage_names}")

    def downstream(self, name: str) -> tuple[StageDefinition, ...]:
        """Return the stage with `name` plus every stage after it."""
        for i, s in enumerate(self.stages):
            if s.name == name:
                return self.stages[i:]
        raise ValueError(f"unknown stage: {name!r}. Valid: {self.stage_names}")


def _resolve_path(output_dir: Path, template: str, source_stem: str) -> Path:
    """Apply the `{stem}` placeholder and join under output_dir."""
    return output_dir / template.format(stem=source_stem)


def files_to_invalidate(
    output_dir: Path,
    *,
    stage: str,
    registry: StageRegistry,
    source_stem: str,
) -> list[Path]:
    """List paths the cascade rule says to invalidate.

    Cascade rule: target stage's OWN content-keyed cache + every
    DOWNSTREAM stage's STRUCTURAL files (including the target's own
    structural files). Downstream content-keyed caches are PRESERVED.

    Returns paths regardless of existence — caller filters.
    """
    target = registry.find(stage)
    paths: list[Path] = []
    # Target's content-keyed cache (only this one — not downstream).
    for template in target.content_keyed_files:
        paths.append(_resolve_path(output_dir, template, source_stem))
    # Target + every downstream stage's structural files.
    for s in registry.downstream(stage):
        for template in s.structural_files:
            paths.append(_resolve_path(output_dir, template, source_stem))
    return paths


def invalidate_caches(
    output_dir: Path,
    *,
    stage: str,
    registry: StageRegistry,
    source_stem: str,
) -> list[Path]:
    """Delete caches per the cascade rule.

    Returns the list of paths that actually existed and were removed.
    Missing files are silent no-ops. OSError on a single delete is
    logged as WARNING; cascade continues for other paths.
    """
    targets = files_to_invalidate(
        output_dir, stage=stage, registry=registry, source_stem=source_stem
    )
    deleted: list[Path] = []
    for p in targets:
        if not p.exists():
            continue
        try:
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
            deleted.append(p)
        except OSError as e:
            logger.warning("invalidate_failed path=%s error=%s", p, e)
    if deleted:
        logger.info(
            "rerun_from stage=%s invalidated=[%s]",
            stage,
            ", ".join(str(p.relative_to(output_dir)) for p in deleted),
        )
    return deleted
