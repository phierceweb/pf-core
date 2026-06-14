"""Brave Search API client.

Thin HTTP wrapper around the Brave Web Search API. Useful as a real
search backend when you need URLs from a real index — typically as an
alternative to LLM-with-search agents (Perplexity, etc.) which can
hallucinate URLs.

Why a dedicated client (instead of :func:`pf_core.utils.urls.fetch_url_content`):

- Auth: Brave requires the ``X-Subscription-Token`` header.
- Response shape: this module pins the ``web.results[]`` →
  ``{title, url, description, age, page_age}`` contract so
  consumers can rely on it.
- Rate limits: Brave free tier is 1 query/second. The client never
  auto-sleeps — callers handle backoff.

Usage::

    from pf_core.clients.brave import get_client

    client = get_client()  # reads BRAVE_API_KEY from env
    results, usage = client.search("python async web frameworks", count=5)
    for r in results:
        print(r["url"], r["title"])

    # `usage` mirrors openrouter's shape, so it forwards directly into
    # any agent-run logging that expects (cost_usd, duration_ms, tokens).

Every call raises :class:`BraveSearchError` on transport, auth,
rate-limit, or malformed-response failure. Callers should catch and
treat as a hard failure.

Pricing model: Brave charges per call, not per token. The client logs
each call at a fixed rate (default $0.005, configurable). Set the rate
to your account's actual price; the default is the documented free-tier
upper bound.
"""

from __future__ import annotations

import os
import time
from typing import Any

try:
    import httpx
except ImportError as e:  # pragma: no cover - exercised by bare-install CI
    from pf_core._extras import extra_import_error

    raise extra_import_error("llm", "httpx", feature="pf_core.clients.brave") from e

from pf_core.exceptions import ClientError


class BraveSearchError(ClientError):
    """Brave Search API call failed."""


_DEFAULT_BASE_URL = "https://api.search.brave.com/res/v1"
_DEFAULT_TIMEOUT = 30
_DEFAULT_COST_PER_CALL_USD = 0.005


class BraveSearchClient:
    """Client for the Brave Web Search API.

    Args:
        api_key: Brave subscription token. Required.
        base_url: API base URL.
        request_timeout: Per-request socket timeout in seconds.
        cost_per_call_usd: Logged cost per ``search()`` call. Set to
            your account's actual rate; default is the upper-bound
            published price.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = _DEFAULT_BASE_URL,
        request_timeout: int = _DEFAULT_TIMEOUT,
        cost_per_call_usd: float = _DEFAULT_COST_PER_CALL_USD,
    ) -> None:
        if not api_key:
            raise BraveSearchError(
                "BRAVE_API_KEY not set. Pass api_key=... or set the env var."
            )
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.request_timeout = request_timeout
        self.cost_per_call_usd = cost_per_call_usd

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": self.api_key,
        }

    def search(
        self,
        query: str,
        *,
        count: int = 10,
        freshness: str | None = None,
        country: str = "us",
        safesearch: str = "moderate",
        extra_params: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Run one web search.

        Args:
            query: Search query (Brave accepts up to 400 chars).
            count: Number of results to return (1–20; Brave caps at 20).
            freshness: Optional recency filter — one of ``"pd"`` (past day),
                ``"pw"`` (past week), ``"pm"`` (past month), ``"py"``
                (past year), or an absolute range
                ``"YYYY-MM-DDtoYYYY-MM-DD"``.
            country: Two-letter country code for result localization.
            safesearch: ``"off"`` / ``"moderate"`` / ``"strict"``.
            extra_params: Raw query params merged onto the request
                (escape hatch; prefer named args above).

        Returns:
            ``(results, usage)``. ``results`` is a list of dicts each with
            ``url``, ``title``, ``description``, ``age``, ``page_age``.
            ``usage`` mirrors the openrouter shape: ``{prompt_tokens,
            completion_tokens, cost_usd, duration_ms}`` — token counts
            are always 0 since Brave is per-call.

        Raises:
            BraveSearchError: network failure, non-200 response, malformed
                JSON, or empty query.
        """
        if not query or not query.strip():
            raise BraveSearchError(
                "Brave search called with empty query.",
                context={"query": query},
            )
        count = max(1, min(int(count), 20))

        params: dict[str, Any] = {
            "q": query.strip(),
            "count": count,
            "country": country,
            "safesearch": safesearch,
            "result_filter": "web",
        }
        if freshness:
            params["freshness"] = freshness
        if extra_params:
            params.update(extra_params)

        url = f"{self.base_url}/web/search"
        headers = self._headers()

        t0 = time.monotonic()
        try:
            resp = httpx.get(
                url, headers=headers, params=params,
                timeout=self.request_timeout,
            )
        except httpx.TimeoutException as e:
            raise BraveSearchError(
                f"Brave request timed out after {self.request_timeout}s",
                context={"query": query, "timeout": self.request_timeout},
                cause=e,
            )
        except httpx.HTTPError as e:
            raise BraveSearchError(
                f"Brave transport error: {e}",
                context={"query": query},
                cause=e,
            )

        elapsed_ms = int((time.monotonic() - t0) * 1000)

        if resp.status_code == 429:
            raise BraveSearchError(
                "Brave rate-limited (HTTP 429). Free tier is 1 QPS.",
                context={
                    "query": query,
                    "status_code": 429,
                    "duration_ms": elapsed_ms,
                },
            )
        if resp.status_code in (401, 403):
            raise BraveSearchError(
                f"Brave auth failed ({resp.status_code}). Check api_key.",
                context={"status_code": resp.status_code},
            )
        if resp.status_code != 200:
            raise BraveSearchError(
                f"Brave {resp.status_code}: {resp.text[:500]}",
                context={
                    "query": query,
                    "status_code": resp.status_code,
                    "duration_ms": elapsed_ms,
                },
            )

        try:
            data = resp.json()
        except ValueError as e:
            raise BraveSearchError(
                "Brave returned non-JSON response",
                context={"query": query, "body_head": resp.text[:200]},
                cause=e,
            )

        web = data.get("web") or {}
        raw_results = web.get("results") or []
        out: list[dict[str, Any]] = []
        for r in raw_results:
            if not isinstance(r, dict):
                continue
            u = r.get("url")
            if not isinstance(u, str) or not u:
                continue
            out.append({
                "url": u,
                "title": str(r.get("title") or ""),
                "description": str(r.get("description") or ""),
                "age": r.get("age"),
                "page_age": r.get("page_age"),
            })

        usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cost_usd": self.cost_per_call_usd,
            "duration_ms": elapsed_ms,
        }
        return out, usage


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_client: BraveSearchClient | None = None


def get_client(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    request_timeout: int | None = None,
    cost_per_call_usd: float | None = None,
) -> BraveSearchClient:
    """Return the module-level singleton, creating it on first call.

    All arguments are optional — on first call, reads from env vars if
    not provided. Subsequent calls return the cached instance (args
    ignored).

    Env vars consulted on first call:
        BRAVE_API_KEY (required)
        BRAVE_BASE_URL (default: https://api.search.brave.com/res/v1)
        BRAVE_REQUEST_TIMEOUT (default: 30)
        BRAVE_COST_PER_CALL_USD (default: 0.005)
    """
    global _client
    if _client is None:
        _client = BraveSearchClient(
            api_key=api_key or os.environ.get("BRAVE_API_KEY", ""),
            base_url=base_url or os.environ.get(
                "BRAVE_BASE_URL", _DEFAULT_BASE_URL,
            ),
            request_timeout=request_timeout or int(
                os.environ.get("BRAVE_REQUEST_TIMEOUT", str(_DEFAULT_TIMEOUT))
            ),
            cost_per_call_usd=(
                cost_per_call_usd
                if cost_per_call_usd is not None
                else float(os.environ.get(
                    "BRAVE_COST_PER_CALL_USD",
                    str(_DEFAULT_COST_PER_CALL_USD),
                ))
            ),
        )
    return _client


def reset_client() -> None:
    """Reset the singleton (useful for testing)."""
    global _client
    _client = None


__all__ = [
    "BraveSearchClient",
    "BraveSearchError",
    "get_client",
    "reset_client",
]
