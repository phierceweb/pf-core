"""Client registry + backend acquisition for LLM routing.

The registry maps backend names (the keys used under ``backends:`` in
``model_router.yaml``) to client factories. Three factories are built in —
``openrouter``, ``claude_code``, ``anthropic`` — and consumers register
their own (an Ollama client, a direct-OpenAI client, …) with
:func:`register_client`. Any object satisfying the
:class:`pf_core.clients.ChatClient` protocol works; no inheritance needed.

Usage::

    # Consumer with a custom backend, at startup:
    from pf_core.clients.routing import register_client
    from app.clients.ollama import OllamaClient

    register_client("ollama", lambda **kw: OllamaClient(**kw))

    # config/model_router.yaml may now route agents to it:
    #   agents:
    #     rag_answerer:
    #       default_backend: ollama
    #       backends:
    #         ollama: {model: "qwen2.5:14b"}

Acquisition normally happens through
:func:`pf_core.llm.router.resolve_agent`; :func:`get_client_for_backend` is
the direct path for callers that don't use the YAML router.

Built-in factories delegate to each client module's ``get_client()`` —
process-wide singletons where the first call's kwargs win. Custom factories
choose their own lifecycle (construct fresh per call, cache, pool).
"""

from __future__ import annotations

import warnings
from typing import Any, Callable

from pf_core.clients import ChatClient
from pf_core.exceptions import ConfigurationError

BUILTIN_BACKENDS = ("anthropic", "claude_code", "openrouter")

_registry: dict[str, Callable[..., ChatClient]] = {}

# Built-in instances constructed with non-empty client_kwargs, keyed by
# (backend, kwargs signature). Keeps "different agents, different tuning"
# from colliding on the module singletons (whose first call's kwargs win),
# while repeated resolution of the same agent reuses one instance instead
# of constructing per call.
_instances: dict[tuple[str, str], ChatClient] = {}


def _builtin_module(name: str):
    # Lazy imports: each backend pulls its own dependency tier only when
    # actually routed to (claude_code needs the CLI, the others httpx/SDK).
    if name == "openrouter":
        from pf_core.clients import openrouter

        return openrouter
    if name == "claude_code":
        from pf_core.clients import claude_code

        return claude_code
    if name == "anthropic":
        from pf_core.clients import anthropic

        return anthropic
    return None


def _kwargs_signature(client_kwargs: dict[str, Any]) -> str:
    # repr-based: kwargs values come from YAML (str/int/float/bool/list),
    # which are not all hashable but repr deterministically.
    return repr(sorted(client_kwargs.items()))


def register_client(name: str, factory: Callable[..., ChatClient]) -> None:
    """Register (or override) a client factory under ``name``.

    The factory is called with the backend block's ``client_kwargs`` (if
    any) and must return a :class:`~pf_core.clients.ChatClient`-shaped
    object. Registering a built-in name overrides it for this process —
    useful for tests and for consumers that need a customized transport.
    """
    if not name or not isinstance(name, str):
        raise ConfigurationError("client backend name must be a non-empty string")
    if not callable(factory):
        raise ConfigurationError(f"factory for backend '{name}' must be callable")
    _registry[name] = factory


def unregister_client(name: str) -> None:
    """Remove a factory registered via :func:`register_client`.

    Raises:
        ConfigurationError: if ``name`` was never registered (built-ins
            that were not overridden cannot be unregistered).
    """
    if name not in _registry:
        raise ConfigurationError(f"no registered client factory named '{name}'")
    del _registry[name]


def registered_backends() -> list[str]:
    """All routable backend names — built-ins plus registered customs, sorted."""
    return sorted(set(BUILTIN_BACKENDS) | set(_registry))


def get_client_for_backend(name: str, **client_kwargs: Any) -> ChatClient:
    """Return a client for ``name``, passing ``client_kwargs`` to its factory.

    Registered factories win over built-in names and own their lifecycle —
    they are called on every acquisition. Built-ins are lifecycle-managed
    here: no kwargs returns the module singleton (``get_client()``); with
    kwargs, a fresh instance is constructed via the module's
    ``new_client()`` and cached per (backend, kwargs) signature — so two
    agents tuning the same backend differently get distinct clients, and
    re-resolving the same agent reuses one. Unknown names raise a
    :class:`ConfigurationError` listing what is routable.
    """
    factory = _registry.get(name)
    if factory is not None:
        return factory(**client_kwargs)

    module = _builtin_module(name)
    if module is None:
        raise ConfigurationError(
            f"unknown LLM backend '{name}' — routable backends: "
            f"{', '.join(registered_backends())}. Register custom backends with "
            "pf_core.clients.routing.register_client(name, factory)."
        )
    if not client_kwargs:
        return module.get_client()
    key = (name, _kwargs_signature(client_kwargs))
    if key not in _instances:
        _instances[key] = module.new_client(**client_kwargs)
    return _instances[key]


def clear_client_cache() -> None:
    """Drop the per-signature built-in instances. For tests.

    Does not touch the module singletons — use each client module's
    ``reset_client()`` for those.
    """
    _instances.clear()


def get_routed_client(use_claude_code: bool) -> ChatClient:
    """Deprecated boolean dispatch — OpenRouter unless ``use_claude_code``.

    **Deprecated** — a thin compatibility shim, to be removed before 1.0.
    The boolean shape bakes in OpenRouter as the silent default and has
    no slot for other backends. Declare backends in ``model_router.yaml``
    and use :func:`pf_core.llm.router.resolve_agent`, or call
    :func:`get_client_for_backend` directly.
    """
    warnings.warn(
        "get_routed_client(bool) is deprecated and will be removed in v1.0 — "
        "declare backends in model_router.yaml and use "
        "pf_core.llm.router.resolve_agent (or get_client_for_backend for "
        "direct acquisition)",
        DeprecationWarning,
        stacklevel=2,
    )
    return get_client_for_backend("claude_code" if use_claude_code else "openrouter")
