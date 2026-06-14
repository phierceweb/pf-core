"""Anthropic API client wrapper.

Wraps the official ``anthropic`` Python SDK's ``messages.create()``
endpoint with pf-core's ``(content, usage)`` return convention. Implements
the same ``.chat(messages, model, ...) -> (content, usage)`` interface as
:class:`pf_core.clients.openrouter.OpenRouterClient` so a caller can swap
clients transparently.

Requires the ``anthropic`` extra::

    pip install 'pf-core[anthropic]'

Multimodal: pass image content blocks in the messages list per Anthropic's
documented schema (``{"role": "user", "content": [{"type": "image",
"source": {...}}, {"type": "text", "text": "..."}]}``). The wrapper
forwards ``messages`` to the SDK as-is — no validation or transformation.

``response_format`` is accepted for API parity but ignored — it's an
OpenAI/OpenRouter vendor feature with no Anthropic equivalent. Use
Anthropic's documented JSON-output techniques (e.g. tool-use,
"```json" framing in the prompt) instead.

Usage::

    from pf_core.clients.anthropic import get_client

    client = get_client()
    content, usage = client.chat(
        messages=[{"role": "user", "content": "Hello"}],
        model="claude-haiku-4-5-20251001",
    )
"""

from __future__ import annotations

import os
import time
from typing import Any

from pf_core.exceptions import ClientError
from pf_core.log import get_logger
from pf_core.pricing import estimate_cost

_log = get_logger(__name__)


DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_MAX_TOKENS = 4096

# Wall-clock cap for ``preflight()`` — should be much shorter than the
# per-request default. The whole point is to fail fast on a missing
# API key or unreachable host.
DEFAULT_PREFLIGHT_TIMEOUT_SECONDS = 30


class AnthropicError(ClientError):
    """Anthropic API call failed (SDK error, network failure, or bad config)."""


class AnthropicClient:
    """Client for Anthropic's ``messages.create()`` endpoint via the official SDK.

    Multimodal-capable — callers pass Anthropic-format messages (with
    ``{"type": "image", "source": ...}`` content blocks for vision).

    Args:
        api_key: Anthropic API key. Required.
        model: Default model passed on every call. Per-call ``chat(model=...)``
            overrides this. If neither is set, ``chat()`` raises
            :class:`AnthropicError`.
        request_timeout: Per-request socket timeout in seconds.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str | None = None,
        request_timeout: int = DEFAULT_TIMEOUT_SECONDS,
        retry: int = 0,
    ) -> None:
        if not api_key:
            raise AnthropicError(
                "AnthropicClient requires a non-empty api_key. "
                "Set ANTHROPIC_API_KEY in your environment or pass api_key=."
            )
        try:
            from anthropic import Anthropic
        except ImportError as e:
            raise ImportError(
                "AnthropicClient requires the anthropic SDK. Install with: "
                "pip install 'pf-core[anthropic]'"
            ) from e

        self.api_key = api_key
        self.model = model
        self.request_timeout = request_timeout
        self.retry = retry
        self._client = Anthropic(api_key=api_key, timeout=request_timeout)

    def chat(
        self,
        messages: list[dict],
        model: str = "",
        temperature: float | None = 0.2,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        top_p: float | None = 1.0,
        response_format: dict | None = None,  # noqa: ARG002 — accepted for API parity, ignored
        timeout: int | None = None,
        **kwargs: Any,
    ) -> tuple[str, dict]:
        """Send a chat completion request via Anthropic's messages API.

        ``model`` is per-call; falls back to the instance default if empty.
        ``timeout`` (per-call) overrides the constructor-time timeout for
        this call only via the SDK's ``with_options(timeout=...)``
        derived-client pattern.
        ``response_format`` is accepted for parity with
        :class:`OpenRouterClient` but ignored — Anthropic has no
        equivalent. Use Anthropic's documented JSON-output techniques
        (tool-use, ``"```json"`` framing in the prompt) instead.

        On failure, retries up to ``self.retry`` times before raising
        :class:`AnthropicError`. The SDK has its own internal retry on
        transient HTTP failures; pf-core retry is layered on top and
        kicks in once the SDK has exhausted its own.

        Returns:
            ``(content, usage)`` — content is the concatenation of all
            text blocks in the response; usage carries the same key set
            as :meth:`OpenRouterClient.chat` (token counts mapped from
            Anthropic's ``input_tokens`` / ``output_tokens`` /
            ``cache_read_input_tokens`` / ``cache_creation_input_tokens``;
            ``reasoning_tokens`` from ``thinking_tokens`` and ``cost_usd`` a
            per-call estimate via :func:`_estimate_cost_usd`).
        """
        resolved_model = model or self.model
        if not resolved_model:
            raise AnthropicError(
                "No model specified. Pass model= to .chat() or to the constructor."
            )

        # Per-call timeout via SDK's with_options derived-client pattern.
        sdk = self._client if timeout is None else self._client.with_options(timeout=timeout)

        # Build the kwargs sent to messages.create. Conditionally include
        # `temperature` and `top_p` so reasoning models (Opus 4.7+, future
        # thinking models) that reject these params don't get them sent.
        # Callers opt out by passing `temperature=None` / `top_p=None`.
        # Per-model knobs belong in the caller's config (e.g. the consumer's
        # model_router.yaml) — they should never be hardcoded here.
        call_kwargs: dict[str, Any] = {
            "model": resolved_model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if temperature is not None:
            call_kwargs["temperature"] = temperature
        if top_p is not None:
            call_kwargs["top_p"] = top_p
        call_kwargs.update(kwargs)

        # Up to (self.retry + 1) attempts. Default retry=0 means one shot.
        # Wraps any SDK error in AnthropicError after the last retry.
        # Validation errors (no model) raised above and aren't reached here.
        response = None
        elapsed_ms = 0
        for attempt in range(self.retry + 1):
            t0 = time.monotonic()
            try:
                response = sdk.messages.create(**call_kwargs)
            except Exception as e:
                if attempt < self.retry:
                    _log.warning(
                        "anthropic_retry",
                        attempt=attempt + 1,
                        of=self.retry + 1,
                        model=resolved_model,
                        error_head=str(e)[:200],
                    )
                    continue
                raise AnthropicError(
                    f"Anthropic API call failed (after {attempt + 1} attempt(s)): {e}",
                    context={"model": resolved_model, "attempts": attempt + 1},
                    cause=e,
                ) from e
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            break  # success — exit retry loop

        # Unreachable: every loop path either continues or breaks/raises.
        assert response is not None  # noqa: S101

        # Anthropic returns a list of content blocks. We concatenate text
        # blocks (matching how openrouter.py returns the first choice's
        # content as a string). Non-text blocks (tool_use, etc.) are
        # skipped — callers needing them should call the SDK directly.
        text_parts: list[str] = []
        for block in getattr(response, "content", []) or []:
            block_text = getattr(block, "text", None)
            if isinstance(block_text, str):
                text_parts.append(block_text)
        content = "".join(text_parts)

        usage_attr = getattr(response, "usage", None)
        prompt_tokens = int(getattr(usage_attr, "input_tokens", 0) or 0)
        completion_tokens = int(getattr(usage_attr, "output_tokens", 0) or 0)
        # Anthropic reasoning models (Opus 4.7+) emit `thinking_tokens` in
        # the usage block; fold into `reasoning_tokens` for parity with
        # OpenRouter's response shape. Older SDK versions return None →
        # zero. Pricing-wise these are billed at the output rate by
        # Anthropic and are already counted in `output_tokens`, so the
        # cost estimate doesn't add them.
        reasoning_tokens = int(getattr(usage_attr, "thinking_tokens", 0) or 0)
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cache_read_tokens": int(
                getattr(usage_attr, "cache_read_input_tokens", 0) or 0
            ),
            "cache_write_tokens": int(
                getattr(usage_attr, "cache_creation_input_tokens", 0) or 0
            ),
            "reasoning_tokens": reasoning_tokens,
            "cost_usd": estimate_cost(
                "anthropic",
                resolved_model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            ),
            "duration_ms": elapsed_ms,
            "system_fingerprint": None,
        }
        return content, usage

    def preflight(self, *, timeout: int = DEFAULT_PREFLIGHT_TIMEOUT_SECONDS) -> None:
        """Smoke-test API key + connectivity before launching a batch.

        Hits the cheap ``models.list()`` endpoint instead of burning an
        LLM call. Returns ``None`` on success; raises
        :class:`AnthropicError` with an actionable
        ``ANTHROPIC_API_KEY`` remediation message on any failure.

        The raised error carries ``context["preflight"] = True`` so log
        filters can distinguish preflight failures from per-call failures.

        Args:
            timeout: Wall-clock cap (seconds) for the smoke call. Defaults
                to :data:`DEFAULT_PREFLIGHT_TIMEOUT_SECONDS` — preflight
                should complete in single-digit seconds; if it doesn't,
                something is worth knowing about.
        """
        sdk = self._client.with_options(timeout=timeout)
        try:
            sdk.models.list()
        except Exception as e:
            raise AnthropicError(
                f"Anthropic preflight failed: {e}. "
                "Most likely ANTHROPIC_API_KEY is missing, expired, or "
                "doesn't have access to this account.",
                context={"preflight": True},
                cause=e,
            ) from e
        _log.info(
            "anthropic_preflight_ok",
            model=self.model,
        )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_client: AnthropicClient | None = None


def get_client(
    *,
    api_key: str | None = None,
    model: str | None = None,
    request_timeout: int | None = None,
    retry: int = 0,
) -> AnthropicClient:
    """Return the module-level singleton, creating it on first call.

    All arguments are optional — on first call, reads from env vars if
    not provided. Subsequent calls return the cached instance (args
    ignored). Use :func:`new_client` for a fresh, independently-configured
    instance.
    """
    global _client
    if _client is None:
        _client = new_client(
            api_key=api_key,
            model=model,
            request_timeout=request_timeout,
            retry=retry,
        )
    return _client


def new_client(
    *,
    api_key: str | None = None,
    model: str | None = None,
    request_timeout: int | None = None,
    retry: int = 0,
) -> AnthropicClient:
    """Construct a fresh client with the same env-var resolution as
    :func:`get_client`, but no caching — every call returns a new instance.

    The escape hatch from the singleton's first-call-wins semantics: use it
    (directly, or via per-backend ``client_kwargs`` in the model router)
    when different agents need differently-tuned clients in one process.
    """
    resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not resolved_key:
        raise AnthropicError(
            "AnthropicClient requires ANTHROPIC_API_KEY env var or "
            "explicit api_key= argument."
        )
    return AnthropicClient(
        api_key=resolved_key,
        model=model or os.environ.get("ANTHROPIC_MODEL") or None,
        request_timeout=request_timeout
        or int(os.environ.get("REQUEST_TIMEOUT", str(DEFAULT_TIMEOUT_SECONDS))),
        retry=retry,
    )


def reset_client() -> None:
    """Reset the singleton (useful for testing)."""
    global _client
    _client = None
