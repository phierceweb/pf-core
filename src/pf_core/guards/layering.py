"""Layered-import checker for consumer app/ trees.

Enforces the four-layer call direction from layering.md with explicit
per-layer allow-sets (an order-based comparison would wrongly allow
orchestrators to import repo). Reports line numbers and a friendly hint;
``# lint-layers: skip`` in the first 5 lines exempts a file.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from pf_core.guards.config import GuardsConfig, app_rel

ALLOWED_IMPORTS: dict[str, set[str]] = {
    "api": {"services", "orchestrators", "db"},
    "cli": {"services", "orchestrators", "db"},
    "orchestrators": {"services", "db"},
    "services": {"repo", "clients", "db"},
    "repo": {"db"},
    "clients": {"db"},
    "db": set(),   # the bottom layer: imports no app layer above it
}

_HINTS: dict[tuple[str, str], str] = {
    ("api", "repo"): "should go through services",
    ("cli", "repo"): "should go through services",
    ("orchestrators", "repo"): "should go through services",
    ("orchestrators", "clients"): "should go through services",
    ("repo", "services"): "repo must not import from upper layers",
    ("repo", "orchestrators"): "repo must not import from upper layers",
    ("clients", "services"): "clients must not import from upper layers",
    ("clients", "orchestrators"): "clients must not import from upper layers",
    ("services", "orchestrators"): "services must not import from orchestrators",
    ("services", "api"): "services must not import from entry points",
    ("services", "cli"): "services must not import from entry points",
    ("db", "services"): "db must not import from upper layers",
    ("db", "repo"): "db must not import from upper layers",
    ("db", "orchestrators"): "db must not import from upper layers",
}

_DB_MODULES = ("pf_core.db", "pf_core.db.connection")


@dataclass(frozen=True)
class LayeringViolation:
    path: str
    imported: str
    reason: str
    line: int = 0


def _skip_comment(p: Path) -> bool:
    try:
        with p.open(encoding="utf-8") as fh:
            return any("lint-layers: skip" in line for _, line in zip(range(5), fh, strict=False))
    except OSError:
        return True


def _imports(tree: ast.AST, pkg: list[str]) -> list[tuple[str, int]]:
    """Absolute-ized ``(module, lineno)`` pairs for every import in ``tree``.

    Relative imports are resolved against ``pkg`` — the importing file's
    package path (``['app', 'services']`` for ``app/services/x.py``) — so
    ``from ..repo import entries`` becomes ``app.repo.entries`` and
    ``from .. import repo`` becomes ``app.repo``. A relative import that
    climbs above the scan root is skipped (nothing there is an app layer).
    """
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((a.name, node.lineno) for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module:
                    out.append((node.module, node.lineno))
                continue
            up = node.level - 1
            if up > len(pkg):
                continue
            base = pkg[: len(pkg) - up]
            if node.module:
                out.append((".".join([*base, node.module]), node.lineno))
            else:
                out.extend((".".join([*base, a.name]), node.lineno) for a in node.names)
    return out


def layering_violations(
    root: str | Path, *, config: GuardsConfig | None = None
) -> list[LayeringViolation]:
    """All layering violations under ``root`` — allowlist NOT applied.

    ``config.allowed_imports`` overrides the built-in allow-sets per key (a new
    key declares a new checked layer). Violation paths are always app-relative
    (``app/…``) regardless of the scan-root shape, so allowlist keys and
    reports stay stable.

    Files outside an ``app/<layer>/`` path, under a ``tests/`` directory, named
    ``conftest.py``, carrying the skip comment, or unparseable are ignored —
    pf-core itself has no app layers, so its own tree is a no-op.
    """
    root = Path(root)
    allowed: dict[str, set[str]] = {k: set(v) for k, v in ALLOWED_IMPORTS.items()}
    if config is not None:
        allowed.update({k: set(v) for k, v in config.allowed_imports.items()})
    out: list[LayeringViolation] = []
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(root).as_posix()
        a = app_rel(root, rel)
        if a is None:
            continue
        parts = a.split("/")
        layer = parts[1] if len(parts) >= 2 and parts[1] in allowed else None
        if layer is None or "tests" in parts or p.name == "conftest.py" or _skip_comment(p):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        pkg = a.split("/")[:-1]
        for mod, lineno in _imports(tree, pkg):
            if layer == "orchestrators" and mod in _DB_MODULES:
                out.append(LayeringViolation(
                    path=a, imported=mod, line=lineno,
                    reason="orchestrator must not open transaction() directly",
                ))
                continue
            mparts = mod.split(".")
            if "app" not in mparts:
                continue
            j = mparts.index("app")
            target = mparts[j + 1] if j + 1 < len(mparts) else None
            if target in allowed and target != layer and target not in allowed[layer]:
                hint = _HINTS.get((layer, target), "not allowed")
                out.append(LayeringViolation(
                    path=a, imported=mod, line=lineno,
                    reason=f"{layer} → {target}, {hint}",
                ))
    return out


def filter_allowlisted(
    violations: list[LayeringViolation], allowlist: dict[str, list[str]]
) -> list[LayeringViolation]:
    """Drop violations named in the allowlist (exact path + imported module)."""
    permitted = {(p, m) for p, mods in allowlist.items() for m in mods}
    return [v for v in violations if (v.path, v.imported) not in permitted]


def stale_allowlist_entries(
    violations: list[LayeringViolation], allowlist: dict[str, list[str]]
) -> list[tuple[str, str]]:
    """Allowlist entries that no longer match any violation — remove them.

    The ratchet: an exception must stay a real exception; once fixed, the
    entry FAILs the gate until deleted (so the list only shrinks).
    """
    current = {(v.path, v.imported) for v in violations}
    return sorted(
        (p, m) for p, mods in allowlist.items() for m in mods if (p, m) not in current
    )


def check_layering(
    root: str | Path, *, config: GuardsConfig | None = None
) -> list[LayeringViolation]:
    """Layering violations with the config's allowlist applied (public API)."""
    raw = layering_violations(root, config=config)
    if config is None:
        return raw
    return filter_allowlisted(raw, config.layering_allowlist)
