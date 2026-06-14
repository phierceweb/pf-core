"""Baseline snapshots — preserve a result for later comparison.

A baseline copies result artifacts (consolidated md, sections, INDEX.md,
run record) from a live output dir into a labeled subdirectory under
`<output_dir>/<baselines_dir_name>/<label>/`. Cache files are NOT copied.

Two entry points:

1. `save_baseline()` — explicit.
2. `auto_snapshot_on_version_change()` — implicit, intended to be called
   from a pipeline's dispatcher when version changes between runs.

Promoted from a consumer document-extraction pipeline. Configurable via
`BaselineConfig` for filename / dir-name conventions; defaults match a
generic pipeline tool.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pf_core.log import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class BaselineConfig:
    """Filename / directory conventions for baseline operations.

    Defaults match a generic pipeline. Override per-field for consumers
    that use different filenames (e.g., a consumer overriding
    `run_record_filename` to a project-prefixed dotfile).
    """

    run_record_filename: str = "run.json"
    sections_dir_name: str = "sections"
    index_file_name: str = "INDEX.md"
    baselines_dir_name: str = ".baselines"
    # Pattern for resolving the consolidated markdown filename given the
    # source input's stem. `{stem}` is the only placeholder.
    consolidated_md_pattern: str = "{stem}.md"


DEFAULT_CONFIG = BaselineConfig()


@dataclass(frozen=True)
class BaselineRecord:
    """Public return shape of `save_baseline` and `list_baselines`."""

    label: str
    path: Path
    consolidated_md: Path
    version: str
    preset: str | None
    saved_at: str
    section_count: int | None
    image_count: int | None
    extra: dict[str, Any] = field(default_factory=dict)


def _read_run_record(output_dir: Path, config: BaselineConfig) -> dict[str, Any]:
    """Read and parse the run record. Raises ValueError on missing or
    unreadable run record."""
    run_record_path = output_dir / config.run_record_filename
    if not run_record_path.exists():
        raise ValueError(f"no {config.run_record_filename} in {output_dir}; nothing to baseline.")
    try:
        data: dict[str, Any] = json.loads(run_record_path.read_text(encoding="utf-8"))
        return data
    except (OSError, ValueError) as e:
        raise ValueError(f"could not read run record at {run_record_path}: {e}") from e


def _consolidated_md_path(
    output_dir: Path, run_record: dict[str, Any], config: BaselineConfig
) -> Path:
    """Find the consolidated md based on the run record's `input` field +
    the config's `consolidated_md_pattern`."""
    input_name = run_record.get("input")
    if not input_name:
        raise ValueError(f"run record missing `input` field: {output_dir}")
    filename = config.consolidated_md_pattern.format(stem=Path(input_name).stem)
    return output_dir / filename


def _now_utc_compact() -> str:
    """`YYYYMMDD-HHMMSS` UTC."""
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def save_baseline(
    output_dir: Path,
    *,
    label: str | None = None,
    config: BaselineConfig = DEFAULT_CONFIG,
) -> BaselineRecord:
    """Snapshot the current live output into `.baselines/<label>/`.

    Default label: `<version>-<YYYYMMDD-HHMMSS>`.
    Raises ValueError on missing run record or label collision.
    """
    run_record = _read_run_record(output_dir, config)
    consolidated = _consolidated_md_path(output_dir, run_record, config)
    version = str(run_record.get("version", "unknown"))
    if label is None:
        label = f"{version}-{_now_utc_compact()}"

    base_dir = output_dir / config.baselines_dir_name / label
    if base_dir.exists():
        raise ValueError(
            f"baseline label {label!r} already exists at {base_dir}; "
            f"pass a different label or remove the directory."
        )

    base_dir.mkdir(parents=True, exist_ok=False)

    if consolidated.exists():
        shutil.copy2(consolidated, base_dir / consolidated.name)

    index = output_dir / config.index_file_name
    if index.exists():
        shutil.copy2(index, base_dir / config.index_file_name)

    sections = output_dir / config.sections_dir_name
    if sections.is_dir():
        shutil.copytree(sections, base_dir / config.sections_dir_name)

    shutil.copy2(
        output_dir / config.run_record_filename,
        base_dir / config.run_record_filename,
    )

    record = BaselineRecord(
        label=label,
        path=base_dir,
        consolidated_md=base_dir / consolidated.name,
        version=version,
        preset=run_record.get("preset"),
        saved_at=str(run_record.get("started_at", "")),
        section_count=run_record.get("section_count"),
        image_count=run_record.get("image_count"),
    )
    logger.info("baseline_saved label=%s path=%s", label, base_dir)
    return record


def list_baselines(
    output_dir: Path,
    *,
    config: BaselineConfig = DEFAULT_CONFIG,
) -> list[BaselineRecord]:
    """Return all baselines, sorted by saved-at descending."""
    baselines_dir = output_dir / config.baselines_dir_name
    if not baselines_dir.is_dir():
        return []

    records: list[BaselineRecord] = []
    for entry in baselines_dir.iterdir():
        if not entry.is_dir():
            continue
        run_record_path = entry / config.run_record_filename
        if not run_record_path.exists():
            logger.warning("baseline_missing_run_record path=%s", entry)
            continue
        try:
            data = json.loads(run_record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("baseline_unreadable_run_record path=%s error=%s", entry, e)
            continue

        input_name = data.get("input", "")
        if input_name:
            consolidated_name = config.consolidated_md_pattern.format(stem=Path(input_name).stem)
            consolidated_path = entry / consolidated_name
        else:
            consolidated_path = entry
        records.append(
            BaselineRecord(
                label=entry.name,
                path=entry,
                consolidated_md=consolidated_path,
                version=str(data.get("version", "unknown")),
                preset=data.get("preset"),
                saved_at=str(data.get("started_at", "")),
                section_count=data.get("section_count"),
                image_count=data.get("image_count"),
            )
        )

    records.sort(key=lambda r: (r.saved_at, r.label), reverse=True)
    return records


def auto_snapshot_on_version_change(
    output_dir: Path,
    *,
    current_version: str,
    config: BaselineConfig = DEFAULT_CONFIG,
) -> BaselineRecord | None:
    """Snapshot the previous output into `.baselines/<previous-version>/`
    when the previous run's version differs from `current_version`.

    Skipped (returns None) when no run record exists, version unchanged,
    or previous run produced no sections. Failures are caught + logged
    as WARNING; never propagated — the caller's pipeline must not be
    killed by an unwritable baseline path.
    """
    run_record_path = output_dir / config.run_record_filename
    if not run_record_path.exists():
        return None

    try:
        prev = json.loads(run_record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("auto_snapshot_skipped reason=unreadable_run_record error=%s", e)
        return None

    prev_version = str(prev.get("version", ""))
    if not prev_version or prev_version == current_version:
        return None

    sections = output_dir / config.sections_dir_name
    if not sections.is_dir() or not any(sections.iterdir()):
        logger.info("auto_snapshot_skipped reason=empty_sections version=%s", prev_version)
        return None

    label = prev_version
    base_dir = output_dir / config.baselines_dir_name / label
    if base_dir.exists():
        label = f"{prev_version}-{_now_utc_compact()}"

    try:
        record = save_baseline(output_dir, label=label, config=config)
        logger.info("auto_baselined label=%s path=%s", record.label, record.path)
        return record
    except (OSError, ValueError) as e:
        logger.warning("auto_snapshot_failed reason=%s", e)
        return None
