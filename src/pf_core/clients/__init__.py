"""Client interfaces and implementations for external APIs.

The ``ChatClient`` protocol is the injection seam for LLM calls. Production
code uses ``pf_core.clients.openrouter.OpenRouterClient``; tests pass a fake
that satisfies the protocol structurally (no inheritance required).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ChatClient(Protocol):
    """Structural interface for an LLM chat client.

    Any object with a ``chat()`` method matching this signature satisfies
    the protocol — no base class or registration required.

    Implementations return ``(content, usage)`` where ``usage`` is a dict
    with at least ``prompt_tokens``, ``completion_tokens``, ``cost_usd``,
    and ``duration_ms``.
    """

    def chat(
        self,
        messages: list[dict],
        model: str,
        temperature: float = ...,
        max_tokens: int = ...,
        top_p: float = ...,
        response_format: dict | None = ...,
        timeout: int | None = ...,
        **kwargs: Any,
    ) -> tuple[str, dict]: ...


__all__ = ["ChatClient"]
