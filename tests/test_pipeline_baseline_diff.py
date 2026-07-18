"""Tests for pf_core.pipeline.baseline_diff."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pf_core.pipeline.baseline import save_baseline
from pf_core.pipeline.baseline_diff import (
    DiffReport,
    LineCountDelta,
    RunRecordDelta,
    SectionRename,
    SectionSetDelta,
    diff_baseline,
)


def _populate_live_output(out: Path, version: str = "1.1.0") -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "doc.md").write_text("# Doc\n\nbody line 1\nbody line 2\n", encoding="utf-8")
    (out / "INDEX.md").write_text("# INDEX\n", encoding="utf-8")
    sections = out / "sections"
    sections.mkdir(exist_ok=True)
    (sections / "Intro.md").write_text("## Intro\n\nIntro body.\n", encoding="utf-8")
    (sections / "Sub").mkdir(exist_ok=True)
    (sections / "Sub" / "Detail.md").write_text("## Detail\nDetail body.\n", encoding="utf-8")
    (out / "run.json").write_text(
        json.dumps(
            {
                "version": version,
                "preset": "rag-default",
                "input": "doc.html",
                "section_count": 2,
                "image_count": 0,
                "resolved_flags": {"cleanup": "basic", "split_min_level": 2},
                "started_at": "2026-05-10T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )


def _save_initial_baseline(out: Path, label: str = "v1") -> None:
    save_baseline(out, label=label)


def test_diff_baseline_unchanged_run_record_section_set_and_bodies(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _populate_live_output(out)
    _save_initial_baseline(out, label="v1")

    report = diff_baseline(out, label="v1")

    assert report.run_record.changed_fields == {}
    assert report.sections.added == []
    assert report.sections.removed == []
    assert report.sections.renamed == []
    assert report.body_changes == []


def test_diff_baseline_run_record_field_change(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _populate_live_output(out, version="1.1.0")
    _save_initial_baseline(out, label="v1")
    # Mutate the live run.json.
    run_record = json.loads((out / "run.json").read_text(encoding="utf-8"))
    run_record["version"] = "1.1.2"
    run_record["section_count"] = 5
    run_record["resolved_flags"]["cleanup"] = "aggressive"
    (out / "run.json").write_text(json.dumps(run_record), encoding="utf-8")

    report = diff_baseline(out, label="v1")

    assert report.run_record.changed_fields == {
        "version": ("1.1.0", "1.1.2"),
        "section_count": (2, 5),
        "resolved_flags.cleanup": ("basic", "aggressive"),
    }


def test_diff_baseline_section_added(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _populate_live_output(out)
    _save_initial_baseline(out, label="v1")
    (out / "sections" / "New.md").write_text("## New\n", encoding="utf-8")

    report = diff_baseline(out, label="v1")

    assert "New.md" in report.sections.added
    assert report.sections.removed == []
    assert report.sections.renamed == []


def test_diff_baseline_section_removed(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _populate_live_output(out)
    _save_initial_baseline(out, label="v1")
    (out / "sections" / "Sub" / "Detail.md").unlink()

    report = diff_baseline(out, label="v1")

    assert "Sub/Detail.md" in report.sections.removed
    assert report.sections.added == []


def test_diff_baseline_section_renamed_via_body_hash_match(tmp_path: Path) -> None:
    """Identical body → renamed regardless of folder."""
    out = tmp_path / "out"
    _populate_live_output(out)
    _save_initial_baseline(out, label="v1")
    body = (out / "sections" / "Sub" / "Detail.md").read_text(encoding="utf-8")
    (out / "sections" / "Sub" / "Detail.md").unlink()
    (out / "sections" / "Renamed.md").write_text(body, encoding="utf-8")

    report = diff_baseline(out, label="v1")

    assert len(report.sections.renamed) == 1
    rename = report.sections.renamed[0]
    assert rename.old_path == "Sub/Detail.md"
    assert rename.new_path == "Renamed.md"
    assert rename.similarity == 1.0


def test_diff_baseline_section_renamed_via_levenshtein_and_similarity(tmp_path: Path) -> None:
    """Same parent folder + close basename + similar body → renamed."""
    out = tmp_path / "out"
    _populate_live_output(out)
    _save_initial_baseline(out, label="v1")
    detail = out / "sections" / "Sub" / "Detail.md"
    body = detail.read_text(encoding="utf-8")
    detail.unlink()
    # Slightly different name, slightly different body.
    new = out / "sections" / "Sub" / "Detai1.md"  # 'l' → '1', distance=1
    new.write_text(body + "extra line\n", encoding="utf-8")

    report = diff_baseline(out, label="v1")

    assert len(report.sections.renamed) == 1
    rename = report.sections.renamed[0]
    assert rename.old_path == "Sub/Detail.md"
    assert rename.new_path == "Sub/Detai1.md"
    assert 0.8 <= rename.similarity < 1.0


def test_diff_baseline_no_rename_when_distance_too_large(tmp_path: Path) -> None:
    """Same folder + distant basename → keep as add+remove, not rename."""
    out = tmp_path / "out"
    _populate_live_output(out)
    _save_initial_baseline(out, label="v1")
    body = (out / "sections" / "Sub" / "Detail.md").read_text(encoding="utf-8")
    (out / "sections" / "Sub" / "Detail.md").unlink()
    (out / "sections" / "Sub" / "TotallyDifferentName.md").write_text(
        body + "extra\n", encoding="utf-8"
    )

    report = diff_baseline(out, label="v1")

    # Body hash isn't identical (extra line added), distance > 4 → no rename.
    assert "Sub/Detail.md" in report.sections.removed
    assert "Sub/TotallyDifferentName.md" in report.sections.added
    assert report.sections.renamed == []


def test_diff_baseline_body_change_line_counts(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _populate_live_output(out)
    _save_initial_baseline(out, label="v1")
    intro = out / "sections" / "Intro.md"
    intro.write_text("## Intro\n\nIntro body.\nNew line 1.\nNew line 2.\n", encoding="utf-8")

    report = diff_baseline(out, label="v1")

    bc = [c for c in report.body_changes if c.path == "Intro.md"]
    assert len(bc) == 1
    assert bc[0].plus == 2
    assert bc[0].minus == 0


def test_diff_baseline_body_changes_sorted_by_total_descending(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _populate_live_output(out)
    _save_initial_baseline(out, label="v1")
    # Big change + small change.
    (out / "sections" / "Intro.md").write_text("## Intro\n" + "new\n" * 20, encoding="utf-8")
    (out / "sections" / "Sub" / "Detail.md").write_text(
        "## Detail\nDetail body changed.\n", encoding="utf-8"
    )

    report = diff_baseline(out, label="v1")

    assert len(report.body_changes) >= 2
    # Top result should be the file with the most line changes.
    assert report.body_changes[0].path == "Intro.md"


def test_diff_baseline_renamed_pairs_excluded_from_body_changes(tmp_path: Path) -> None:
    """A renamed section appears in `renamed`, not in `body_changes`."""
    out = tmp_path / "out"
    _populate_live_output(out)
    _save_initial_baseline(out, label="v1")
    body = (out / "sections" / "Sub" / "Detail.md").read_text(encoding="utf-8")
    (out / "sections" / "Sub" / "Detail.md").unlink()
    (out / "sections" / "Renamed.md").write_text(body, encoding="utf-8")

    report = diff_baseline(out, label="v1")

    assert all(c.path != "Sub/Detail.md" for c in report.body_changes)
    assert all(c.path != "Renamed.md" for c in report.body_changes)


def test_diff_baseline_unknown_label_raises(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _populate_live_output(out)

    with pytest.raises(ValueError, match="no baseline labeled"):
        diff_baseline(out, label="nope")


def test_diff_report_dataclass_fields() -> None:
    """Public return shape — DiffReport composes the three sub-deltas."""
    report = DiffReport(
        baseline_label="v1",
        baseline_path=Path("/tmp/.baselines/v1"),
        current_path=Path("/tmp"),
        run_record=RunRecordDelta(changed_fields={}),
        sections=SectionSetDelta(added=[], removed=[], renamed=[]),
        body_changes=[],
    )
    assert report.baseline_label == "v1"
    assert report.run_record.changed_fields == {}
    # Reference unused symbols to keep the import smoke-check meaningful.
    _ = LineCountDelta(path="x.md", plus=0, minus=0)
    _ = SectionRename(old_path="a", new_path="b", similarity=1.0)
