"""Schema validation for ``model_router.yaml`` — flat and nested agent blocks.

Internal helper for :mod:`pf_core.llm.router`. Validation is strict and
fail-fast: a malformed file raises :class:`ConfigurationError` naming the
offending agent/backend, so a typo surfaces at load time rather than as a
confusing SDK error at the first LLM call.

Two agent-block forms are valid:

flat (single-backend shorthand)::

    agents:
      summarizer:
        model: anthropic/claude-haiku-4-5
        temperature: 0.0

nested (per-backend models — model ids are NOT translatable across
backends, so each backend declares its own)::

    agents:
      drafter:
        default_backend: openrouter      # optional
        fallback: true                   # optional, opt-in availability fallback
        temperature: 0.3                 # agent-wide; backend entries may override
        backends:
          openrouter:  {model: anthropic/claude-sonnet-4.6}
          claude_code: {model: sonnet, client_kwargs: {retry: 1}}
"""

from __future__ import annotations

from typing import Any

from pf_core.exceptions import ConfigurationError

# Agent-block keys that configure routing, not chat(). Stripped before the
# resolved config is splatted into client.chat().
STRUCTURAL_KEYS = frozenset({"backends", "default_backend", "fallback"})

# Backend-block keys consumed at client acquisition, never passed to chat().
CLIENT_ONLY_KEYS = frozenset({"client_kwargs"})


def validate_router_doc(raw: Any, path: Any) -> dict[str, Any]:
    """Validate a parsed model_router.yaml document.

    Returns a normalized dict with keys ``agents`` (validated blocks),
    ``default_client`` (str | None), ``env_prefix`` (str | None), and
    ``non_chat_keys`` (frozenset of consumer-extension keys to strip from
    chat kwargs).

    Raises:
        ConfigurationError: on any structural problem, naming the agent
            and backend involved.
    """
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{path} must be a mapping at the top level")

    agents = raw.get("agents")
    if agents is None:
        raise ConfigurationError(f"{path} is missing required 'agents' section")
    if not isinstance(agents, dict):
        raise ConfigurationError(f"{path}: 'agents' must be a mapping")

    default_client = _optional_str(raw, "default_client", path)
    env_prefix = _optional_str(raw, "env_prefix", path)

    non_chat_raw = raw.get("non_chat_keys") or []
    if not isinstance(non_chat_raw, list) or not all(isinstance(k, str) for k in non_chat_raw):
        raise ConfigurationError(f"{path}: 'non_chat_keys' must be a list of strings")

    validated: dict[str, dict[str, Any]] = {}
    for slug, cfg in agents.items():
        validated[str(slug)] = _validate_agent(str(slug), cfg)

    return {
        "agents": validated,
        "default_client": default_client,
        "env_prefix": env_prefix,
        "non_chat_keys": frozenset(non_chat_raw),
    }


def _optional_str(raw: dict, key: str, path: Any) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ConfigurationError(f"{path}: '{key}' must be a non-empty string")
    return value


def _validate_agent(slug: str, cfg: Any) -> dict[str, Any]:
    if not isinstance(cfg, dict):
        raise ConfigurationError(f"agent '{slug}' entry must be a mapping")

    backends = cfg.get("backends")
    if backends is None:
        if "model" not in cfg:
            raise ConfigurationError(
                f"agent '{slug}' must declare a 'model' (flat form) or a "
                "'backends' mapping (nested form)"
            )
        _require_model(cfg["model"], f"agent '{slug}'")
        return dict(cfg)

    if "model" in cfg:
        raise ConfigurationError(
            f"agent '{slug}' declares both a top-level 'model' and 'backends' — "
            "in the nested form each backend declares its own model"
        )
    if not isinstance(backends, dict) or not backends:
        raise ConfigurationError(f"agent '{slug}': 'backends' must be a non-empty mapping")

    for name, block in backends.items():
        where = f"agent '{slug}' backend '{name}'"
        if not isinstance(block, dict):
            raise ConfigurationError(f"{where} entry must be a mapping")
        if "model" not in block:
            raise ConfigurationError(f"{where} missing required key 'model'")
        _require_model(block["model"], where)
        client_kwargs = block.get("client_kwargs")
        if client_kwargs is not None and not isinstance(client_kwargs, dict):
            raise ConfigurationError(f"{where}: 'client_kwargs' must be a mapping")

    default_backend = cfg.get("default_backend")
    if default_backend is not None and default_backend not in backends:
        raise ConfigurationError(
            f"agent '{slug}': default_backend '{default_backend}' is not one of "
            f"its declared backends ({', '.join(sorted(backends))})"
        )

    fallback = cfg.get("fallback")
    if fallback is not None and not isinstance(fallback, bool):
        raise ConfigurationError(f"agent '{slug}': 'fallback' must be a boolean")

    return dict(cfg)


def _require_model(value: Any, where: str) -> None:
    if not isinstance(value, str) or not value:
        raise ConfigurationError(f"{where} 'model' must be a non-empty string")
