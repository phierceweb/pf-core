"""
OpenRouter API client.

Thin wrapper around the OpenAI-compatible chat completions endpoint:
  - Configurable per-request timeout
  - Provider ignore list
  - Usage tracking (tokens + cost + duration)
  - Module-level singleton with lazy init

Usage::

    from pf_core.clients.openrouter import get_client

    client = get_client()
    content, usage = client.chat(
        messages=[{"role": "user", "content": "Hello"}],
        model="anthropic/claude-sonnet-4.6",
    )
"""

from __future__ import annotations

import math
import random
import time
from typing import Any

try:
    import httpx
except ImportError as e:  # pragma: no cover - exercised by bare-install CI
    from pf_core._extras import extra_import_error

    raise extra_import_error("llm", "httpx", feature="pf_core.clients.openrouter") from e

from pf_core.exceptions import ClientError
from pf_core.log import get_logger
from pf_core.pricing import estimate_cost

_log = get_logger(__name__)


# Wall-clock cap for ``preflight()`` — should be much shorter than the
# per-request default. The whole point is to fail fast on a missing
# API key or unreachable host.
DEFAULT_PREFLIGHT_TIMEOUT_SECONDS = 30

# Status codes that indicate a transient failure worth retrying. 429
# is rate-limit (transient by design); 5xx is server-side. 4xx other
# than 429 are caller errors (bad request, auth, missing scope) that
# will deterministically fail every retry — don't burn API budget.
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

# Jitter keeps parallel workers from re-hitting a saturated rate-limit
# window in lockstep.
_RETRY_AFTER_CAP_SECONDS = 30.0
_RETRY_BACKOFF_SECONDS = 0.5
_RETRY_JITTER_FRACTION = 0.25


class OpenRouterError(ClientError):
    """OpenRouter API call failed."""


def _retry_delay(attempt: int, retry_after: str | None = None) -> float:
    """Seconds to wait before the next attempt — the server's ``Retry-After``
    (capped) when it's a sane number, else jittered backoff."""
    if retry_after:
        try:
            value = float(retry_after)
        except (TypeError, ValueError):
            pass
        else:
            # time.sleep rejects a negative or NaN argument.
            if not math.isnan(value):
                return max(0.0, min(value, _RETRY_AFTER_CAP_SECONDS))
    base = _RETRY_BACKOFF_SECONDS * (attempt + 1)
    return base + random.uniform(0.0, base * _RETRY_JITTER_FRACTION)


class OpenRouterClient:
    """Client for the OpenRouter chat completions API.

    Args:
        api_key: OpenRouter API key. Required.
        base_url: API base URL (default: https://openrouter.ai/api/v1).
        app_name: Application name sent in X-Title header.
        app_url: Application URL sent in HTTP-Referer header.
        provider_ignore: List of providers to exclude from routing.
        request_timeout: Per-request socket timeout in seconds.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://openrouter.ai/api/v1",
        app_name: str = "",
        app_url: str = "",
        provider_ignore: list[str] | None = None,
        request_timeout: int = 120,
        retry: int = 0,
    ) -> None:
        if not api_key:
            raise OpenRouterError(
                "OPENROUTER_API_KEY not set. Add it to .env."
            )
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.app_name = app_name
        self.app_url = app_url
        self.provider_ignore = provider_ignore or []
        self.request_timeout = request_timeout
        self.retry = retry

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if self.app_url:
            h["HTTP-Referer"] = self.app_url
        if self.app_name:
            h["X-Title"] = self.app_name
        return h

    def chat(
        self,
        messages: list[dict],
        model: str,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        top_p: float = 1.0,
        response_format: dict | None = None,
        timeout: int | None = None,
        **kwargs: Any,
    ) -> tuple[str, dict]:
        """Send a chat completion request.

        Returns:
            (content, usage) where usage has keys:
            prompt_tokens, completion_tokens, cache_read_tokens,
            cache_write_tokens, reasoning_tokens, cost_usd, duration_ms,
            system_fingerprint.
        """
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "top_p": top_p,
        }
        if response_format:
            body["response_format"] = response_format
        body.update(kwargs)

        # Merge provider ignore list
        if self.provider_ignore:
            prov = dict(body.get("provider") or {})
            prior = list(prov.get("ignore") or [])
            merged = list(dict.fromkeys(prior + self.provider_ignore))
            prov["ignore"] = merged
            body["provider"] = prov

        req_timeout = timeout or self.request_timeout

        url = f"{self.base_url}/chat/completions"
        headers = self._headers()

        # Up to (self.retry + 1) attempts. Default retry=0 means one shot.
        # Retryable: timeout (network blip), 429 (rate limit), 5xx
        # (server). 4xx-other (400/401/403/...) are caller errors and
        # are NOT retried — they'd just burn API budget on a deterministic
        # failure.
        resp = None
        elapsed_ms = 0
        for attempt in range(self.retry + 1):
            t0 = time.monotonic()
            try:
                resp = httpx.post(
                    url, headers=headers, json=body, timeout=req_timeout
                )
            except httpx.TimeoutException as e:
                if attempt < self.retry:
                    delay = _retry_delay(attempt)
                    _log.warning(
                        "openrouter_retry_timeout",
                        attempt=attempt + 1,
                        of=self.retry + 1,
                        model=model,
                        timeout=req_timeout,
                        delay_s=round(delay, 2),
                    )
                    time.sleep(delay)
                    continue
                raise OpenRouterError(
                    f"Request timed out after {req_timeout}s "
                    f"(after {attempt + 1} attempt(s)): {e}",
                    context={
                        "model": model,
                        "timeout": req_timeout,
                        "attempts": attempt + 1,
                    },
                )
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            if resp.status_code in _RETRYABLE_STATUS_CODES and attempt < self.retry:
                delay = _retry_delay(
                    attempt,
                    resp.headers.get("Retry-After")
                    if resp.status_code == 429
                    else None,
                )
                _log.warning(
                    "openrouter_retry_status",
                    attempt=attempt + 1,
                    of=self.retry + 1,
                    model=model,
                    status_code=resp.status_code,
                    body_head=resp.text[:200],
                    delay_s=round(delay, 2),
                )
                time.sleep(delay)
                continue
            break  # success or non-retryable failure — fall through

        if resp is None:
            # Only reachable when retry < 0 leaves the attempt loop unentered.
            raise OpenRouterError(
                f"no request attempted (retry={self.retry}); retry must be >= 0",
                context={"model": model, "retry": self.retry},
            )

        if resp.status_code != 200:
            raise OpenRouterError(
                f"OpenRouter {resp.status_code}: {resp.text[:500]}",
                context={"model": model, "status_code": resp.status_code},
            )

        data = resp.json()

        if "error" in data:
            raise OpenRouterError(
                f"OpenRouter error: {data['error']}",
                context={"model": model},
            )

        choices = data.get("choices") or []
        if not choices:
            raise OpenRouterError(
                "OpenRouter returned no choices",
                context={"model": model, "body_head": resp.text[:200]},
            )
        choice = choices[0]
        raw_content = (choice.get("message") or {}).get("content")
        content = raw_content if isinstance(raw_content, str) else ""
        finish_reason = choice.get("finish_reason")
        usage_raw = data.get("usage") or {}
        prompt_details = usage_raw.get("prompt_tokens_details") or {}
        completion_details = usage_raw.get("completion_tokens_details") or {}

        # Prefer OpenRouter's server-reported cost; fall back to a local
        # estimate only when a route doesn't surface one.
        server_cost = usage_raw.get("cost")

        usage = {
            "prompt_tokens": usage_raw.get("prompt_tokens", 0),
            "completion_tokens": usage_raw.get("completion_tokens", 0),
            "cache_read_tokens": (
                usage_raw.get("cache_read_tokens")
                or prompt_details.get("cached_tokens")
                or 0
            ),
            "cache_write_tokens": (
                usage_raw.get("cache_write_tokens")
                or usage_raw.get("cache_creation_input_tokens")
                or 0
            ),
            "reasoning_tokens": (
                usage_raw.get("reasoning_tokens")
                or completion_details.get("reasoning_tokens")
                or 0
            ),
            "cost_usd": (
                server_cost
                if server_cost is not None
                else estimate_cost(
                    "openrouter",
                    model,
                    prompt_tokens=usage_raw.get("prompt_tokens", 0) or 0,
                    completion_tokens=usage_raw.get("completion_tokens", 0) or 0,
                )
            ),
            "duration_ms": elapsed_ms,
            "system_fingerprint": data.get("system_fingerprint"),
            "finish_reason": finish_reason,
        }

        if finish_reason == "length":
            _log.warning(
                "openrouter_truncated",
                model=model,
                max_tokens=max_tokens,
                completion_tokens=usage["completion_tokens"],
            )

        # Citation URLs (Perplexity routes) ride in usage so content stays
        # exactly what the model returned (raw_response / cache hold it).
        citations = data.get("citations") or []
        if citations:
            usage["citations"] = list(citations)

        return content, usage

    def preflight(self, *, timeout: int = DEFAULT_PREFLIGHT_TIMEOUT_SECONDS) -> None:
        """Smoke-test API key + connectivity before launching a batch.

        Hits the cheap ``GET /models`` endpoint instead of burning an
        LLM call. Returns ``None`` on success; raises
        :class:`OpenRouterError` with an actionable remediation message
        on any failure (auth lapse, network, rate-limit, server error).

        The raised error carries ``context["preflight"] = True`` so log
        filters can distinguish preflight failures from per-call failures.

        Args:
            timeout: Wall-clock cap (seconds) for the smoke call. Defaults
                to :data:`DEFAULT_PREFLIGHT_TIMEOUT_SECONDS` — preflight
                should complete in single-digit seconds; if it doesn't,
                something is worth knowing about.
        """
        url = f"{self.base_url}/models"
        try:
            resp = httpx.get(url, headers=self._headers(), timeout=timeout)
        except httpx.TimeoutException as e:
            raise OpenRouterError(
                f"OpenRouter preflight timed out after {timeout}s — "
                f"check network connectivity to {self.base_url}. "
                f"Underlying: {e}",
                context={"preflight": True, "timeout": timeout},
            )
        except httpx.HTTPError as e:
            raise OpenRouterError(
                f"OpenRouter preflight failed: {e}. "
                f"Check network connectivity to {self.base_url}.",
                context={"preflight": True},
                cause=e,
            )
        if resp.status_code == 401 or resp.status_code == 403:
            raise OpenRouterError(
                f"OpenRouter preflight returned {resp.status_code}. "
                "Most likely OPENROUTER_API_KEY is missing, expired, or "
                "doesn't have access to this base URL.",
                context={
                    "preflight": True,
                    "status_code": resp.status_code,
                    "body_head": resp.text[:200],
                },
            )
        if resp.status_code != 200:
            raise OpenRouterError(
                f"OpenRouter preflight returned {resp.status_code}: "
                f"{resp.text[:500]}",
                context={
                    "preflight": True,
                    "status_code": resp.status_code,
                    "body_head": resp.text[:200],
                },
            )
        _log.info(
            "openrouter_preflight_ok",
            base_url=self.base_url,
            status_code=resp.status_code,
        )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_client: OpenRouterClient | None = None


def get_client(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    app_name: str | None = None,
    app_url: str | None = None,
    provider_ignore: list[str] | None = None,
    request_timeout: int | None = None,
    retry: int = 0,
) -> OpenRouterClient:
    """Return the module-level singleton, creating it on first call.

    All arguments are optional — on first call, reads from env vars if not
    provided. Subsequent calls return the cached instance (args ignored).
    Use :func:`new_client` for a fresh, independently-configured instance.
    """
    global _client
    if _client is None:
        _client = new_client(
            api_key=api_key,
            base_url=base_url,
            app_name=app_name,
            app_url=app_url,
            provider_ignore=provider_ignore,
            request_timeout=request_timeout,
            retry=retry,
        )
    return _client


def new_client(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    app_name: str | None = None,
    app_url: str | None = None,
    provider_ignore: list[str] | None = None,
    request_timeout: int | None = None,
    retry: int = 0,
) -> OpenRouterClient:
    """Construct a fresh client with the same env-var resolution as
    :func:`get_client`, but no caching — every call returns a new instance.

    The escape hatch from the singleton's first-call-wins semantics: use it
    (directly, or via per-backend ``client_kwargs`` in the model router)
    when different agents need differently-tuned clients in one process.
    """
    import os

    return OpenRouterClient(
        api_key=api_key or os.environ.get("OPENROUTER_API_KEY", ""),
        base_url=base_url or os.environ.get(
            "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
        ),
        app_name=app_name or os.environ.get("APP_NAME", ""),
        app_url=app_url or os.environ.get("APP_URL", ""),
        provider_ignore=provider_ignore or [
            s.strip()
            for s in os.environ.get("OPENROUTER_PROVIDER_IGNORE", "").split(",")
            if s.strip()
        ],
        request_timeout=request_timeout or int(
            os.environ.get("REQUEST_TIMEOUT", "120")
        ),
        retry=retry,
    )


def reset_client() -> None:
    """Reset the singleton (useful for testing)."""
    global _client
    _client = None
