"""Resume helpers — skip downstream work when an upstream snapshot is valid.

Pipelines often write intermediate snapshots (post-extraction, post-cleanup,
etc.) so a re-run can short-circuit past expensive upstream phases. This
module provides a generic validator + reader.

The validity model is mtime-based plus optional run-record flag matching:

  - The snapshot must exist.
  - Its mtime must be >= every upstream file's mtime (catches "upstream
    changed since the snapshot was written").
  - Its mtime must be >= the latest file matching any upstream-glob spec
    (e.g., `<output_dir>/.extract-cache/*.json` — catches "an intermediate
    cache was updated after the snapshot, so the snapshot is stale").
  - If a run-record path is given, the snapshot is invalid unless the
    record exists, parses as JSON, and its `resolved_flags` match the
    current run's values for every `flag_keys` entry.

The helpers return `str | None` (raw snapshot content); callers hydrate
their own result types from that content.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pf_core.log import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class SnapshotValidator:
    """Rules for deciding when a snapshot is fresh enough to reuse.

    Each rule's failure causes `is_snapshot_valid()` to return False.
    All rules are optional — an empty validator only checks snapshot
    existence (useful for simple "is the cache there?" checks).
    """

    upstream_files: tuple[Path, ...] = ()
    """Files whose mtime must be <= the snapshot's mtime. If any of these
    is missing, the snapshot is invalid (you don't have valid upstream
    state to validate against)."""

    upstream_dirs_glob: tuple[tuple[Path, str], ...] = ()
    """`(dir, glob_pattern)` pairs. Every file in `dir.glob(pattern)`
    must have mtime <= the snapshot's mtime. Missing dirs are
    silently ignored (no upstream files == no invalidation source)."""

    run_record_path: Path | None = None
    """If set, the snapshot is invalid unless this file exists and is
    parseable JSON."""

    flag_keys: tuple[str, ...] = ()
    """Keys to look up in the run record's `resolved_flags` dict. For
    each key, `current_flags[key]` must equal the run record's value."""

    current_flags: dict[str, Any] = field(default_factory=dict)
    """Current run's flag values to compare against the run record."""


def is_snapshot_valid(snapshot_path: Path, validator: SnapshotValidator) -> bool:
    """Return True if the snapshot passes every validation rule."""
    if not snapshot_path.exists():
        return False
    snap_mtime = snapshot_path.stat().st_mtime

    # Upstream files: snapshot must be at least as new as each.
    for upstream in validator.upstream_files:
        if not upstream.exists():
            return False
        if snap_mtime < upstream.stat().st_mtime:
            return False

    # Upstream globs: snapshot must be at least as new as the latest
    # matching file.
    for upstream_dir, pattern in validator.upstream_dirs_glob:
        if not upstream_dir.is_dir():
            continue  # no upstream files to invalidate against
        for f in upstream_dir.glob(pattern):
            try:
                f_mtime = f.stat().st_mtime
            except OSError:
                # File vanished between glob and stat; treat as no
                # invalidation source for this file.
                continue
            if f_mtime > snap_mtime:
                return False

    # Run record + flag-set match.
    if validator.run_record_path is not None:
        if not validator.run_record_path.exists():
            return False
        try:
            prev = json.loads(validator.run_record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        prev_flags = prev.get("resolved_flags", {})
        if not isinstance(prev_flags, dict):
            return False
        for key in validator.flag_keys:
            if prev_flags.get(key) != validator.current_flags.get(key):
                return False

    return True


def try_resume_from_snapshot(
    snapshot_path: Path,
    validator: SnapshotValidator,
) -> str | None:
    """Return the snapshot's text contents if valid, else None.

    Consumer-friendly wrapper: validates via `is_snapshot_valid()` and
    reads the file on a hit. Errors during read return None (don't
    propagate — if the file vanishes between validation and read, the
    caller should re-run the upstream).
    """
    if not is_snapshot_valid(snapshot_path, validator):
        return None
    try:
        return snapshot_path.read_text(encoding="utf-8")
    except OSError:
        return None
