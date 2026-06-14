"""Config-side resolution for the model router — no client acquisition.

Internal helper for :mod:`pf_core.llm.router` (import everything from
there): agent-block lookup, backend selection precedence, and
chat-kwargs assembly. The client-acquisition family (``resolve_agent``,
``call_with_fallback``) lives in the router module and builds on these.
"""

from __future__ import annotations

import os
from typing import Any

from pf_core.exceptions import ConfigurationError
from pf_core.llm._router_loader import config_path, load
from pf_core.llm._router_schema import CLIENT_ONLY_KEYS, STRUCTURAL_KEYS


def agent_block_or_raise(slug: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (doc, agent block) or raise naming the slug and file path."""
    if not slug:
        raise ConfigurationError("agent slug is required")
    doc = load()
    agents = doc["agents"]
    if slug not in agents:
        raise ConfigurationError(
            f"agent '{slug}' not defined in {config_path()}"
        )
    return doc, agents[slug]


def get_agent_block(slug: str) -> dict[str, Any]:
    """Return the raw validated YAML block for ``slug`` (a copy).

    For consumers that declared extension keys via top-level
    ``non_chat_keys`` (e.g. an input-token gate) and need to read them —
    :func:`get_agent_config` strips those from the chat kwargs.
    """
    _, block = agent_block_or_raise(slug)
    return dict(block)


def selection(doc: dict[str, Any], slug: str, block: dict[str, Any]) -> tuple[str | None, str]:
    """YAML/env backend selection for an agent.

    Returns ``(backend, source)`` where source is ``"env"`` / ``"yaml"`` /
    ``"none"``. An env value naming a backend the agent doesn't declare
    falls through (ops resilience), as does a ``default_client`` the agent
    doesn't declare.
    """
    declared = block.get("backends")
    env_prefix = doc["env_prefix"]
    if env_prefix:
        env_val = (os.environ.get(f"{env_prefix}_{slug.upper()}_BACKEND") or "").strip()
        if env_val and (declared is None or env_val in declared):
            return env_val, "env"
    if declared is not None:
        default_backend = block.get("default_backend")
        if default_backend:
            return default_backend, "yaml"
        if len(declared) == 1:
            return next(iter(declared)), "yaml"
    default_client = doc["default_client"]
    if default_client and (declared is None or default_client in declared):
        return default_client, "yaml"
    return None, "none"


def resolve_backend(slug: str, *, backend: str | None = None) -> str:
    """Active backend name for ``slug``.

    Precedence: ``backend=`` kwarg > env ``<ENV_PREFIX>_<SLUG>_BACKEND`` >
    agent ``default_backend`` (or its single declared backend) > top-level
    ``default_client``. No resolution is a hard error — the framework ships
    no default backend.
    """
    doc, block = agent_block_or_raise(slug)
    if backend:
        return backend
    chosen, _source = selection(doc, slug, block)
    if chosen is None:
        raise ConfigurationError(
            f"no backend resolvable for agent '{slug}' in {config_path()} — "
            "set 'default_backend' on the agent, a top-level 'default_client', "
            "pass backend=, or set the env override (requires 'env_prefix')"
        )
    return chosen


def get_agent_config(
    slug: str,
    *,
    backend: str | None = None,
    model_override: str | None = None,
) -> dict[str, Any]:
    """Return chat-unpackable kwargs for ``slug``.

    Flat agents return their block as-is (minus declared ``non_chat_keys``).
    Nested agents return agent-wide sampling overlaid with the active
    backend's block — the backend's ``model`` wins, structural and
    client-acquisition keys are stripped. ``model_override`` beats
    everything.

    Raises:
        ConfigurationError: if the YAML is missing, malformed, doesn't
            contain ``slug``, or (nested) no backend is resolvable / the
            requested backend isn't declared.
    """
    doc, block = agent_block_or_raise(slug)
    non_chat = doc["non_chat_keys"]
    declared = block.get("backends")

    if declared is None:
        cfg = {k: v for k, v in block.items() if k not in non_chat}
    else:
        active = backend or resolve_backend(slug)
        if active not in declared:
            raise ConfigurationError(
                f"agent '{slug}' does not declare backend '{active}' — "
                f"declared: {', '.join(sorted(declared))}"
            )
        cfg = {
            k: v
            for k, v in block.items()
            if k not in STRUCTURAL_KEYS and k not in non_chat
        }
        cfg.update(
            {
                k: v
                for k, v in declared[active].items()
                if k not in CLIENT_ONLY_KEYS and k not in non_chat
            }
        )

    if model_override:
        cfg["model"] = model_override
    return cfg


def list_agents() -> list[str]:
    """Return all agent slugs defined in the YAML, sorted."""
    return sorted(load()["agents"].keys())


def assert_agents_registered(expected: list[str]) -> None:
    """Fail fast at startup if any expected slug is missing from the YAML.

    Call once during app boot with every agent slug the codebase uses.
    Catches typos and un-staged YAML edits before the first LLM call.
    (Deep per-agent validation — backend models present, default_backend
    declared — already runs on every load.)

    Raises:
        ConfigurationError: if any slug in ``expected`` is absent.
    """
    agents = load()["agents"]
    missing = [s for s in expected if s not in agents]
    if missing:
        raise ConfigurationError(
            f"agents missing from {config_path()}: {', '.join(missing)}"
        )
