"""Structural guards — file-size and layering checks for the build gate.

Pure, stdlib-only. ``scan_file_sizes`` flags Python files over a hard or soft
line limit; ``filter_baselined`` grandfathers known violations by path -> recorded
line count so the gate can be adopted on a dirty tree and fails only on *new*
violations or *growth* of a baselined file. ``check_layering`` flags upward
imports that violate the four-layer call direction (for consumer apps).
"""
from __future__ import annotations

import argparse
import json
import tomllib
from dataclasses import dataclass
from pathlib import Path

from pf_core.guards.config import (
    HARD_DEFAULT,
    SOFT_DEFAULT,
    GuardsConfig,
    app_rel,
    hard_limit_for,
    load_guards_config,
    prefix_limit,
)
from pf_core.guards.layering import (
    filter_allowlisted,
    layering_violations,
    stale_allowlist_entries,
)


@dataclass(frozen=True)
class FileSizeViolation:
    path: str          # POSIX, relative to scan root
    lines: int
    limit: int         # the limit that was exceeded
    severity: str      # "hard" or "soft"


def _line_count(p: Path) -> int:
    return len(p.read_text(encoding="utf-8").splitlines())


def scan_file_sizes(
    root: str | Path,
    *,
    hard: int = HARD_DEFAULT,
    soft: int = SOFT_DEFAULT,
    config: GuardsConfig | None = None,
    path_prefix: str = "",
) -> list[FileSizeViolation]:
    """Return file-size violations under ``root`` (recursively, ``*.py`` only).

    Files under an ``app/<layer>/`` tree (when ``config`` is given) use the
    per-layer hard limit with soft = ``int(hard * SOFT_FRACTION)``; other files
    use a matching ``[tool.pf_guards.limits]`` prefix budget if any, else the
    flat ``hard``/``soft``. ``path_prefix`` (multi-root scans) is prepended to
    reported paths and participates in prefix matching. Baseline grandfathering
    is applied separately by :func:`filter_baselined` so this stays pure and total.
    """
    root = Path(root)
    out: list[FileSizeViolation] = []
    for p in sorted(root.rglob("*.py")):
        n = _line_count(p)
        rel = p.relative_to(root).as_posix()
        shown = f"{path_prefix}{rel}"
        a = app_rel(root, rel) if config is not None else None
        if a is not None:
            file_hard = hard_limit_for(a, config)
            file_soft = int(file_hard * config.soft_fraction)
        else:
            file_hard, file_soft = hard, soft
            if config is not None:
                cap = prefix_limit(shown, config.limits)
                if cap is not None:
                    file_hard, file_soft = cap, int(cap * config.soft_fraction)
        if n > file_hard:
            out.append(FileSizeViolation(path=shown, lines=n, limit=file_hard, severity="hard"))
        elif n > file_soft:
            out.append(FileSizeViolation(path=shown, lines=n, limit=file_soft, severity="soft"))
    return out


def stale_baseline_entries(
    raw: list[FileSizeViolation], *, baseline: dict[str, int]
) -> list[str]:
    """Baseline entries whose file is no longer over its hard limit — remove them.

    The ratchet's other half: once a file is split below budget, dead
    grandfathering FAILs the gate until the entry is deleted.
    """
    over = {v.path for v in raw if v.severity == "hard"}
    return sorted(p for p in baseline if p not in over)


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


def _load_baseline(path: str | None) -> dict[str, int]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    return {k: int(v) for k, v in json.loads(p.read_text(encoding="utf-8")).items()}


def _config_problems(*, hard: int, soft: int, cfg: GuardsConfig) -> list[str]:
    """Nonsense-value checks for the resolved gate config (fail loud, exit 2)."""
    out: list[str] = []
    if hard <= 0:
        out.append(f"hard must be positive (got {hard})")
    if soft <= 0:
        out.append(f"soft must be positive (got {soft})")
    if cfg.util <= 0:
        out.append(f"util must be positive (got {cfg.util})")
    if not 0 < cfg.soft_fraction <= 1:
        out.append(f"soft_fraction must be in (0, 1] (got {cfg.soft_fraction})")
    bad_layers = {k: v for k, v in cfg.layers.items() if v <= 0}
    if bad_layers:
        out.append(f"layers limits must be positive (got {bad_layers})")
    bad_baseline = {k: v for k, v in cfg.baseline.items() if v <= 0}
    if bad_baseline:
        out.append(f"baseline counts must be positive (got {bad_baseline})")
    return out


def run_cli(argv: list[str] | None = None) -> int:
    """Run the structural gate. Returns the process exit code (0 ok, 1 fail).

    Reads [tool.pf_guards] from --config (default ./pyproject.toml); explicit
    flags override config values. Runs the file-size gate and the layering
    checker; a hard size violation or any layering violation fails the gate.
    """
    parser = argparse.ArgumentParser(prog="pf-guards", description="pf-core structural gate")
    parser.add_argument("--config", default="pyproject.toml")
    parser.add_argument("--root", default=None)
    parser.add_argument("--hard", type=int, default=None)
    parser.add_argument("--soft", type=int, default=None)
    parser.add_argument("--baseline", default=None)
    parser.add_argument(
        "--emit-allowlist", action="store_true",
        help="print a paste-ready [tool.pf_guards.layering_allowlist] block for "
             "current violations instead of failing (gate adoption helper)",
    )
    parser.add_argument(
        "--emit-baseline", action="store_true",
        help="print a paste-ready [tool.pf_guards.baseline] block grandfathering "
             "every file currently over its hard limit (gate adoption helper)",
    )
    args = parser.parse_args(argv)

    try:
        cfg = load_guards_config(args.config)
    except (tomllib.TOMLDecodeError, ValueError) as e:
        print(f"pf-guards: malformed config {args.config}: {e}")
        return 2
    roots = args.root if args.root is not None else cfg.root
    roots = [roots] if isinstance(roots, str) else list(roots)
    hard = args.hard if args.hard is not None else cfg.hard
    soft = args.soft if args.soft is not None else cfg.soft

    problems = _config_problems(hard=hard, soft=soft, cfg=cfg)
    if problems:
        for p in problems:
            print(f"pf-guards: bad config: {p}")
        return 2
    for r in roots:
        if not Path(r).is_dir():
            print(f"pf-guards: scan root not found: {r} (set [tool.pf_guards] root or --root)")
            return 2

    multi = len(roots) > 1
    raw: list[FileSizeViolation] = []
    lay_raw: list = []
    for r in roots:
        prefix = f"{r.rstrip('/')}/" if multi else ""
        raw += scan_file_sizes(r, hard=hard, soft=soft, config=cfg, path_prefix=prefix)
        lay_raw += layering_violations(r, config=cfg)

    baseline = _load_baseline(args.baseline) if args.baseline is not None else cfg.baseline
    violations = filter_baselined(raw, baseline=baseline)
    layering = filter_allowlisted(lay_raw, cfg.layering_allowlist)

    if args.emit_baseline or args.emit_allowlist:
        if args.emit_baseline:
            _print_baseline_block(raw)
        if args.emit_allowlist:
            _print_allowlist_block(layering)
        return 0

    stale_bl = stale_baseline_entries(raw, baseline=baseline)
    stale_al = stale_allowlist_entries(lay_raw, cfg.layering_allowlist)

    hard_v = [v for v in violations if v.severity == "hard"]
    soft_v = [v for v in violations if v.severity == "soft"]
    for v in soft_v:
        print(f"WARN  {v.path}: {v.lines} lines (soft target {v.limit})")
    for v in hard_v:
        print(f"FAIL  {v.path}: {v.lines} lines (hard limit {v.limit})")
    for v in layering:
        print(f"LAYER {v.path}:{v.line}: import {v.imported} ({v.reason})")
    for p in stale_bl:
        print(f"STALE baseline entry: {p} (no longer over its hard limit — remove it)")
    for p, m in stale_al:
        print(f"STALE allowlist entry: {p} -> {m} (no longer a violation — remove it)")
    if hard_v:
        print(f"\n{len(hard_v)} file(s) over the hard limit. Split them or update the baseline.")
    if layering:
        print(f"\n{len(layering)} layering violation(s).")
    return 1 if hard_v or layering or stale_bl or stale_al else 0


def _print_baseline_block(raw: list[FileSizeViolation]) -> None:
    """Paste-ready TOML grandfathering every file currently over its hard limit."""
    print("[tool.pf_guards.baseline]")
    for v in sorted((v for v in raw if v.severity == "hard"), key=lambda v: v.path):
        print(f'"{v.path}" = {v.lines}')


def _print_allowlist_block(violations: list) -> None:
    """Paste-ready TOML for the current (un-allowlisted) layering violations."""
    print("[tool.pf_guards.layering_allowlist]")
    by_path: dict[str, set[str]] = {}
    for v in violations:
        by_path.setdefault(v.path, set()).add(v.imported)
    for path in sorted(by_path):
        mods = ", ".join(f'"{m}"' for m in sorted(by_path[path]))
        print(f'"{path}" = [{mods}]')
