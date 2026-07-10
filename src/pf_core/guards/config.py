"""[tool.pf_guards] configuration + per-layer limit resolution for the gate.

Stdlib-only. The gate's machine-read surface lives in pyproject.toml (config)
and a repo-root baseline JSON — not under .ai/.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# Default limit values — every one of these is overridable via [tool.pf_guards].
LAYER_DEFAULTS = {"cli": 100, "api": 300, "services": 300, "repo": 300, "orchestrators": 400}
UTIL_LIMIT = 150       # _util*.py anywhere under an app tree ([tool.pf_guards] util)
SOFT_FRACTION = 0.8    # layer soft warn = fraction of hard ([tool.pf_guards] soft_fraction)
HARD_DEFAULT = 500     # flat hard limit ([tool.pf_guards] hard)
SOFT_DEFAULT = 300     # flat soft target ([tool.pf_guards] soft)


@dataclass(frozen=True)
class GuardsConfig:
    root: str | list[str] = "src"   # one scan root, or several (paths then get root-prefixed)
    baseline: dict[str, int] = field(default_factory=dict)   # path -> grandfathered line count
    hard: int = HARD_DEFAULT
    soft: int = SOFT_DEFAULT
    util: int = UTIL_LIMIT
    soft_fraction: float = SOFT_FRACTION
    layers: dict[str, int] = field(default_factory=dict)   # overrides LAYER_DEFAULTS
    limits: dict[str, int] = field(default_factory=dict)   # path-prefix overrides, longest wins
    # Layering-rule overrides: per-layer allow-sets (per-key replace over the built-in
    # ALLOWED_IMPORTS; new keys declare new checked layers)…
    allowed_imports: dict[str, list[str]] = field(default_factory=dict)
    # …and the grandfather list: app-relative path -> imported modules permitted
    # despite the rules (exact module match; the visible burn-down list).
    layering_allowlist: dict[str, list[str]] = field(default_factory=dict)


def load_guards_config(pyproject: str | Path = "pyproject.toml") -> GuardsConfig:
    """Read [tool.pf_guards]; absent file or section -> all defaults."""
    p = Path(pyproject)
    if not p.is_file():
        return GuardsConfig()
    tool = tomllib.loads(p.read_text(encoding="utf-8")).get("tool", {}).get("pf_guards", {})
    raw_root = tool.get("root", "src")
    raw_baseline = tool.get("baseline", {})
    if not isinstance(raw_baseline, dict):
        raise ValueError(
            '[tool.pf_guards] baseline must be a table of "path" = line_count '
            "(a JSON file path is only valid for the --baseline CLI flag)"
        )
    return GuardsConfig(
        root=[str(x) for x in raw_root] if isinstance(raw_root, list) else str(raw_root),
        baseline={k: int(v) for k, v in raw_baseline.items()},
        hard=int(tool.get("hard", HARD_DEFAULT)),
        soft=int(tool.get("soft", SOFT_DEFAULT)),
        util=int(tool.get("util", UTIL_LIMIT)),
        soft_fraction=float(tool.get("soft_fraction", SOFT_FRACTION)),
        layers={k: int(v) for k, v in tool.get("layers", {}).items()},
        limits={k: int(v) for k, v in tool.get("limits", {}).items()},
        allowed_imports={
            k: [str(x) for x in v] for k, v in tool.get("allowed_imports", {}).items()
        },
        layering_allowlist={
            k: [str(x) for x in v] for k, v in tool.get("layering_allowlist", {}).items()
        },
    )


def app_rel(root: Path, rel: str) -> str | None:
    """Normalize to an app-relative path ('app/<layer>/...'), or None outside app trees.

    Handles both scan shapes: root above the app dir (rel contains an 'app'
    segment) and root *being* the app dir (root.name == 'app').
    """
    parts = rel.split("/")
    if "app" in parts:
        return "/".join(parts[parts.index("app"):])
    if Path(root).name == "app":
        return f"app/{rel}"
    return None


def prefix_limit(path: str, limits: dict[str, int]) -> int | None:
    """Longest-prefix [tool.pf_guards.limits] match for ``path``, or None."""
    matches = [
        (prefix, cap)
        for prefix, cap in limits.items()
        if path == prefix or path.startswith(prefix.rstrip("/") + "/")
    ]
    if matches:
        return max(matches, key=lambda kv: len(kv[0]))[1]
    return None


def hard_limit_for(app_path: str, cfg: GuardsConfig) -> int:
    """Hard line limit for an app-tree file.

    Precedence: [tool.pf_guards.limits] prefix override (longest wins) >
    _util*.py special case > per-layer limit > flat hard.
    """
    cap = prefix_limit(app_path, cfg.limits)
    if cap is not None:
        return cap
    if app_path.rsplit("/", 1)[-1].startswith("_util"):
        return cfg.util
    layers = {**LAYER_DEFAULTS, **cfg.layers}
    seg = app_path.split("/")
    if len(seg) >= 2 and seg[1] in layers:
        return layers[seg[1]]
    return cfg.hard
