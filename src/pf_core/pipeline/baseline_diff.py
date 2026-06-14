"""Baseline diff — compare a saved baseline against the live output.

Returns a structured `DiffReport` covering:

1. Run-record field-level diff (dotted paths into the run record JSON).
2. Section filename set diff (added / removed / renamed).
3. Per-section line-count rollup for files present on both sides.

Rename detection is conservative: exact body sha256 match (similarity
1.0) OR same-folder + Levenshtein basename ≤ 4 + body similarity ≥ 0.8.
Bias is toward "added+removed" over false "renamed" claims.

Generalized from production pipeline use. Configurable via
`BaselineConfig` for filename / dir-name conventions; defaults match a
generic pipeline tool.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from pf_core.log import get_logger

from .baseline import DEFAULT_CONFIG, BaselineConfig

logger = get_logger(__name__)


@dataclass(frozen=True)
class RunRecordDelta:
    """Field-level diff of the run record JSON. Keys are dotted paths
    into the JSON (e.g. `resolved_flags.cleanup`)."""

    changed_fields: dict[str, tuple[object, object]]
    """Maps `field_path → (baseline_value, current_value)`."""


@dataclass(frozen=True)
class SectionRename:
    old_path: str  # relative to sections/
    new_path: str
    similarity: float  # 0.0–1.0


@dataclass(frozen=True)
class SectionSetDelta:
    added: list[str]
    removed: list[str]
    renamed: list[SectionRename]


@dataclass(frozen=True)
class LineCountDelta:
    path: str  # relative to sections/
    plus: int
    minus: int


@dataclass(frozen=True)
class DiffReport:
    baseline_label: str
    baseline_path: Path
    current_path: Path
    run_record: RunRecordDelta
    sections: SectionSetDelta
    body_changes: list[LineCountDelta]


# Tunables — conservative on purpose. Bias toward "added+removed" over
# false "renamed" claims.
_RENAME_LEVENSHTEIN_MAX = 4
_RENAME_SIMILARITY_MIN = 0.8


def _flatten(d: object, prefix: str = "") -> dict[str, object]:
    """Recursively flatten a JSON-shaped dict into dotted-path keys.
    Lists and scalars become leaf values."""
    if not isinstance(d, dict):
        return {prefix: d} if prefix else {}
    out: dict[str, object] = {}
    for k, v in d.items():
        path = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, path))
        else:
            out[path] = v
    return out


def _diff_run_records(baseline: dict[str, Any], current: dict[str, Any]) -> RunRecordDelta:
    """Return field-level diff. Includes fields present in either side
    but with different values; skips fields where both sides match."""
    flat_b = _flatten(baseline)
    flat_c = _flatten(current)
    keys = set(flat_b) | set(flat_c)
    changed: dict[str, tuple[object, object]] = {}
    for k in sorted(keys):
        bv = flat_b.get(k)
        cv = flat_c.get(k)
        if bv != cv:
            changed[k] = (bv, cv)
    return RunRecordDelta(changed_fields=changed)


def _walk_sections(sections_dir: Path) -> dict[str, Path]:
    """Map of `<relative-path-string> → absolute-path` for every .md file
    under `sections_dir`. Empty dict if dir absent."""
    if not sections_dir.is_dir():
        return {}
    out: dict[str, Path] = {}
    for p in sections_dir.rglob("*.md"):
        if p.is_file():
            rel = p.relative_to(sections_dir).as_posix()
            out[rel] = p
    return out


def _body_hash(path: Path) -> str:
    """sha256 of the file's bytes. Used for body-equality rename detection."""
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _levenshtein(a: str, b: str) -> int:
    """Standard Levenshtein distance. Bounded by `max(len(a), len(b))`."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            curr[j] = min(
                prev[j] + 1,
                curr[j - 1] + 1,
                prev[j - 1] + (0 if ca == cb else 1),
            )
        prev = curr
    return prev[-1]


def _body_similarity(a: Path, b: Path) -> float:
    """0.0–1.0 similarity ratio (difflib.SequenceMatcher)."""
    return SequenceMatcher(
        None,
        a.read_text(encoding="utf-8"),
        b.read_text(encoding="utf-8"),
    ).ratio()


def _detect_renames(
    baseline_files: dict[str, Path],
    current_files: dict[str, Path],
    added: list[str],
    removed: list[str],
) -> tuple[list[SectionRename], set[str], set[str]]:
    """Match removed→added pairs as renames using two heuristics:

    1. body sha256 match (similarity = 1.0), regardless of path.
    2. same parent folder + Levenshtein basename ≤ 4 + body
       similarity ≥ 0.8.

    Returns (renames, removed_paired, added_paired) so the caller can
    exclude paired paths from the final added/removed lists.
    """
    removed_hashes = {p: _body_hash(baseline_files[p]) for p in removed}
    added_hashes = {p: _body_hash(current_files[p]) for p in added}

    renames: list[SectionRename] = []
    removed_paired: set[str] = set()
    added_paired: set[str] = set()

    # Pass 1: exact body-hash match.
    for old_p, old_hash in removed_hashes.items():
        for new_p, new_hash in added_hashes.items():
            if new_p in added_paired:
                continue
            if old_hash == new_hash:
                renames.append(SectionRename(old_path=old_p, new_path=new_p, similarity=1.0))
                removed_paired.add(old_p)
                added_paired.add(new_p)
                break

    # Pass 2: same-folder + close basename + similar body.
    for old_p in removed:
        if old_p in removed_paired:
            continue
        old_parent = str(Path(old_p).parent)
        old_basename = Path(old_p).name
        best: tuple[str, float] | None = None
        for new_p in added:
            if new_p in added_paired:
                continue
            if str(Path(new_p).parent) != old_parent:
                continue
            if _levenshtein(old_basename, Path(new_p).name) > _RENAME_LEVENSHTEIN_MAX:
                continue
            sim = _body_similarity(baseline_files[old_p], current_files[new_p])
            if sim < _RENAME_SIMILARITY_MIN:
                continue
            if best is None or sim > best[1]:
                best = (new_p, sim)
        if best is not None:
            renames.append(SectionRename(old_path=old_p, new_path=best[0], similarity=best[1]))
            removed_paired.add(old_p)
            added_paired.add(best[0])

    return renames, removed_paired, added_paired


def _line_counts(baseline_path: Path, current_path: Path) -> tuple[int, int]:
    """Return `(plus, minus)` — lines added in current vs baseline."""
    base_lines = baseline_path.read_text(encoding="utf-8").splitlines()
    curr_lines = current_path.read_text(encoding="utf-8").splitlines()
    matcher = SequenceMatcher(None, base_lines, curr_lines)
    plus = minus = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "delete":
            minus += i2 - i1
        elif tag == "insert":
            plus += j2 - j1
        elif tag == "replace":
            minus += i2 - i1
            plus += j2 - j1
    return plus, minus


def diff_baseline(
    output_dir: Path,
    *,
    label: str,
    config: BaselineConfig = DEFAULT_CONFIG,
) -> DiffReport:
    """Compare `<output_dir>/<baselines_dir_name>/<label>/` to the
    current live output. Raises ValueError if the label doesn't exist."""
    baseline_dir = output_dir / config.baselines_dir_name / label
    if not baseline_dir.is_dir():
        raise ValueError(
            f"no baseline labeled {label!r} at {baseline_dir}; "
            f"check available labels via `list_baselines()`."
        )

    # 1. Run-record diff.
    base_record = json.loads(
        (baseline_dir / config.run_record_filename).read_text(encoding="utf-8")
    )
    curr_record_path = output_dir / config.run_record_filename
    curr_record = (
        json.loads(curr_record_path.read_text(encoding="utf-8"))
        if curr_record_path.exists()
        else {}
    )
    run_delta = _diff_run_records(base_record, curr_record)

    # 2. Section filename set + rename detection.
    base_files = _walk_sections(baseline_dir / config.sections_dir_name)
    curr_files = _walk_sections(output_dir / config.sections_dir_name)
    base_set = set(base_files)
    curr_set = set(curr_files)
    added = sorted(curr_set - base_set)
    removed = sorted(base_set - curr_set)

    renames, removed_paired, added_paired = _detect_renames(base_files, curr_files, added, removed)

    section_delta = SectionSetDelta(
        added=[p for p in added if p not in added_paired],
        removed=[p for p in removed if p not in removed_paired],
        renamed=sorted(renames, key=lambda r: r.old_path),
    )

    # 3. Per-section line-count rollup. Skip rename pairs (they're
    # accounted for by the renamed list); skip files only on one side.
    common = (base_set & curr_set) - removed_paired - added_paired
    body_changes: list[LineCountDelta] = []
    for rel in sorted(common):
        plus, minus = _line_counts(base_files[rel], curr_files[rel])
        if plus or minus:
            body_changes.append(LineCountDelta(path=rel, plus=plus, minus=minus))
    body_changes.sort(key=lambda c: (-(c.plus + c.minus), c.path))

    return DiffReport(
        baseline_label=label,
        baseline_path=baseline_dir,
        current_path=output_dir,
        run_record=run_delta,
        sections=section_delta,
        body_changes=body_changes,
    )
