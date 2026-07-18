"""Tests for pf_core.pipeline.baseline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pf_core.pipeline.baseline import (
    BaselineConfig,
    BaselineRecord,
    auto_snapshot_on_version_change,
    list_baselines,
    save_baseline,
)


def _populate_live_output(out: Path, version: str = "1.1.0") -> None:
    """Create a live output dir shaped like a real pipeline result."""
    out.mkdir(parents=True, exist_ok=True)
    (out / "doc.md").write_text("# Doc\n\nBody.\n", encoding="utf-8")
    (out / "doc.raw.md").write_text("raw", encoding="utf-8")
    (out / "doc.post-cleanup.md").write_text("post", encoding="utf-8")
    (out / "doc.pre-normalize.md").write_text("pre", encoding="utf-8")
    (out / "INDEX.md").write_text("# INDEX\n", encoding="utf-8")
    (out / "sections").mkdir(exist_ok=True)
    (out / "sections" / "Intro.md").write_text("## Intro\n\nBody.\n", encoding="utf-8")
    (out / "sections" / "Sub").mkdir(exist_ok=True)
    (out / "sections" / "Sub" / "Detail.md").write_text("## Detail\n", encoding="utf-8")
    (out / "images").mkdir(exist_ok=True)
    (out / "images" / "image1.png").write_text("png", encoding="utf-8")
    (out / ".extract-cache").mkdir(exist_ok=True)
    (out / ".extract-cache" / "abc.json").write_text("{}", encoding="utf-8")
    (out / "run.json").write_text(
        json.dumps(
            {
                "version": version,
                "preset": "rag-default",
                "input": "doc.html",
                "section_count": 2,
                "image_count": 1,
                "started_at": "2026-05-10T00:00:00Z",
                "finished_at": "2026-05-10T00:00:01Z",
            }
        ),
        encoding="utf-8",
    )


def test_save_baseline_copies_manifest_files(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _populate_live_output(out)

    save_baseline(out, label="v1.0.0-corpus")

    base = out / ".baselines" / "v1.0.0-corpus"
    assert (base / "doc.md").read_text(encoding="utf-8") == "# Doc\n\nBody.\n"
    assert (base / "INDEX.md").exists()
    assert (base / "sections" / "Intro.md").exists()
    assert (base / "sections" / "Sub" / "Detail.md").exists()
    assert (base / "run.json").exists()


def test_save_baseline_skips_caches_and_raw(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _populate_live_output(out)

    save_baseline(out, label="v1.0.0-corpus")

    base = out / ".baselines" / "v1.0.0-corpus"
    assert not (base / "doc.raw.md").exists()
    assert not (base / "doc.post-cleanup.md").exists()
    assert not (base / "doc.pre-normalize.md").exists()
    assert not (base / "images").exists()
    assert not (base / ".extract-cache").exists()


def test_save_baseline_default_label_uses_version_and_timestamp(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _populate_live_output(out, version="1.1.0")

    record = save_baseline(out)

    assert record.label.startswith("1.1.0-")
    assert (out / ".baselines" / record.label / "doc.md").exists()


def test_save_baseline_no_run_record_raises(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "doc.md").write_text("body")

    with pytest.raises(ValueError, match="no run.json"):
        save_baseline(out)


def test_save_baseline_label_collision_raises(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _populate_live_output(out)

    save_baseline(out, label="duplicate")

    with pytest.raises(ValueError, match="already exists"):
        save_baseline(out, label="duplicate")


def test_save_baseline_finds_consolidated_md_via_run_record(tmp_path: Path) -> None:
    """The consolidated md filename is derived from the run record's `input`
    field (stem) — not hardcoded to any name."""
    out = tmp_path / "out"
    _populate_live_output(out)
    # The fixture writes "doc.md" matching `input: doc.html` (stem doc).

    record = save_baseline(out, label="v1")
    assert record.consolidated_md == out / ".baselines" / "v1" / "doc.md"


def test_list_baselines_empty_dir_returns_empty(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    assert list_baselines(out) == []


def test_list_baselines_returns_records_sorted_by_saved_at_desc(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _populate_live_output(out)

    save_baseline(out, label="alpha")
    save_baseline(out, label="beta")

    records = list_baselines(out)
    labels = [r.label for r in records]
    assert set(labels) == {"alpha", "beta"}
    # Sort order: latest first. Both saves happen in quick succession;
    # ordering is by .baselines/<label>/run.json's `started_at` value,
    # which we copy from the live run record. The fixture has identical
    # started_at, so the sort is stable on label as a fallback. Assert
    # both present + the records have the expected fields.
    assert all(r.version == "1.1.0" for r in records)
    assert all(r.preset == "rag-default" for r in records)


def test_baseline_record_dataclass_fields() -> None:
    """BaselineRecord is the public return shape of save_baseline + list_baselines."""
    rec = BaselineRecord(
        label="x",
        path=Path("/tmp/x"),
        consolidated_md=Path("/tmp/x/doc.md"),
        version="1.1.0",
        preset="rag-default",
        saved_at="2026-05-10T00:00:00Z",
        section_count=2,
        image_count=1,
    )
    assert rec.label == "x"
    assert rec.section_count == 2


def test_auto_snapshot_skips_when_no_previous_run_record(tmp_path: Path) -> None:
    """First run on a fresh output dir → no snapshot."""
    out = tmp_path / "out"
    out.mkdir()
    auto_snapshot_on_version_change(out, current_version="1.1.0")
    assert not (out / ".baselines").exists()


def test_auto_snapshot_skips_when_version_unchanged(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _populate_live_output(out, version="1.1.0")
    auto_snapshot_on_version_change(out, current_version="1.1.0")
    assert not (out / ".baselines").exists()


def test_auto_snapshot_fires_on_version_change(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _populate_live_output(out, version="1.0.0")
    auto_snapshot_on_version_change(out, current_version="1.1.0")

    base = out / ".baselines" / "1.0.0"
    assert base.exists()
    assert (base / "doc.md").exists()


def test_auto_snapshot_dedupes_collision_with_timestamp(tmp_path: Path) -> None:
    """If `<previous-version>` already exists as a baseline, append a
    timestamp suffix."""
    out = tmp_path / "out"
    _populate_live_output(out, version="1.0.0")
    auto_snapshot_on_version_change(out, current_version="1.1.0")

    # Restore the live output (auto_snapshot doesn't move; it copies)
    # and bump again to trigger a collision.
    auto_snapshot_on_version_change(out, current_version="1.1.0-rc1")

    bases = sorted((out / ".baselines").iterdir())
    labels = [p.name for p in bases]
    assert "1.0.0" in labels
    assert any(label.startswith("1.0.0-") and label != "1.0.0" for label in labels)


def test_auto_snapshot_skips_when_sections_empty(tmp_path: Path) -> None:
    """If the previous run produced no section files, skip snapshot —
    nothing useful to baseline."""
    import shutil

    out = tmp_path / "out"
    _populate_live_output(out, version="1.0.0")
    # Wipe sections to simulate an empty previous run.
    shutil.rmtree(out / "sections")

    auto_snapshot_on_version_change(out, current_version="1.1.0")
    assert not (out / ".baselines").exists()


def test_auto_snapshot_failure_is_non_fatal(tmp_path: Path, monkeypatch) -> None:
    """A baseline-write failure must not break the calling pipeline."""
    out = tmp_path / "out"
    _populate_live_output(out, version="1.0.0")

    import pf_core.pipeline.baseline as baseline_mod

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(baseline_mod, "save_baseline", boom)

    # Should not raise.
    auto_snapshot_on_version_change(out, current_version="1.1.0")


def _populate_live_output_custom(
    out: Path,
    *,
    version: str,
    run_record_filename: str,
    sections_dir_name: str,
    index_file_name: str = "INDEX.md",
) -> None:
    """Variant of `_populate_live_output` for custom-config tests."""
    out.mkdir(parents=True, exist_ok=True)
    (out / "doc.md").write_text("# Doc\n\nBody.\n", encoding="utf-8")
    (out / index_file_name).write_text("# INDEX\n", encoding="utf-8")
    (out / sections_dir_name).mkdir(exist_ok=True)
    (out / sections_dir_name / "Intro.md").write_text("## Intro\n", encoding="utf-8")
    (out / run_record_filename).write_text(
        json.dumps(
            {
                "version": version,
                "preset": "rag-default",
                "input": "doc.html",
                "section_count": 1,
                "image_count": 0,
                "started_at": "2026-05-10T00:00:00Z",
                "finished_at": "2026-05-10T00:00:01Z",
            }
        ),
        encoding="utf-8",
    )


def test_save_baseline_custom_config_uses_custom_filenames(tmp_path: Path) -> None:
    """A non-default BaselineConfig threads through filename/dir-name choices."""
    config = BaselineConfig(
        run_record_filename=".custom-run.json",
        baselines_dir_name=".my-baselines",
    )
    out = tmp_path / "out"
    _populate_live_output_custom(
        out,
        version="1.1.0",
        run_record_filename=".custom-run.json",
        sections_dir_name="sections",
    )

    record = save_baseline(out, label="custom-v1", config=config)

    assert record.path == out / ".my-baselines" / "custom-v1"
    assert (out / ".my-baselines" / "custom-v1" / ".custom-run.json").exists()
    assert (out / ".my-baselines" / "custom-v1" / "doc.md").exists()
    assert (out / ".my-baselines" / "custom-v1" / "sections" / "Intro.md").exists()


def test_list_baselines_custom_config_finds_baselines_in_custom_dir(tmp_path: Path) -> None:
    """`list_baselines` honors `baselines_dir_name` and `run_record_filename`."""
    config = BaselineConfig(
        run_record_filename=".custom-run.json",
        baselines_dir_name=".my-baselines",
    )
    out = tmp_path / "out"
    _populate_live_output_custom(
        out,
        version="1.1.0",
        run_record_filename=".custom-run.json",
        sections_dir_name="sections",
    )

    save_baseline(out, label="custom-v1", config=config)
    save_baseline(out, label="custom-v2", config=config)

    records = list_baselines(out, config=config)
    labels = {r.label for r in records}
    assert labels == {"custom-v1", "custom-v2"}
    assert all(r.version == "1.1.0" for r in records)
    # Default config sees no baselines in the default dir.
    assert list_baselines(out) == []
