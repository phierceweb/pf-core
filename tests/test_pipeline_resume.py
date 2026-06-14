"""Tests for pf_core.pipeline.resume."""

from __future__ import annotations

import os
from pathlib import Path

from pf_core.pipeline.resume import (
    SnapshotValidator,
    is_snapshot_valid,
    try_resume_from_snapshot,
)

# --- is_snapshot_valid ---------------------------------------------------


def test_is_snapshot_valid_empty_validator_passes_when_file_exists(
    tmp_path: Path,
) -> None:
    """Empty validator only checks snapshot existence."""
    snap = tmp_path / "snap.md"
    snap.write_text("content", encoding="utf-8")
    assert is_snapshot_valid(snap, SnapshotValidator()) is True


def test_is_snapshot_valid_returns_false_when_snapshot_missing(
    tmp_path: Path,
) -> None:
    snap = tmp_path / "snap.md"
    # do not write
    assert is_snapshot_valid(snap, SnapshotValidator()) is False


def test_is_snapshot_valid_returns_false_when_upstream_newer(
    tmp_path: Path,
) -> None:
    """A single upstream file with mtime > snapshot's mtime invalidates."""
    snap = tmp_path / "snap.md"
    snap.write_text("snap", encoding="utf-8")
    upstream = tmp_path / "upstream.md"
    upstream.write_text("up", encoding="utf-8")
    # Make upstream strictly newer than snapshot.
    snap_mtime = snap.stat().st_mtime
    os.utime(upstream, (snap_mtime + 60, snap_mtime + 60))

    validator = SnapshotValidator(upstream_files=(upstream,))
    assert is_snapshot_valid(snap, validator) is False


def test_is_snapshot_valid_returns_false_when_upstream_missing(
    tmp_path: Path,
) -> None:
    """An upstream file that doesn't exist invalidates the snapshot."""
    snap = tmp_path / "snap.md"
    snap.write_text("snap", encoding="utf-8")
    upstream = tmp_path / "nope.md"  # never written

    validator = SnapshotValidator(upstream_files=(upstream,))
    assert is_snapshot_valid(snap, validator) is False


def test_is_snapshot_valid_returns_true_when_upstream_older_or_equal(
    tmp_path: Path,
) -> None:
    """Snapshot mtime >= upstream mtime is valid (equality allowed)."""
    upstream = tmp_path / "upstream.md"
    upstream.write_text("up", encoding="utf-8")
    snap = tmp_path / "snap.md"
    snap.write_text("snap", encoding="utf-8")
    # Force equal mtimes — equality is valid per the rule.
    up_mtime = upstream.stat().st_mtime
    os.utime(snap, (up_mtime, up_mtime))

    validator = SnapshotValidator(upstream_files=(upstream,))
    assert is_snapshot_valid(snap, validator) is True


def test_is_snapshot_valid_glob_returns_false_when_any_match_newer(
    tmp_path: Path,
) -> None:
    snap = tmp_path / "snap.md"
    snap.write_text("snap", encoding="utf-8")
    cache_dir = tmp_path / ".vision-cache"
    cache_dir.mkdir()
    older = cache_dir / "a.json"
    older.write_text("{}", encoding="utf-8")
    newer = cache_dir / "b.json"
    newer.write_text("{}", encoding="utf-8")
    snap_mtime = snap.stat().st_mtime
    # `older` predates snapshot.
    os.utime(older, (snap_mtime - 60, snap_mtime - 60))
    # `newer` postdates snapshot — should invalidate.
    os.utime(newer, (snap_mtime + 60, snap_mtime + 60))

    validator = SnapshotValidator(
        upstream_dirs_glob=((cache_dir, "*.json"),),
    )
    assert is_snapshot_valid(snap, validator) is False


def test_is_snapshot_valid_glob_ignores_missing_dir(tmp_path: Path) -> None:
    """Missing upstream dir is silently treated as no invalidation source."""
    snap = tmp_path / "snap.md"
    snap.write_text("snap", encoding="utf-8")
    missing_dir = tmp_path / "does-not-exist"
    validator = SnapshotValidator(
        upstream_dirs_glob=((missing_dir, "*.json"),),
    )
    assert is_snapshot_valid(snap, validator) is True


def test_is_snapshot_valid_returns_false_when_run_record_missing(
    tmp_path: Path,
) -> None:
    snap = tmp_path / "snap.md"
    snap.write_text("snap", encoding="utf-8")
    run_record = tmp_path / "run.json"  # not written

    validator = SnapshotValidator(run_record_path=run_record)
    assert is_snapshot_valid(snap, validator) is False


def test_is_snapshot_valid_returns_false_when_run_record_unreadable(
    tmp_path: Path,
) -> None:
    """Malformed JSON in the run-record is treated as invalid (no raise)."""
    snap = tmp_path / "snap.md"
    snap.write_text("snap", encoding="utf-8")
    run_record = tmp_path / "run.json"
    run_record.write_text("{not valid json", encoding="utf-8")

    validator = SnapshotValidator(run_record_path=run_record)
    assert is_snapshot_valid(snap, validator) is False


def test_is_snapshot_valid_returns_false_when_flag_mismatch(
    tmp_path: Path,
) -> None:
    snap = tmp_path / "snap.md"
    snap.write_text("snap", encoding="utf-8")
    run_record = tmp_path / "run.json"
    run_record.write_text('{"resolved_flags": {"cleanup": "basic"}}', encoding="utf-8")

    validator = SnapshotValidator(
        run_record_path=run_record,
        flag_keys=("cleanup",),
        current_flags={"cleanup": "aggressive"},
    )
    assert is_snapshot_valid(snap, validator) is False


def test_is_snapshot_valid_returns_true_when_flags_match(
    tmp_path: Path,
) -> None:
    snap = tmp_path / "snap.md"
    snap.write_text("snap", encoding="utf-8")
    run_record = tmp_path / "run.json"
    run_record.write_text('{"resolved_flags": {"cleanup": "basic", "x": 1}}', encoding="utf-8")

    validator = SnapshotValidator(
        run_record_path=run_record,
        flag_keys=("cleanup", "x"),
        current_flags={"cleanup": "basic", "x": 1},
    )
    assert is_snapshot_valid(snap, validator) is True


def test_is_snapshot_valid_run_record_non_dict_flags_invalidates(
    tmp_path: Path,
) -> None:
    """`resolved_flags` not being a dict is graceful-fail to invalid."""
    snap = tmp_path / "snap.md"
    snap.write_text("snap", encoding="utf-8")
    run_record = tmp_path / "run.json"
    run_record.write_text('{"resolved_flags": "oops"}', encoding="utf-8")

    validator = SnapshotValidator(
        run_record_path=run_record,
        flag_keys=("cleanup",),
        current_flags={"cleanup": "basic"},
    )
    assert is_snapshot_valid(snap, validator) is False


# --- try_resume_from_snapshot --------------------------------------------


def test_try_resume_from_snapshot_returns_content_on_valid(
    tmp_path: Path,
) -> None:
    snap = tmp_path / "snap.md"
    snap.write_text("cached body", encoding="utf-8")
    assert try_resume_from_snapshot(snap, SnapshotValidator()) == "cached body"


def test_try_resume_from_snapshot_returns_none_on_invalid(
    tmp_path: Path,
) -> None:
    snap = tmp_path / "snap.md"  # never written
    assert try_resume_from_snapshot(snap, SnapshotValidator()) is None


def test_try_resume_from_snapshot_returns_none_when_file_vanishes_mid_call(
    tmp_path: Path, monkeypatch
) -> None:
    """Race: validator passes, then the file is removed before read.
    The helper must return None, not raise."""
    snap = tmp_path / "snap.md"
    snap.write_text("body", encoding="utf-8")

    # Force the validation path to "succeed" but the read to fail.
    def fake_is_valid(*args, **kwargs) -> bool:
        return True

    from pf_core.pipeline import resume as resume_mod

    monkeypatch.setattr(resume_mod, "is_snapshot_valid", fake_is_valid)
    snap.unlink()  # vanish before read

    assert try_resume_from_snapshot(snap, SnapshotValidator()) is None
