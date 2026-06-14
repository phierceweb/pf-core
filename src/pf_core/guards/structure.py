"""Structural guards — file-size and layering checks for the build gate.

Pure, stdlib-only. ``scan_file_sizes`` flags Python files over a hard or soft
line limit; ``filter_baselined`` grandfathers known violations by path -> recorded
line count so the gate can be adopted on a dirty tree and fails only on *new*
violations or *growth* of a baselined file. ``check_layering`` flags upward
imports that violate the four-layer call direction (for consumer apps).
"""
from __future__ import annotations

import argparse
import ast
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FileSizeViolation:
    path: str          # POSIX, relative to scan root
    lines: int
    limit: int         # the limit that was exceeded
    severity: str      # "hard" or "soft"


@dataclass(frozen=True)
class LayeringViolation:
    path: str
    imported: str
    reason: str


def _line_count(p: Path) -> int:
    return len(p.read_text(encoding="utf-8").splitlines())


def scan_file_sizes(
    root: str | Path,
    *,
    hard: int = 500,
    soft: int = 300,
) -> list[FileSizeViolation]:
    """Return file-size violations under ``root`` (recursively, ``*.py`` only).

    A file over ``hard`` is a ``"hard"`` violation; over ``soft`` (but not
    ``hard``) is ``"soft"``. Baseline grandfathering is applied separately by
    :func:`filter_baselined` so this function stays pure and total.
    """
    root = Path(root)
    out: list[FileSizeViolation] = []
    for p in sorted(root.rglob("*.py")):
        n = _line_count(p)
        rel = p.relative_to(root).as_posix()
        if n > hard:
            out.append(FileSizeViolation(path=rel, lines=n, limit=hard, severity="hard"))
        elif n > soft:
            out.append(FileSizeViolation(path=rel, lines=n, limit=soft, severity="soft"))
    return out


def filter_baselined(
    violations: list[FileSizeViolation],
    *,
    baseline: dict[str, int],
) -> list[FileSizeViolation]:
    """Drop hard violations covered by the baseline unless the file has grown.

    Soft violations pass through untouched (they only warn, never block, so
    grandfathering them is pointless). A hard violation is suppressed when the
    file is in ``baseline`` and its current line count is <= the baselined
    count; if it has grown beyond the baselined count, it is reported.
    """
    out: list[FileSizeViolation] = []
    for v in violations:
        if v.severity != "hard":
            out.append(v)
            continue
        recorded = baseline.get(v.path)
        if recorded is not None and v.lines <= recorded:
            continue
        out.append(v)
    return out


# lower index = higher layer; an import is illegal if it targets a higher layer
_LAYER_ORDER = ["cli", "api", "orchestrators", "services", "repo", "clients", "db"]


def _layer_of(rel_posix: str) -> str | None:
    parts = rel_posix.split("/")
    if "app" in parts:
        i = parts.index("app")
        if i + 1 < len(parts) and parts[i + 1] in _LAYER_ORDER:
            return parts[i + 1]
    return None


def _imported_modules(src: str) -> list[str]:
    tree = ast.parse(src)
    mods: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            mods.append(node.module)
        elif isinstance(node, ast.Import):
            mods.extend(a.name for a in node.names)
    return mods


def check_layering(root: str | Path) -> list[LayeringViolation]:
    """Flag imports that violate the four-layer call direction (consumer apps).

    Allowed direction: cli/api -> orchestrators -> services -> repo/clients -> db.
    An import that targets a higher layer is a violation, as is an orchestrator
    importing ``pf_core.db`` (opening ``transaction()``) directly. Files outside
    an ``app/<layer>/`` path are ignored — pf-core itself has no such structure.
    """
    root = Path(root)
    out: list[LayeringViolation] = []
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(root).as_posix()
        layer = _layer_of(rel)
        if layer is None:
            continue
        for mod in _imported_modules(p.read_text(encoding="utf-8")):
            if layer == "orchestrators" and mod in ("pf_core.db", "pf_core.db.connection"):
                out.append(LayeringViolation(
                    path=rel, imported=mod,
                    reason="orchestrator must not open transaction() directly",
                ))
                continue
            parts = mod.split(".")
            if "app" in parts:
                j = parts.index("app")
                target = parts[j + 1] if j + 1 < len(parts) else None
                if target in _LAYER_ORDER and _LAYER_ORDER.index(target) < _LAYER_ORDER.index(layer):
                    out.append(LayeringViolation(
                        path=rel, imported=mod,
                        reason=f"{layer} must not import {target}",
                    ))
    return out


def _load_baseline(path: str | None) -> dict[str, int]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    return {k: int(v) for k, v in json.loads(p.read_text(encoding="utf-8")).items()}


def run_cli(argv: list[str] | None = None) -> int:
    """Run the structural gate. Returns the process exit code (0 ok, 1 hard fail)."""
    parser = argparse.ArgumentParser(prog="pf-guards", description="pf-core structural gate")
    parser.add_argument("--root", default="src")
    parser.add_argument("--hard", type=int, default=500)
    parser.add_argument("--soft", type=int, default=300)
    parser.add_argument("--baseline", default=None)
    args = parser.parse_args(argv)

    baseline = _load_baseline(args.baseline)
    raw = scan_file_sizes(args.root, hard=args.hard, soft=args.soft)
    violations = filter_baselined(raw, baseline=baseline)

    hard = [v for v in violations if v.severity == "hard"]
    soft = [v for v in violations if v.severity == "soft"]
    for v in soft:
        print(f"WARN  {v.path}: {v.lines} lines (soft target {v.limit})")
    for v in hard:
        print(f"FAIL  {v.path}: {v.lines} lines (hard limit {v.limit})")
    if hard:
        print(f"\n{len(hard)} file(s) over the hard limit. Split them or update the baseline.")
        return 1
    return 0
