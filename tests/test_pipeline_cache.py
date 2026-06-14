from __future__ import annotations

from pathlib import Path

import pytest
from pf_core.pipeline.cache import (
    StageDefinition,
    StageRegistry,
    files_to_invalidate,
    invalidate_caches,
)


@pytest.fixture
def pipeline_registry() -> StageRegistry:
    """Mirrors a consumer's 5-stage document pipeline: backend → vision
    → cleanup → normalize → split. Exercises the cascade rule under the
    same structural/content-keyed split that motivated the design."""
    return StageRegistry(
        stages=(
            StageDefinition(
                "backend",
                structural_files=("{stem}.raw.md", "images"),
            ),
            StageDefinition(
                "vision",
                content_keyed_files=(".vision-cache",),
            ),
            StageDefinition(
                "cleanup",
                structural_files=("{stem}.post-cleanup.md",),
            ),
            StageDefinition(
                "normalize",
                structural_files=("{stem}.pre-normalize.md",),
                content_keyed_files=(".heading-normalize-cache",),
            ),
            StageDefinition(
                "split",
                structural_files=("sections", "INDEX.md"),
            ),
        )
    )


def _populate(out: Path, stem: str = "doc") -> dict[str, Path]:
    """Create a full set of cache artifacts. Returns the mapping for
    asserting which were deleted."""
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{stem}.raw.md").write_text("raw")
    (out / "images").mkdir()
    (out / "images" / "image1.png").write_text("png")
    (out / ".vision-cache").mkdir()
    (out / ".vision-cache" / "abc.json").write_text("{}")
    (out / f"{stem}.post-cleanup.md").write_text("post")
    (out / f"{stem}.pre-normalize.md").write_text("pre")
    (out / ".heading-normalize-cache").mkdir()
    (out / ".heading-normalize-cache" / "x.json").write_text("{}")
    (out / "sections").mkdir()
    (out / "sections" / "Intro.md").write_text("# Intro")
    (out / "INDEX.md").write_text("# INDEX")
    return {
        "raw": out / f"{stem}.raw.md",
        "images": out / "images",
        "vision-cache": out / ".vision-cache",
        "post-cleanup": out / f"{stem}.post-cleanup.md",
        "pre-normalize": out / f"{stem}.pre-normalize.md",
        "normalize-cache": out / ".heading-normalize-cache",
        "sections": out / "sections",
        "index": out / "INDEX.md",
    }


def test_stage_registry_stage_names_matches_registered(
    pipeline_registry: StageRegistry,
) -> None:
    assert pipeline_registry.stage_names == (
        "backend",
        "vision",
        "cleanup",
        "normalize",
        "split",
    )


def test_stage_registry_rejects_duplicate_names() -> None:
    with pytest.raises(ValueError, match="duplicate stage name"):
        StageRegistry(
            stages=(
                StageDefinition("a"),
                StageDefinition("b"),
                StageDefinition("a"),
            )
        )


def test_stage_registry_find_unknown_raises(
    pipeline_registry: StageRegistry,
) -> None:
    with pytest.raises(ValueError, match="unknown stage"):
        pipeline_registry.find("bogus")


def test_stage_registry_downstream_returns_target_plus_after(
    pipeline_registry: StageRegistry,
) -> None:
    names = tuple(s.name for s in pipeline_registry.downstream("cleanup"))
    assert names == ("cleanup", "normalize", "split")


def test_files_to_invalidate_stem_placeholder_substituted(
    pipeline_registry: StageRegistry, tmp_path: Path
) -> None:
    paths = files_to_invalidate(
        tmp_path, stage="backend", registry=pipeline_registry, source_stem="manual"
    )
    names = {p.name for p in paths}
    assert "manual.raw.md" in names
    assert "manual.post-cleanup.md" in names
    assert "manual.pre-normalize.md" in names


def test_files_to_invalidate_backend_preserves_content_keyed_caches(
    pipeline_registry: StageRegistry, tmp_path: Path
) -> None:
    """v0.17.4 invariant: --rerun-from backend busts structural files
    only; content-keyed caches (.vision-cache, .heading-normalize-cache)
    are PRESERVED because their phash / content-hash key encodes
    validity directly."""
    paths = files_to_invalidate(
        tmp_path, stage="backend", registry=pipeline_registry, source_stem="doc"
    )
    names = {p.name for p in paths}
    assert names == {
        "doc.raw.md",
        "images",
        "doc.post-cleanup.md",
        "doc.pre-normalize.md",
        "sections",
        "INDEX.md",
    }
    assert ".vision-cache" not in names
    assert ".heading-normalize-cache" not in names


def test_files_to_invalidate_split_only_split_files(
    pipeline_registry: StageRegistry, tmp_path: Path
) -> None:
    paths = files_to_invalidate(
        tmp_path, stage="split", registry=pipeline_registry, source_stem="doc"
    )
    names = {p.name for p in paths}
    assert names == {"sections", "INDEX.md"}


def test_files_to_invalidate_normalize_preserves_vision_cache(
    pipeline_registry: StageRegistry, tmp_path: Path
) -> None:
    """v0.17.4: --rerun-from normalize busts the normalize cache (its
    own content-keyed cache) plus structural files from normalize
    onward. Vision cache is upstream and unrelated — preserved."""
    paths = files_to_invalidate(
        tmp_path, stage="normalize", registry=pipeline_registry, source_stem="doc"
    )
    names = {p.name for p in paths}
    assert names == {
        ".heading-normalize-cache",
        "doc.pre-normalize.md",
        "sections",
        "INDEX.md",
    }
    assert ".vision-cache" not in names


def test_files_to_invalidate_unknown_stage_raises(
    pipeline_registry: StageRegistry, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="unknown stage"):
        files_to_invalidate(tmp_path, stage="bogus", registry=pipeline_registry, source_stem="doc")


def test_invalidate_caches_backend_preserves_content_keyed_caches(
    pipeline_registry: StageRegistry, tmp_path: Path
) -> None:
    """v0.17.4: --rerun-from backend deletes structural files but
    leaves both content-keyed caches alive."""
    files = _populate(tmp_path)
    deleted = invalidate_caches(
        tmp_path, stage="backend", registry=pipeline_registry, source_stem="doc"
    )
    # 6 structural deletions: raw, images, post-cleanup, pre-normalize, sections, INDEX.
    assert len(deleted) == 6
    assert not files["raw"].exists()
    assert not files["images"].exists()
    assert not files["post-cleanup"].exists()
    assert not files["pre-normalize"].exists()
    assert not files["sections"].exists()
    assert not files["index"].exists()
    # Content-keyed caches SURVIVE.
    assert files["vision-cache"].exists()
    assert files["normalize-cache"].exists()


def test_invalidate_caches_vision_preserves_backend(
    pipeline_registry: StageRegistry, tmp_path: Path
) -> None:
    """--rerun-from vision: vision's own cache + downstream structural
    files are gone; backend stays; downstream content-keyed
    (heading-normalize-cache) ALSO stays under the v0.17.4 rule."""
    files = _populate(tmp_path)
    invalidate_caches(tmp_path, stage="vision", registry=pipeline_registry, source_stem="doc")
    assert files["raw"].exists()
    assert files["images"].exists()
    assert not files["vision-cache"].exists()
    assert not files["post-cleanup"].exists()
    assert not files["pre-normalize"].exists()
    assert not files["sections"].exists()
    # v0.17.4: heading-normalize-cache is downstream content-keyed; PRESERVED.
    assert files["normalize-cache"].exists()


def test_invalidate_caches_cleanup_preserves_vision(
    pipeline_registry: StageRegistry, tmp_path: Path
) -> None:
    """--rerun-from cleanup: post-cleanup + downstream structural files
    are gone; vision-cache stays; downstream content-keyed
    (heading-normalize-cache) ALSO stays under the v0.17.4 rule."""
    files = _populate(tmp_path)
    invalidate_caches(tmp_path, stage="cleanup", registry=pipeline_registry, source_stem="doc")
    assert files["raw"].exists()
    assert files["vision-cache"].exists()
    assert not files["post-cleanup"].exists()
    assert not files["pre-normalize"].exists()
    assert not files["sections"].exists()
    # v0.17.4: heading-normalize-cache is downstream content-keyed; PRESERVED.
    assert files["normalize-cache"].exists()


def test_invalidate_caches_normalize_busts_normalize_cache_only(
    pipeline_registry: StageRegistry, tmp_path: Path
) -> None:
    """v0.17.4: --rerun-from normalize busts the normalize cache (its
    OWN content-keyed cache) plus pre-normalize.md + downstream split
    artifacts. Vision cache is upstream and untouched."""
    files = _populate(tmp_path)
    invalidate_caches(tmp_path, stage="normalize", registry=pipeline_registry, source_stem="doc")
    assert files["raw"].exists()
    assert files["images"].exists()
    assert files["vision-cache"].exists()
    assert files["post-cleanup"].exists()
    assert not files["normalize-cache"].exists()
    assert not files["pre-normalize"].exists()
    assert not files["sections"].exists()
    assert not files["index"].exists()


def test_invalidate_caches_split_preserves_everything_else(
    pipeline_registry: StageRegistry, tmp_path: Path
) -> None:
    files = _populate(tmp_path)
    invalidate_caches(tmp_path, stage="split", registry=pipeline_registry, source_stem="doc")
    assert files["raw"].exists()
    assert files["vision-cache"].exists()
    assert files["post-cleanup"].exists()
    assert files["pre-normalize"].exists()
    assert files["normalize-cache"].exists()
    assert not files["sections"].exists()
    assert not files["index"].exists()


def test_invalidate_caches_silently_skips_missing(
    pipeline_registry: StageRegistry, tmp_path: Path
) -> None:
    """Empty output dir → no errors, no deletions."""
    tmp_path.mkdir(exist_ok=True)
    deleted = invalidate_caches(
        tmp_path, stage="split", registry=pipeline_registry, source_stem="doc"
    )
    assert deleted == []


def test_invalidate_caches_nonexistent_output_dir_returns_empty(
    pipeline_registry: StageRegistry, tmp_path: Path
) -> None:
    """`output_dir` doesn't exist at all → return [] gracefully, don't
    raise. Protects callers from surprising the user with a stack trace
    when they pass a typo'd path."""
    missing = tmp_path / "does-not-exist"
    result = invalidate_caches(
        missing, stage="split", registry=pipeline_registry, source_stem="doc"
    )
    assert result == []
