"""
Per-agent model, sampling, and backend routing loaded from YAML.

One YAML file per consumer project maps each agent slug to its model plus
optional sampling kwargs — and, in the nested form, to a model *per backend*
plus which backend is active. The file hot-reloads on a TTL
(``MODEL_ROUTER_RELOAD_SECONDS``, default 60), so operators can change an
agent's model or backend without a restart.

Flat form (single backend, the original shorthand)::

    agents:
      grader:
        model: anthropic/claude-haiku-4-5
        temperature: 0.0

Nested form (per-backend models; absorbed from the consumer projects that
independently built it — model ids are not translatable across backends)::

    default_client: openrouter        # top-level declared default (optional)
    env_prefix: MYPROJ                # enables MYPROJ_<SLUG>_BACKEND overrides
    agents:
      drafter:
        default_backend: claude_code  # per-agent default (optional)
        fallback: true                # opt-in availability + call fallback
        temperature: 0.3              # agent-wide; backend entries may override
        backends:
          openrouter:  {model: anthropic/claude-sonnet-4.6}
          claude_code: {model: sonnet, client_kwargs: {retry: 1}}
          anthropic:   {model: claude-sonnet-4-6}

Backend selection precedence: per-call ``backend=`` kwarg > env
``<ENV_PREFIX>_<SLUG>_BACKEND`` > agent ``default_backend`` (or the agent's
single declared backend) > top-level ``default_client`` > hard
:class:`ConfigurationError`. There is deliberately no framework-hardcoded
default backend.

Usage in a service::

    from pf_core.llm.router import resolve_agent

    client, cfg, backend = resolve_agent("drafter")
    content, usage = client.chat(messages=msgs, **cfg)

Or, with call-failure fallback down the declared chain::

    from pf_core.llm.router import call_with_fallback

    content, usage, resolved = call_with_fallback("drafter", msgs)

Environment variables: ``MODEL_ROUTER_CONFIG`` (path, default
``config/model_router.yaml``) and ``MODEL_ROUTER_RELOAD_SECONDS``.
See ``docs/model-router.md`` for the full reference.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, NamedTuple

from pf_core.exceptions import ConfigurationError
from pf_core.llm._router_config import (
    agent_block_or_raise as _agent_block_or_raise,
    selection as _selection,
)
from pf_core.llm._router_config import (
    assert_agents_registered,
    get_agent_block,
    get_agent_config,
    list_agents,
    resolve_backend,
)
from pf_core.llm._router_loader import clear_cache
from pf_core.log import get_logger


logger = get_logger(__name__)

__all__ = [
    "FallbackCall",
    "ResolvedAgent",
    "assert_agents_registered",
    "call_with_fallback",
    "clear_cache",
    "get_agent_block",
    "get_agent_config",
    "list_agents",
    "resolve_agent",
    "resolve_agent_candidates",
    "resolve_backend",
]


class ResolvedAgent(NamedTuple):
    """Result of :func:`resolve_agent` — unpacks as (client, chat_kwargs, backend)."""

    client: Any
    chat_kwargs: dict[str, Any]
    backend: str


class FallbackCall(NamedTuple):
    """Result of :func:`call_with_fallback` — unpacks as (content, usage, resolved)."""

    content: str
    usage: dict[str, Any]
    resolved: ResolvedAgent


def resolve_agent(
    slug: str,
    *,
    backend: str | None = None,
    model_override: str | None = None,
) -> ResolvedAgent:
    """Route ``slug`` to a client and return (client, chat_kwargs, backend).

    The one-stop call for YAML-routed LLM sites::

        client, cfg, backend = resolve_agent("drafter")
        content, usage = client.chat(messages=msgs, **cfg)

    Selection follows :func:`resolve_backend` precedence. When the agent
    opts in with ``fallback: true`` and selection came from the YAML or an
    env override (the env choice goes first in the chain), unavailable
    backends are skipped: a candidate whose client can't be constructed or
    whose ``preflight()`` raises falls through to the next declared
    backend. An explicit per-call ``backend=`` disables the scan. Without
    fallback, acquisition is direct — no preflight call, and construction
    errors propagate unchanged.

    The backend block's ``client_kwargs`` are passed to the client factory
    (see :func:`pf_core.clients.routing.register_client`). Built-in
    backends construct one instance per distinct ``client_kwargs``
    signature (and use the module singleton when there are none), so
    differently-tuned agents don't collide.
    """
    from pf_core.clients.routing import get_client_for_backend

    doc, block = _agent_block_or_raise(slug)
    declared = block.get("backends")

    if backend is not None:
        chosen, fallback_enabled = backend, False
    else:
        chosen = resolve_backend(slug)
        _, source = _selection(doc, slug, block)
        # Env-selected backends participate in fallback (the env choice
        # goes first in the chain); only an explicit per-call backend=
        # disables the scan.
        fallback_enabled = (
            bool(block.get("fallback"))
            and source in ("yaml", "env")
            and declared is not None
        )

    if declared is not None and chosen not in declared:
        raise ConfigurationError(
            f"agent '{slug}' does not declare backend '{chosen}' — "
            f"declared: {', '.join(sorted(declared))}"
        )

    def _client_kwargs(name: str) -> dict[str, Any]:
        if declared is None:
            return {}
        return dict(declared[name].get("client_kwargs") or {})

    if not fallback_enabled:
        client = get_client_for_backend(chosen, **_client_kwargs(chosen))
        cfg = get_agent_config(
            slug,
            backend=chosen if declared is not None else None,
            model_override=model_override,
        )
        return ResolvedAgent(client, cfg, chosen)

    candidates = [chosen] + [b for b in declared if b != chosen]
    failures: list[str] = []
    for candidate in candidates:
        try:
            client = get_client_for_backend(candidate, **_client_kwargs(candidate))
            preflight = getattr(client, "preflight", None)
            if callable(preflight):
                preflight()
        except Exception as exc:
            failures.append(f"{candidate}: {exc}")
            logger.warning(
                "router_backend_unavailable", agent=slug, backend=candidate, error=str(exc)[:200]
            )
            continue
        cfg = get_agent_config(slug, backend=candidate, model_override=model_override)
        return ResolvedAgent(client, cfg, candidate)

    raise ConfigurationError(
        f"agent '{slug}': no available backend — tried " + "; ".join(failures)
    )


def _candidates(
    slug: str,
    *,
    model_override: str | None,
    failures: list[str],
) -> Iterator[ResolvedAgent]:
    """Lazily yield the agent's fallback chain as ResolvedAgents.

    Chain order: the selected backend first (env choice included), then the
    remaining declared backends in YAML order — when the agent opted in
    with ``fallback: true``. Without fallback, the chain is the single
    selected backend and acquisition errors propagate unchanged.

    No ``preflight()`` here — in the call-fallback path the chat call
    itself is the availability probe. Construction failures are recorded
    in ``failures`` and skipped (when fallback is on).
    """
    from pf_core.clients.routing import get_client_for_backend

    doc, block = _agent_block_or_raise(slug)
    declared = block.get("backends")
    chosen = resolve_backend(slug)
    _, source = _selection(doc, slug, block)
    fallback_enabled = (
        bool(block.get("fallback")) and source in ("yaml", "env") and declared is not None
    )
    if declared is not None and chosen not in declared:
        raise ConfigurationError(
            f"agent '{slug}' does not declare backend '{chosen}' — "
            f"declared: {', '.join(sorted(declared))}"
        )

    chain = [chosen]
    if fallback_enabled:
        chain += [b for b in declared if b != chosen]

    for candidate in chain:
        client_kwargs = (
            {} if declared is None else dict(declared[candidate].get("client_kwargs") or {})
        )
        try:
            client = get_client_for_backend(candidate, **client_kwargs)
        except Exception as exc:
            if not fallback_enabled:
                raise
            failures.append(f"{candidate}: {exc}")
            logger.warning(
                "router_backend_unavailable", agent=slug, backend=candidate, error=str(exc)[:200]
            )
            continue
        cfg = get_agent_config(
            slug,
            backend=candidate if declared is not None else None,
            model_override=model_override,
        )
        yield ResolvedAgent(client, cfg, candidate)


def resolve_agent_candidates(
    slug: str,
    *,
    model_override: str | None = None,
) -> Iterator[ResolvedAgent]:
    """Iterate the agent's fallback chain — the primitive for caller-owned
    call-failure fallback.

    Each yielded :class:`ResolvedAgent` carries its own backend's model and
    chat kwargs. Acquisition is lazy (a later backend's client is only
    constructed if iteration reaches it) and construction failures are
    skipped when the agent declares ``fallback: true``. The caller owns the
    try/except around ``chat()`` — useful when each attempt needs its own
    tracking row::

        for client, cfg, backend in resolve_agent_candidates("drafter"):
            try:
                content, usage = client.chat(messages=msgs, **cfg)
                break
            except ClientError:
                continue

    For the common shape, :func:`call_with_fallback` does this loop for you.
    """
    yield from _candidates(slug, model_override=model_override, failures=[])


def call_with_fallback(
    slug: str,
    messages: list[dict],
    *,
    model_override: str | None = None,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
) -> FallbackCall:
    """Call ``chat()`` down the agent's fallback chain until one succeeds.

    Walks :func:`resolve_agent_candidates`: for each candidate, calls
    ``client.chat(messages=messages, **chat_kwargs)``; an exception
    matching ``retry_on`` logs and moves to the next backend (with *its*
    model), anything else propagates immediately. Returns
    :class:`FallbackCall` — ``(content, usage, resolved)``, where
    ``resolved.backend`` is the backend that answered (pass it to tracking
    as the provider label).

    Exhaustion semantics: if every attempt failed, the **last call
    exception is re-raised unchanged** (so callers' existing except clauses
    keep working); if no backend was even constructable, raises
    :class:`ConfigurationError` listing the failures. Clients still own
    same-backend retry — narrow ``retry_on`` to your domain's transport
    errors to avoid burning a second backend on a non-transient bug.
    """
    failures: list[str] = []
    last_exc: BaseException | None = None
    for resolved in _candidates(slug, model_override=model_override, failures=failures):
        try:
            content, usage = resolved.client.chat(
                messages=messages, **resolved.chat_kwargs
            )
        except retry_on as exc:
            last_exc = exc
            failures.append(f"{resolved.backend}: {exc}")
            logger.warning(
                "router_call_failed_trying_next",
                agent=slug,
                backend=resolved.backend,
                error=str(exc)[:200],
            )
            continue
        return FallbackCall(content, usage, resolved)

    if last_exc is not None:
        raise last_exc
    raise ConfigurationError(
        f"agent '{slug}': no available backend — tried " + "; ".join(failures)
    )
