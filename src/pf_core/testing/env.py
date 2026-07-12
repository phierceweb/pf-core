"""Import-time environment helpers for consumer conftests.

Consumer test suites pin the same block of env vars before importing their
app (config objects read the environment at import time, so pytest fixtures
run too late). These helpers absorb that block. Call them at the TOP of
``conftest.py``, before any app import::

    from pf_core.testing.env import hermetic_test_env, stub_model_router

    hermetic_test_env(extra={"MYAPP_MODE": "1"})
    stub_model_router(["summarizer", "classifier"])

Stdlib + pyyaml only — importable in any install tier.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_API_KEY_VARS = (
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "BRAVE_API_KEY",
)


def hermetic_test_env(
    *,
    database_url: str = "sqlite://",
    disable_budget: bool = True,
    disable_cache: bool = True,
    clear_api_keys: bool = True,
    extra: dict[str, str] | None = None,
) -> None:
    """Force the no-external-services env block for a test process.

    Sets ``DATABASE_URL`` (``pf_engine`` re-pins it per test) and blanks
    ``REDIS_URL`` unconditionally — a developer's exported real URLs must
    never leak into tests. Disables the LLM cache (``CACHE_CONFIG=off``,
    reload ``0``) and the budget guard (``BUDGET_ENFORCEMENT_DISABLED=1``),
    and deletes provider API keys so a misconfigured test can't reach a real
    provider. Keyword flags opt pieces out; ``extra`` sets project-specific
    vars verbatim (e.g. ``{"MYAPP_MODE": "1"}``).
    """
    os.environ["DATABASE_URL"] = database_url
    os.environ["REDIS_URL"] = ""
    if disable_cache:
        os.environ["CACHE_CONFIG"] = "off"
        os.environ["CACHE_CONFIG_RELOAD_SECONDS"] = "0"
    if disable_budget:
        os.environ["BUDGET_ENFORCEMENT_DISABLED"] = "1"
    if clear_api_keys:
        for key in _API_KEY_VARS:
            os.environ.pop(key, None)
    for key, value in (extra or {}).items():
        os.environ[key] = value


def stub_model_router(
    agents: list[str] | tuple[str, ...],
    *,
    model: str = "test-model",
    dir: str | Path | None = None,
) -> Path:
    """Write a stub router YAML mapping every *agents* slug to *model*.

    Points ``MODEL_ROUTER_CONFIG`` at the written file with reload TTL ``0``
    so tests never depend on the project's real ``model_router.yaml``.
    Pass the slugs your suite resolves (the ``assert_agents_registered``
    list); returns the written path (a fresh temp dir unless ``dir`` is
    given).
    """
    import yaml

    target_dir = (
        Path(dir) if dir is not None else Path(tempfile.mkdtemp(prefix="pf_router_"))
    )
    path = target_dir / "model_router_stub.yaml"
    doc = {"agents": {slug: {"model": model} for slug in agents}}
    path.write_text(yaml.safe_dump(doc, sort_keys=True), encoding="utf-8")
    os.environ["MODEL_ROUTER_CONFIG"] = str(path)
    os.environ["MODEL_ROUTER_RELOAD_SECONDS"] = "0"
    return path
