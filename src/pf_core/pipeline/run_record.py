"""Write `<output_dir>/<filename>` after a successful pipeline run.

A run record captures the resolved configuration that produced the
output — preset (if any), every resolved flag value, input file SHA-256,
timestamps, and counts. Used to make re-run drift diagnosable: a
one-line diff between two run-record files shows exactly what config
differs between two outputs.

Schema is consumer-facing — fields beyond `input`, `input_sha256`,
`started_at`, `finished_at`, `version`, `preset`, and `resolved_flags`
are optional. `section_count` / `image_count` are standard optional
counts; consumer-specific metadata goes in `extra=`.

Promoted from a consumer document-extraction pipeline's
`services._run_record` module.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pf_core.log import get_logger

logger = get_logger(__name__)

DEFAULT_FILENAME = "run.json"
_SHA256_BUF_SIZE = 1024 * 1024  # 1 MiB chunks


def file_sha256(path: Path) -> str:
    """Stream a SHA-256 over the input file in bounded-memory chunks.

    Returns the hex digest. Useful as part of a run record so two runs
    against bit-identical inputs produce comparable hashes for drift
    detection.
    """
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(_SHA256_BUF_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class RunRecord:
    """Typed representation of a written run record. The on-disk
    JSON schema mirrors these fields one-for-one (with `extra` flattened
    into the top-level dict on write)."""

    version: str
    preset: str | None
    resolved_flags: dict[str, Any]
    input: str  # filename (basename, not full path) of the source input
    input_sha256: str
    started_at: str
    finished_at: str
    section_count: int | None = None
    image_count: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def write_run_record(
    output_dir: Path,
    *,
    version: str,
    preset: str | None,
    resolved_flags: dict[str, Any],
    input_path: Path,
    started_at: str,
    finished_at: str,
    section_count: int | None = None,
    image_count: int | None = None,
    extra: dict[str, Any] | None = None,
    filename: str = DEFAULT_FILENAME,
) -> Path:
    """Write a run record JSON file to `<output_dir>/<filename>`.

    Returns the written path. Raises OSError on filesystem failure —
    caller decides whether to swallow (most pipelines do, so a write
    failure here doesn't kill an otherwise-successful run).

    The on-disk schema is a flat JSON object combining the standard
    fields with whatever's in `extra` (extra keys override standard
    keys only if a consumer deliberately collides — generally don't).
    """
    record: dict[str, Any] = {
        "version": version,
        "preset": preset,
        "resolved_flags": resolved_flags,
        "input": input_path.name,
        "input_sha256": file_sha256(input_path),
        "started_at": started_at,
        "finished_at": finished_at,
        "section_count": section_count,
        "image_count": image_count,
    }
    if extra:
        record.update(extra)
    target = output_dir / filename
    target.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return target


def read_run_record(
    output_dir: Path,
    *,
    filename: str = DEFAULT_FILENAME,
) -> dict[str, Any] | None:
    """Read a run record from `<output_dir>/<filename>`.

    Returns the parsed dict, or None if the file doesn't exist. Raises
    OSError on read failure; json.JSONDecodeError on malformed JSON.
    Callers typically catch both and return None for "unusable run
    record" so a corrupt file doesn't break downstream gating logic.
    """
    target = output_dir / filename
    if not target.exists():
        return None
    result: dict[str, Any] = json.loads(target.read_text(encoding="utf-8"))
    return result
