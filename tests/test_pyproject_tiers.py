"""Guard the dependency-tier invariants of the foundation split.

These assert the *packaging* contract the foundation split delivers: the base
install is dependency-light, and the LLM/HTTP/CLI stacks live behind extras
that compose correctly. If someone re-adds httpx (etc.) to the base
``dependencies``, or breaks the ``[tracking] -> [llm]`` chain that consumer
projects depend on, these fail.

The actual "bare install really is lean" proof is the clean-venv smoke test in
CI (see docs/INSTALLATION.md) — this only checks the declared metadata.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _meta() -> dict:
    return tomllib.loads(_PYPROJECT.read_text())


def _dist_name(requirement: str) -> str:
    """Bare distribution name from a requirement string.

    'json-repair>=0.40' -> 'json-repair'; 'pf-core[db,llm]' -> 'pf-core';
    'uvicorn[standard]>=0.30' -> 'uvicorn'.
    """
    name = requirement.strip()
    for sep in ("[", ">", "<", "=", "!", "~", " ", "@"):
        name = name.split(sep, 1)[0]
    return name.strip().lower()


# Deps that must NOT be in the base install — the whole point of the split.
_BANNED_FROM_BASE = {"httpx", "pydantic", "typer", "click", "json-repair", "tenacity"}
# Deps that the foundation genuinely needs.
_REQUIRED_IN_BASE = {"python-dotenv", "pyyaml", "structlog", "nanoid", "rich"}


def test_base_dependencies_are_foundation_only() -> None:
    base = {_dist_name(d) for d in _meta()["project"]["dependencies"]}
    assert _BANNED_FROM_BASE.isdisjoint(base), (
        f"LLM/HTTP/CLI deps leaked into base dependencies: "
        f"{sorted(_BANNED_FROM_BASE & base)}"
    )
    assert _REQUIRED_IN_BASE <= base, (
        f"foundation deps missing from base: {sorted(_REQUIRED_IN_BASE - base)}"
    )


def _extra(name: str) -> list[str]:
    return _meta()["project"]["optional-dependencies"][name]


def test_http_extra_carries_httpx() -> None:
    assert "httpx" in {_dist_name(d) for d in _extra("http")}


def test_cli_extra_carries_typer() -> None:
    assert "typer" in {_dist_name(d) for d in _extra("cli")}


def test_validate_extra_is_guards_without_the_client_stack() -> None:
    # The anti-slop output guards (parse + json-repair, pydantic validation)
    # must be installable WITHOUT httpx/clients/tenacity. This is the whole
    # point of the [validate] extra.
    names = {_dist_name(d) for d in _extra("validate")}
    assert {"json-repair", "pydantic"} <= names
    assert "httpx" not in names
    assert "tenacity" not in names


def test_llm_extra_composes_validate_http_and_retry() -> None:
    llm = _extra("llm")
    refs = {d.replace(" ", "") for d in llm}
    names = {_dist_name(d) for d in llm}
    # [llm] ⊇ [validate] (guards) + [http] (clients need httpx); tenacity direct.
    # Same dependency closure as before, so [llm]/[full]/[tracking] consumers
    # are unchanged — mirrors the [tracking] = [db,llm] superset relationship.
    assert "pf-core[validate,http]" in refs
    assert "tenacity" in names


@pytest.mark.parametrize(
    "extra, must_reference",
    [
        # A consumer that declares only [image-phash,tracking] stays green
        # ONLY because [tracking] pulls [llm]. This is the load-bearing
        # invariant.
        ("tracking", "pf-core[db,llm]"),
        # [full] must pull both new extras so [full] consumers are unaffected.
        ("jobs", "pf-core[db,cli]"),
    ],
)
def test_extra_references_required_subextra(extra: str, must_reference: str) -> None:
    refs = {d.replace(" ", "") for d in _extra(extra)}
    assert must_reference in refs, f"[{extra}] must reference {must_reference}; got {refs}"


def test_full_includes_llm_and_cli() -> None:
    refs = {d.replace(" ", "") for d in _extra("full")}
    assert "pf-core[llm]" in refs
    assert "pf-core[cli]" in refs
