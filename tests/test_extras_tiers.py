"""Enforce the extras tier contract on pf-core's own source tree.

A module may only import, at module scope, things inside its own extra's closure
(read from pyproject). Break that and a consumer who installed only the importer's
tier hits an ImportError the maintainer never sees — a dev venv has every extra.
Imports nested in a function are the deliberate lazy-gate pattern and are ignored.
"""

from __future__ import annotations

import ast
import importlib
import tomllib
from pathlib import Path

import pytest

from pf_core._extras import _MODULE_EXTRA, required_extra

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src" / "pf_core"
_PYPROJECT = _ROOT / "pyproject.toml"


def _extras() -> dict[str, list[str]]:
    return tomllib.loads(_PYPROJECT.read_text())["project"]["optional-dependencies"]


def _closure(extra: str | None) -> set[str]:
    """Every extra implied by installing *extra* (itself included)."""
    if extra is None:
        return set()
    extras = _extras()
    seen: set[str] = set()
    stack = [extra]
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        for dep in extras.get(name, []):
            if dep.startswith("pf-core["):
                stack += [p.strip() for p in dep[len("pf-core[") : dep.rindex("]")].split(",")]
    return seen


def _module_name(path: Path) -> str:
    rel = path.relative_to(_SRC).with_suffix("")
    parts = [p for p in rel.parts if p != "__init__"]
    return ".".join(["pf_core", *parts])


def _module_scope_pf_core_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[str] = []
    for node in ast.walk(tree):
        if getattr(node, "col_offset", 0) != 0:  # nested = deliberate lazy gate
            continue
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("pf_core"):
            out.append(node.module)
        elif isinstance(node, ast.Import):
            out += [a.name for a in node.names if a.name.startswith("pf_core")]
    return out


def _source_files() -> list[Path]:
    return [p for p in sorted(_SRC.rglob("*.py")) if "__pycache__" not in p.parts]


# (importer prefix, target prefix) pairs allowed to cross tiers. Add one only
# with a reason; the staleness test below forces its removal once it stops
# matching a real edge, so this list can only shrink.
_ALLOWED_CROSS_TIER: set[tuple[str, str]] = set()


def _edges() -> list[tuple[str, str, str | None, str]]:
    """(importer, target, importer_extra, target_extra) for every cross-tier edge."""
    out = []
    for path in _source_files():
        importer = _module_name(path)
        allowed = _closure(required_extra(importer))
        for target in _module_scope_pf_core_imports(path):
            needed = required_extra(target)
            if needed is not None and needed not in allowed:
                out.append((importer, target, required_extra(importer), needed))
    return out


def _exempt(importer: str, target: str) -> bool:
    return any(
        (importer == i or importer.startswith(i + "."))
        and (target == t or target.startswith(t + "."))
        for i, t in _ALLOWED_CROSS_TIER
    )


def test_no_module_scope_import_escapes_its_tier() -> None:
    offenders = [
        f"{imp} [{imp_x or 'base'}] imports {tgt} [{tgt_x}] at module scope"
        for imp, tgt, imp_x, tgt_x in _edges()
        if not _exempt(imp, tgt)
    ]
    assert not offenders, "extras tier violations:\n  " + "\n  ".join(offenders)


def test_cross_tier_allowlist_has_no_stale_entries() -> None:
    """An exemption that no longer matches a real edge must be deleted."""
    live = [(i, t) for i, t, _, _ in _edges()]
    stale = sorted(
        (imp, tgt)
        for imp, tgt in _ALLOWED_CROSS_TIER
        if not any(
            (i == imp or i.startswith(imp + ".")) and (t == tgt or t.startswith(tgt + "."))
            for i, t in live
        )
    )
    assert not stale, f"stale cross-tier exemptions — delete them: {stale}"


def test_every_mapped_extra_exists_in_pyproject() -> None:
    declared = set(_extras())
    unknown = sorted({e for e in _MODULE_EXTRA.values() if e not in declared})
    assert not unknown, f"_MODULE_EXTRA names extras that pyproject does not define: {unknown}"


def test_every_mapped_module_exists() -> None:
    missing = [
        prefix
        for prefix in _MODULE_EXTRA
        if not (_SRC / Path(*prefix.split(".")[1:])).with_suffix(".py").exists()
        and not (_SRC / Path(*prefix.split(".")[1:])).is_dir()
    ]
    assert not missing, f"_MODULE_EXTRA maps modules that no longer exist: {missing}"


@pytest.mark.parametrize(
    "module, expected",
    [
        ("pf_core.log", None),
        ("pf_core.fetch.images", None),
        ("pf_core.llm.prompts", None),
        ("pf_core.llm.tracking.repo", "tracking"),  # longest prefix beats pf_core.llm
        ("pf_core.web.llm_admin.api", "admin"),  # subpackage outranks pf_core.web
        ("pf_core.db.connection", "db"),
    ],
)
def test_required_extra_resolution(module: str, expected: str | None) -> None:
    assert required_extra(module) == expected


# ---------------------------------------------------------------------------
# Lazy-facade routing
#
# A PEP 562 ``_LAZY`` map defers an import that needs an extra. Routing an
# entry through a re-exporting module in a *higher* tier than the one that
# defines it gates a helper behind a dependency it never needed — the base
# install then raises ImportError for a function that would have worked.
# Naming a same-tier public facade over private submodules is fine.
# ---------------------------------------------------------------------------

_LAZY_FACADES = ("pf_core.utils", "pf_core.llm", "pf_core.budget")


@pytest.mark.parametrize("package", _LAZY_FACADES)
def test_lazy_entries_do_not_overgate(package: str) -> None:
    lazy = importlib.import_module(package)._LAZY
    overgated = []
    for name, target in lazy.items():
        module, attr = target if isinstance(target, tuple) else (target, name)
        obj = getattr(importlib.import_module(module), attr)
        defining = getattr(obj, "__module__", None)
        # Data objects (SQLAlchemy Tables, typing constructs) report their
        # library's module, not pf-core's — only code has a meaningful origin.
        if defining is None or not defining.startswith("pf_core."):
            continue
        declared_extra = required_extra(module)
        if declared_extra is not None and required_extra(defining) is None:
            overgated.append(f"{name}: routed via {module} [{declared_extra}], defined in {defining}")
    assert not overgated, (
        f"{package}._LAZY routes names through a module that needs an extra their "
        f"defining module does not: {overgated} — point the entry at the definition, "
        "or import it eagerly"
    )
