"""
LLM run tracking decorator.

``@track_run`` wraps any function whose return shape matches
``OpenRouterClient.chat()`` — a ``(content, usage)`` tuple — or an equivalent
dict of ``{"content": ..., "usage": {...}}``. It times the call, captures
fingerprint/usage/error, records the run via ``LlmRunRepo``, and stamps the
new ``run_id`` onto the returned usage dict so the caller can attach
configs/metrics/tags after parsing.

Contract with the wrapped function:

1. MUST be called with ``model=...`` as a keyword argument — the decorator
   uses this to resolve ``llm_models.id``.
2. SHOULD accept ``messages=[{role, content}, ...]`` as a keyword argument if
   you want rendered prompts stored in ``llm_run_payloads``. Optional.
3. SHOULD accept sampling kwargs (``temperature``, ``top_p``, ``max_tokens``,
   ``seed``, ``stop_sequences``). Captured opportunistically for replay fidelity.

Usage::

    from pf_core.llm.tracking import track_run
    from pf_core.llm.router import get_agent_config
    from pf_core.clients import openrouter

    @track_run(agent_type="drafter")
    def tracked_chat(*, model, messages, **sampling):
        return openrouter.get_client().chat(model=model, messages=messages, **sampling)

    cfg = get_agent_config("drafter")
    content, usage = tracked_chat(messages=msgs, **cfg)
    run_id = usage["_llm_run_id"]
    LlmRunRepo()._... # attach configs/metrics/tags using run_id

On exception: records ``status='failed'`` with ``error`` / ``error_class``
populated, then re-raises the original exception.
"""

from __future__ import annotations

import functools
import time
import warnings
from typing import Any, Callable

from pf_core.llm.tracking.repo import LlmRunRepo


_SAMPLING_KWARGS = ("temperature", "top_p", "max_tokens", "seed", "stop_sequences")

_MAX_ERROR_LEN = 10_000

# Sentinel distinguishing "caller omitted provider=" from an explicit None
# (None already means "skip the label"). The implicit "openrouter" default
# is deprecated; the backend should be passed explicitly — e.g. the
# ``backend`` field of pf_core.llm.router.resolve_agent's result.
_PROVIDER_UNSET: Any = object()


def track_run(
    *,
    agent_type: str,
    provider: str | None = _PROVIDER_UNSET,
    repo: LlmRunRepo | None = None,
) -> Callable:
    """Return a decorator that records one ``llm_runs`` row per wrapped call.

    Args:
        agent_type: slug for ``llm_agent_types`` (e.g. ``"drafter"``).
        provider: label written to ``llm_runs.provider``. Pass the backend
            name (e.g. ``ResolvedAgent.backend``) or ``None`` to skip.
            Omitting it currently falls back to ``"openrouter"`` with a
            ``DeprecationWarning``; the implicit default is removed in v1.0.
        repo: optional ``LlmRunRepo`` instance. Defaults to a fresh one per
            call. Inject a custom instance to share a transaction or route
            writes during testing.
    """
    if provider is _PROVIDER_UNSET:
        warnings.warn(
            "track_run() without provider= currently defaults the llm_runs "
            "label to 'openrouter'; this implicit default is deprecated and "
            "will be removed in v1.0 — pass the backend explicitly (e.g. "
            "resolve_agent(...).backend) or provider=None to skip",
            DeprecationWarning,
            stacklevel=2,
        )
        provider = "openrouter"

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            model = kwargs.get("model")
            if model is None:
                raise TypeError(
                    f"@track_run-wrapped function {func.__name__!r} must be "
                    "called with model= as a keyword argument"
                )
            sampling = {k: kwargs[k] for k in _SAMPLING_KWARGS if k in kwargs}
            messages = kwargs.get("messages") or []
            rendered_system, rendered_user = _extract_rendered_prompts(messages)

            _repo = repo if repo is not None else LlmRunRepo()

            t0 = time.monotonic()
            try:
                result = func(*args, **kwargs)
            except Exception as exc:
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                ctx = getattr(exc, "context", None) or {}
                http_status = ctx.get("status_code") if isinstance(ctx, dict) else None
                _repo.record(
                    agent_type=agent_type,
                    model=model,
                    sampling=sampling or None,
                    provider=provider,
                    usage={"duration_ms": elapsed_ms},
                    status="failed",
                    error=str(exc)[:_MAX_ERROR_LEN],
                    error_class=type(exc).__name__,
                    http_status=http_status if isinstance(http_status, int) else None,
                    rendered_prompts=(rendered_system, rendered_user),
                )
                raise

            content, usage = _unpack_result(result)
            usage.setdefault("duration_ms", int((time.monotonic() - t0) * 1000))
            fingerprint = usage.get("system_fingerprint")
            record_usage = {
                k: v
                for k, v in usage.items()
                if k not in ("system_fingerprint", "_llm_run_id")
            }

            run_id = _repo.record(
                agent_type=agent_type,
                model=model,
                sampling=sampling or None,
                provider=provider,
                model_fingerprint=fingerprint,
                usage=record_usage,
                rendered_prompts=(rendered_system, rendered_user),
                raw_response=content if isinstance(content, str) else None,
            )
            usage["_llm_run_id"] = run_id
            return result

        return wrapper

    return decorator


def _extract_rendered_prompts(
    messages: list[dict],
) -> tuple[str | None, str | None]:
    """Flatten system and user messages into two strings for payload storage.

    Multi-part messages (list-of-parts content) are skipped — only plain
    string content is captured. Assistant and tool messages are ignored.
    """
    system_parts: list[str] = []
    user_parts: list[str] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if not isinstance(content, str):
            continue
        if role == "system":
            system_parts.append(content)
        elif role == "user":
            user_parts.append(content)
    return (
        "\n".join(system_parts) if system_parts else None,
        "\n".join(user_parts) if user_parts else None,
    )


def _unpack_result(result: Any) -> tuple[str | None, dict]:
    """Extract ``(content, usage)`` from a wrapped function's return.

    Accepts:
      - ``(content, usage)`` tuple — ``OpenRouterClient.chat()`` shape.
      - ``{"content": ..., "usage": {...}}`` dict.
    """
    if (
        isinstance(result, tuple)
        and len(result) == 2
        and isinstance(result[1], dict)
    ):
        return result[0], result[1]
    if isinstance(result, dict):
        usage = result.get("usage")
        if isinstance(usage, dict):
            return result.get("content"), usage
    raise TypeError(
        "@track_run expected (content, usage) tuple or "
        "{'content': ..., 'usage': {...}} dict return, got "
        f"{type(result).__name__}"
    )
