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

from pf_core.exceptions import ConfigurationError
from pf_core.guards.adoption import allowlist_block, baseline_block
from pf_core.guards.comments import total_root_problems
from pf_core.guards.config import (
    HARD_DEFAULT,
    SOFT_DEFAULT,
    GuardsConfig,
    app_rel,
    hard_limit_for,
    load_guards_config,
    prefix_limit,
)
from pf_core.guards.framework_config import FrameworkConfig
from pf_core.guards.layering import (
    filter_allowlisted,
    layering_violations,
    stale_allowlist_entries,
)
from pf_core.guards.opt_in import report_opt_in


_DEFAULT_CONFIG = ".pf-guards.toml"


@dataclass(frozen=True)
class FileSizeViolation:
    path: str  # POSIX, relative to scan root
    lines: int
    limit: int  # the limit that was exceeded
    severity: str  # "hard" or "soft"


def _line_count(p: Path) -> int:
    # Bytes split only where Python ends a row, and no source encoding can fail the read.
    return len(p.read_bytes().splitlines())


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
        if config is not None and a is not None:
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


def stale_baseline_entries(raw: list[FileSizeViolation], *, baseline: dict[str, int]) -> list[str]:
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


def _config_problems(*, hard: int, soft: int, cfg: GuardsConfig, roots: list[str]) -> list[str]:
    """Nonsense-value checks for the resolved gate config (fail loud, exit 2)."""
    out = total_root_problems(roots, cfg.comment_budget) if cfg.comment_budget is not None else []
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pf-guards", description="pf-core structural gate")
    parser.add_argument("--config", default=_DEFAULT_CONFIG)
    parser.add_argument("--root", default=None)
    parser.add_argument("--hard", type=int, default=None)
    parser.add_argument("--soft", type=int, default=None)
    parser.add_argument("--baseline", default=None)
    parser.add_argument(
        "--emit-allowlist",
        action="store_true",
        help="print a paste-ready [tool.pf_guards.layering_allowlist] block for "
        "current violations instead of failing (gate adoption helper)",
    )
    parser.add_argument(
        "--emit-baseline",
        action="store_true",
        help="print a paste-ready [tool.pf_guards.baseline] block grandfathering "
        "every file currently over its hard limit (gate adoption helper)",
    )
    parser.add_argument(
        "--framework",
        action="store_true",
        help="run only the framework check, with every rule on when "
        "[tool.pf_guards.framework] is absent",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="list framework breaches without failing on them (rollout)",
    )
    return parser


def run_cli(argv: list[str] | None = None) -> int:
    """Run the structural gate. Returns the process exit code (0 ok, 1 fail).

    Reads [tool.pf_guards] from --config (default ./.pf-guards.toml); explicit
    flags override config values. A missing config file exits 2 — except the
    default path with --root given, which is an ad-hoc, flag-specified run
    (gate adoption). Runs the file-size gate and the layering checker; a hard
    size violation or any layering violation fails the gate, as does a function
    over max_function_lines or prose over comment_budget when set. The framework check
    runs when [tool.pf_guards.framework] exists, or on --framework (it alone) or
    --report (its breaches listed, never failing).
    """
    args = _parser().parse_args(argv)

    if Path(args.config).is_file():
        try:
            cfg = load_guards_config(args.config)
        except (tomllib.TOMLDecodeError, ValueError, ConfigurationError) as e:
            print(f"pf-guards: malformed config {args.config}: {e}")
            return 2
    elif args.config == _DEFAULT_CONFIG and args.root is not None:
        cfg = GuardsConfig()  # ad-hoc run, fully flag-specified (gate adoption)
    else:
        hint = (
            " — create it with a [tool.pf_guards] table (root = ...), or pass --root for an ad-hoc run"
            if args.config == _DEFAULT_CONFIG
            else ""
        )
        print(f"pf-guards: config not found: {args.config}{hint}")
        return 2
    roots = args.root if args.root is not None else cfg.root
    roots = [roots] if isinstance(roots, str) else list(roots)
    hard = args.hard if args.hard is not None else cfg.hard
    soft = args.soft if args.soft is not None else cfg.soft

    problems = _config_problems(hard=hard, soft=soft, cfg=cfg, roots=roots)
    if problems:
        for p in problems:
            print(f"pf-guards: bad config: {p}")
        return 2
    run_fw = cfg.framework is not None or args.framework or args.report
    fw_cfg = cfg.framework or FrameworkConfig()
    fw_roots = roots if args.root is not None or fw_cfg.root is None else fw_cfg.root
    fw_roots = [fw_roots] if isinstance(fw_roots, str) else list(fw_roots)
    for r in [*roots, *(fw_roots if run_fw else [])]:
        if not Path(r).is_dir():
            print(f"pf-guards: scan root not found: {r} (set [tool.pf_guards] root or --root)")
            return 2
    if args.framework:
        only = report_opt_in(
            fw_roots, GuardsConfig(), framework_roots=fw_roots, framework=fw_cfg, report=args.report
        )
        return 1 if only else 0

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
            print(baseline_block({v.path: v.lines for v in raw if v.severity == "hard"}))
        if args.emit_allowlist:
            print(allowlist_block(layering))
        return 0

    stale_bl = stale_baseline_entries(raw, baseline=baseline)
    stale_al = stale_allowlist_entries(lay_raw, cfg.layering_allowlist)

    hard_v = [v for v in violations if v.severity == "hard"]
    soft_v = [v for v in violations if v.severity == "soft"]
    for v in soft_v:
        print(f"WARN  {v.path}: {v.lines} lines (soft target {v.limit})")
    for v in hard_v:
        print(f"FAIL  {v.path}: {v.lines} lines (hard limit {v.limit})")
    for lv in layering:
        print(f"LAYER {lv.path}:{lv.line}: import {lv.imported} ({lv.reason})")
    for p in stale_bl:
        print(f"STALE baseline entry: {p} (no longer over its hard limit — remove it)")
    for p, m in stale_al:
        print(f"STALE allowlist entry: {p} -> {m} (no longer a violation — remove it)")
    if hard_v:
        print(f"\n{len(hard_v)} file(s) over the hard limit. Split them or update the baseline.")
    if layering:
        print(f"\n{len(layering)} layering violation(s).")
    opt_failed = report_opt_in(
        roots,
        cfg,
        framework_roots=fw_roots if run_fw else None,
        framework=fw_cfg,
        report=args.report,
    )
    return 1 if hard_v or layering or stale_bl or stale_al or opt_failed else 0
