"""[tool.pf_guards] configuration + per-layer limit resolution for the gate.

Stdlib-only. The gate's machine-read surface is a repo-root .pf-guards.toml —
not pyproject.toml, not files under .ai/.
"""

from __future__ import annotations

import difflib
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from pf_core.exceptions import ConfigurationError
from pf_core.guards.comments import CommentBudget, parse_comment_budget
from pf_core.guards.framework_config import FrameworkConfig, parse_framework_config

# Default limit values — every one of these is overridable via [tool.pf_guards].
LAYER_DEFAULTS = {"cli": 100, "api": 300, "services": 300, "repo": 300, "orchestrators": 400}
UTIL_LIMIT = 150  # _util*.py anywhere under an app tree ([tool.pf_guards] util)
SOFT_FRACTION = 0.8  # layer soft warn = fraction of hard ([tool.pf_guards] soft_fraction)
HARD_DEFAULT = 500  # flat hard limit ([tool.pf_guards] hard)
SOFT_DEFAULT = 300  # flat soft target ([tool.pf_guards] soft)


_KEYS = frozenset(
    "root baseline hard soft util soft_fraction layers limits allowed_imports "
    "layering_allowlist framework max_function_lines comment_budget".split()
)
_FUNCTIONS = "[tool.pf_guards.max_function_lines]"
_FUNCTION_KEYS = frozenset({"hard", "soft", "layers", "limits"})


@dataclass(frozen=True)
class FunctionLimits:
    """Function-length limits in lines, counted from the ``def`` line to the last line."""

    hard: int
    soft: int
    soft_fraction: float = SOFT_FRACTION  # soft for a layer or prefix limit = its hard x this
    layers: dict[str, int] = field(default_factory=dict)  # app/<layer>/ hard limits
    limits: dict[str, int] = field(default_factory=dict)  # path-prefix hard limits, longest wins


@dataclass(frozen=True)
class GuardsConfig:
    root: str | list[str] = "src"  # one scan root, or several (paths then get root-prefixed)
    baseline: dict[str, int] = field(default_factory=dict)  # path -> grandfathered line count
    hard: int = HARD_DEFAULT
    soft: int = SOFT_DEFAULT
    util: int = UTIL_LIMIT
    soft_fraction: float = SOFT_FRACTION
    layers: dict[str, int] = field(default_factory=dict)  # overrides LAYER_DEFAULTS
    limits: dict[str, int] = field(default_factory=dict)  # path-prefix overrides, longest wins
    # Layering-rule overrides: per-layer allow-sets (per-key replace over the built-in
    # ALLOWED_IMPORTS; new keys declare new checked layers)…
    allowed_imports: dict[str, list[str]] = field(default_factory=dict)
    # …and the grandfather list: app-relative path -> imported modules permitted
    # despite the rules (exact module match; the visible burn-down list).
    layering_allowlist: dict[str, list[str]] = field(default_factory=dict)
    framework: FrameworkConfig | None = None  # [tool.pf_guards.framework]; None = check off
    max_function_lines: FunctionLimits | None = None  # None = function-length check off
    comment_budget: CommentBudget | None = None  # [tool.pf_guards.comment_budget]; None = off


def load_guards_config(path: str | Path = ".pf-guards.toml") -> GuardsConfig:
    """Read [tool.pf_guards] from ``path``; absent file or section -> all defaults.

    The CLI refuses to run on a missing config (exit 2) — the silent-defaults
    path here is for programmatic callers only. An unknown key, or a malformed
    framework, max_function_lines or comment_budget value, raises ``ConfigurationError``.
    """
    p = Path(path)
    if not p.is_file():
        return GuardsConfig()
    tool = tomllib.loads(p.read_text(encoding="utf-8")).get("tool", {}).get("pf_guards", {})
    _refuse_unknown_keys(tool)
    raw_root = tool.get("root", "src")
    raw_baseline = tool.get("baseline", {})
    if not isinstance(raw_baseline, dict):
        raise ValueError(
            '[tool.pf_guards] baseline must be a table of "path" = line_count '
            "(a JSON file path is only valid for the --baseline CLI flag)"
        )
    soft_fraction = float(tool.get("soft_fraction", SOFT_FRACTION))
    raw_functions = tool.get("max_function_lines")
    return GuardsConfig(
        root=[str(x) for x in raw_root] if isinstance(raw_root, list) else str(raw_root),
        baseline={k: int(v) for k, v in raw_baseline.items()},
        hard=int(tool.get("hard", HARD_DEFAULT)),
        soft=int(tool.get("soft", SOFT_DEFAULT)),
        util=int(tool.get("util", UTIL_LIMIT)),
        soft_fraction=soft_fraction,
        layers={k: int(v) for k, v in tool.get("layers", {}).items()},
        limits={k: int(v) for k, v in tool.get("limits", {}).items()},
        allowed_imports={
            k: [str(x) for x in v] for k, v in tool.get("allowed_imports", {}).items()
        },
        layering_allowlist={
            k: [str(x) for x in v] for k, v in tool.get("layering_allowlist", {}).items()
        },
        framework=parse_framework_config(tool["framework"]) if "framework" in tool else None,
        max_function_lines=None
        if raw_functions is None
        else parse_function_limits(raw_functions, soft_fraction=soft_fraction),
        comment_budget=parse_comment_budget(tool["comment_budget"])
        if "comment_budget" in tool
        else None,
    )


def _refuse_unknown_keys(tool: dict[str, object]) -> None:
    """A misspelled key would leave the check it names off, so every unknown one is refused."""
    unknown = sorted(set(tool) - _KEYS)
    if not unknown:
        return
    named = [
        f"{k!r}"
        + "".join(f" (did you mean {m!r}?)" for m in difflib.get_close_matches(k, _KEYS, 1))
        for k in unknown
    ]
    raise ConfigurationError(
        f"[tool.pf_guards]: unknown key(s) {', '.join(named)}; allowed: {sorted(_KEYS)}"
    )


def parse_function_limits(raw: object, *, soft_fraction: float) -> FunctionLimits:
    """Validate ``max_function_lines``: a hard limit, or a table of hard / soft / layers / limits.

    ``soft`` defaults to ``hard x soft_fraction``.

    Raises:
        ConfigurationError: an unknown key, a missing hard limit, or a limit that is not a
            positive whole number of lines.
    """
    table = {"hard": raw} if isinstance(raw, int) and not isinstance(raw, bool) else raw
    if not isinstance(table, dict):
        raise ConfigurationError(f"{_FUNCTIONS} must be a line count or a table with hard = N")
    unknown = sorted(set(table) - _FUNCTION_KEYS)
    if unknown:
        raise ConfigurationError(f"{_FUNCTIONS}: unknown key(s) {unknown}")
    if "hard" not in table:
        raise ConfigurationError(f"{_FUNCTIONS} needs hard = <lines>")
    hard = _lines(table["hard"], "hard")
    return FunctionLimits(
        hard=hard,
        soft=_lines(table["soft"], "soft") if "soft" in table else int(hard * soft_fraction),
        soft_fraction=soft_fraction,
        layers=_line_table(table.get("layers", {}), "layers"),
        limits=_line_table(table.get("limits", {}), "limits"),
    )


def _lines(value: object, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigurationError(
            f"{_FUNCTIONS} {key} must be a positive line count (got {value!r})"
        )
    return value


def _line_table(value: object, key: str) -> dict[str, int]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"{_FUNCTIONS} {key} must be a table of name = lines")
    return {str(k): _lines(v, f"{key}.{k}") for k, v in value.items()}


def app_rel(root: Path, rel: str) -> str | None:
    """Normalize to an app-relative path ('app/<layer>/...'), or None outside app trees.

    Handles both scan shapes: root above the app dir (rel contains an 'app'
    segment) and root *being* the app dir (root.name == 'app').
    """
    parts = rel.split("/")
    if "app" in parts:
        return "/".join(parts[parts.index("app") :])
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


def function_limits_for(path: str, app_path: str | None, limits: FunctionLimits) -> tuple[int, int]:
    """``(hard, soft)`` for a function in ``path``.

    Precedence: prefix limit (longest wins, matched on the app-relative path under an app
    tree) > per-layer limit > flat hard / soft.
    """
    cap = prefix_limit(app_path or path, limits.limits)
    if cap is None and app_path is not None:
        seg = app_path.split("/")
        cap = limits.layers.get(seg[1]) if len(seg) >= 2 else None
    if cap is None:
        return limits.hard, limits.soft
    return cap, int(cap * limits.soft_fraction)
