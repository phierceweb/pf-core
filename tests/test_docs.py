"""Docs-truth gates: shipped prose must describe the shipped package."""

from __future__ import annotations

import importlib
import inspect
import os
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "src/pf_core/docs"

_BACKTICKED = re.compile(r"`([^`\n]*)`")
_DOTTED = re.compile(r"pf_core(?:\.[A-Za-z_][A-Za-z0-9_]*)+")

# Dotted strings that are deliberately not import paths.
_NON_IMPORT_PATHS = frozenset(
    {
        "pf_core.X",  # placeholder in a template snippet
        "pf_core.jobs.jobs.id",  # database column, not an attribute
    }
)


def _doc_files() -> list[Path]:
    return sorted(DOCS.rglob("*.md"))


def _resolves(dotted: str) -> bool:
    parts = dotted.split(".")
    for i in range(len(parts), 0, -1):
        try:
            obj = importlib.import_module(".".join(parts[:i]))
        except ModuleNotFoundError as exc:
            if (exc.name or "").split(".")[0] != "pf_core":
                return True  # gated behind an uninstalled extra
            continue
        except ImportError:
            return True  # pf-core's own friendly extra gate
        for attr in parts[i:]:
            if not hasattr(obj, attr):
                return False
            obj = getattr(obj, attr)
        return True
    return False


def test_pf_core_references_in_docs_resolve():
    broken: dict[str, set[str]] = {}
    for path in _doc_files():
        for span in _BACKTICKED.findall(path.read_text()):
            for dotted in _DOTTED.findall(span):
                if dotted in _NON_IMPORT_PATHS or _resolves(dotted):
                    continue
                broken.setdefault(dotted, set()).add(path.name)
    assert not broken, f"docs name pf_core paths that do not exist: {broken}"


# ---------------------------------------------------------------------------
# LLM admin surface
# ---------------------------------------------------------------------------

_ADMIN_PATH = re.compile(r"/admin/llm[A-Za-z0-9_./{}-]*")


def _normalize_path(path: str) -> str:
    segments = [
        "{}" if seg.startswith("{") or seg.isdigit() else seg
        for seg in path.rstrip("/").split("/")
    ]
    return "/".join(segments)


def test_admin_paths_in_docs_are_mounted():
    from pf_core.web.llm_admin import make_admin_router

    router = make_admin_router(prefix="/admin/llm", allow_unauthenticated=True)
    mounted = {_normalize_path(r.path) for r in router.routes}

    missing: dict[str, set[str]] = {}
    for path in _doc_files():
        for raw in _ADMIN_PATH.findall(path.read_text()):
            cleaned = raw.rstrip(".,;:)")
            if _normalize_path(cleaned) not in mounted:
                missing.setdefault(cleaned, set()).add(path.name)
    assert not missing, f"docs link admin routes that are not mounted: {missing}"


def test_llm_run_repo_methods_named_in_docs_exist():
    from pf_core.llm.tracking import LlmRunRepo

    missing: dict[str, set[str]] = {}
    for path in _doc_files():
        for name in re.findall(r"LlmRunRepo\.([a-zA-Z_][a-zA-Z0-9_]*)", path.read_text()):
            if not hasattr(LlmRunRepo, name):
                missing.setdefault(name, set()).add(path.name)
    assert not missing, f"docs name LlmRunRepo methods that do not exist: {missing}"


def test_track_run_kwargs_in_docs_exist():
    from pf_core.llm.tracking import track_run

    accepted = set(inspect.signature(track_run).parameters)
    missing: dict[str, set[str]] = {}
    for path in _doc_files():
        for call in re.findall(r"track_run\(([^)]*)\)", path.read_text()):
            for kwarg in re.findall(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*=", call):
                if kwarg not in accepted:
                    missing.setdefault(kwarg, set()).add(path.name)
    assert not missing, f"docs pass track_run kwargs it does not accept: {missing}"


# ---------------------------------------------------------------------------
# Auto-registered pytest plugin
# ---------------------------------------------------------------------------

_AUTO_PLUGIN = re.compile(r"auto-(?:discovered|registered)")


def test_auto_plugin_fixtures_are_all_documented():
    from pf_core.testing import fixtures

    names = {
        name
        for name, obj in vars(fixtures).items()
        if hasattr(obj, "_pytestfixturefunction") or hasattr(obj, "_fixture_function")
    }
    assert names, "no fixtures found in pf_core.testing.fixtures"

    for doc in (DOCS / "testing.md", DOCS / "INSTALLATION.md"):
        claims = [ln for ln in doc.read_text().splitlines() if _AUTO_PLUGIN.search(ln)]
        assert claims, f"{doc.name} no longer describes the auto-registered plugin"
        for line in claims:
            undocumented = sorted(n for n in names if n not in line)
            assert not undocumented, (
                f"{doc.name} describes the auto-registered plugin without "
                f"naming {undocumented}: {line.strip()!r}"
            )


# ---------------------------------------------------------------------------
# Scope prose vs shipped surface
# ---------------------------------------------------------------------------

_NEGATION = re.compile(r"(?i)\bnever\b|\bmust not\b")


def _prohibition_blocks(text: str) -> list[str]:
    paragraphs = re.split(r"\n\s*\n", text)
    blocks = []
    for i, para in enumerate(paragraphs):
        if not _NEGATION.search(para):
            continue
        following = paragraphs[i + 1] if i + 1 < len(paragraphs) else ""
        if following.lstrip().startswith(("-", "*")):
            para = f"{para}\n{following}"
        blocks.append(para)
    return blocks


def _ships_route_handlers() -> bool:
    return any(
        re.search(r"@router\.(get|post|put|patch|delete)\(", p.read_text())
        for p in (ROOT / "src/pf_core/web").rglob("*.py")
    )


def _ships_console_scripts() -> bool:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    return bool(data["project"].get("scripts"))


def test_scope_prose_does_not_forbid_shipped_surface():
    shipped = {
        "route handler": _ships_route_handlers(),
        "cli command": _ships_console_scripts(),
    }
    assert all(shipped.values()), f"surface probe found nothing to check: {shipped}"

    offences: list[str] = []
    for doc in (ROOT / ".ai/rules/scope.md", ROOT / "CONTRIBUTING.md"):
        for block in _prohibition_blocks(doc.read_text()):
            for surface in shipped:
                if surface in block.lower():
                    offences.append(f"{doc.name} forbids {surface!r}: {block!r}")
    assert not offences, "\n".join(offences)


# ---------------------------------------------------------------------------
# bin/ wrappers
# ---------------------------------------------------------------------------


def test_bin_test_wrapper_matches_scaffolded_projects():
    wrapper = ROOT / "bin/test"
    assert wrapper.is_file(), "pf-core scaffolds bin/test but does not ship one"
    assert os.access(wrapper, os.X_OK), "bin/test is not executable"

    body = wrapper.read_text()
    assert ".venv/bin/python" in body and "pytest" in body
    for template in ROOT.glob("templates/*/bin/test"):
        assert body == template.read_text(), f"bin/test diverges from {template}"


def test_contributing_documents_the_test_wrapper():
    assert "bin/test" in (ROOT / "CONTRIBUTING.md").read_text()
