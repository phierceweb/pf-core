"""URL liveness checking with optional caching and browser-UA fallback.

A layer on top of :func:`pf_core.utils.urls.check_url` that adds three things
needed in production but absent from the bare primitive:

1. **Cached results.** Liveness is expensive and reasonably stable; consumers
   almost always want a TTL'd cache in front of it.
2. **Browser-UA GET fallback on 403/401.** Major news sites (NYT, WaPo, AP)
   and some federal portals return 403 to bare HEAD even when the content is
   real, because they're aggressive about bot detection. The bare ``check_url``
   already uses a browser-like UA but doesn't reissue as GET. This module
   does — distinguishing real fabrication from bot-block.
3. **A kill switch.** Audit pipelines need a single boolean to bypass liveness
   during incidents (network outage, mass false positives) without code edits.

Usage::

    from pf_core.utils.url_liveness import check_url_cached

    # No cache — useful in tests:
    check_url_cached("https://example.com")

    # With a redis-py client (or any object satisfying CacheBackend):
    import redis
    r = redis.from_url("redis://localhost:6379/0")
    check_url_cached(
        "https://example.com",
        cache=r,
        cache_key_prefix="myapp:url_liveness:",
        cache_ttl_seconds=86400,
    )

    # Kill switch (consumer derives the boolean from env, config, etc.):
    check_url_cached(url, disabled=os.environ.get("URL_LIVENESS_DISABLED") == "1")

The "trusted domain" short-circuit some consumers want (skip liveness for
known-good evidence-tier sites that frequently bot-block) is **NOT** in this
module — the trusted-domain list is project policy, not framework
infrastructure. Consumers wrap this function with their own short-circuit.
"""

from __future__ import annotations

import json
from typing import Protocol

try:
    import httpx
except ImportError as e:  # pragma: no cover - exercised by bare-install CI
    from pf_core._extras import extra_import_error

    raise extra_import_error("http", "httpx", feature="pf_core.utils.url_liveness") from e

from pf_core.utils.urls import check_url

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_BROWSER_HEADERS = {
    "User-Agent": _BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

DEFAULT_CACHE_TTL_SECONDS = 86_400  # 24h


class CacheBackend(Protocol):
    """Minimal key-value protocol for liveness result caching.

    Compatible with the ``redis-py`` ``Redis`` client out of the box —
    ``get(key) -> bytes | None`` and ``setex(key, ttl, value)`` match.
    Consumers using a different store (in-memory dict for tests, dogpile
    region adapter, etc.) provide a small wrapper that satisfies this shape.
    """

    def get(self, key: str) -> bytes | str | None: ...

    def setex(self, key: str, time: int, value: str) -> None: ...


def _get_with_browser_ua(url: str, timeout: int = 10) -> tuple[int, str]:
    """GET with browser UA and redirect-follow.

    Used as fallback when ``check_url`` returns 403/401 — distinguishes
    real bot-block (still 403 via GET → "forbidden") from real content
    behind a paywall (200 via GET → "ok").
    """
    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            verify=False,
            headers=_BROWSER_HEADERS,
        ) as client:
            resp = client.get(url)
            code = resp.status_code
            if 200 <= code < 300:
                return code, "ok"
            if code == 404:
                return code, "not_found"
            if code == 410:
                return code, "gone"
            if code in (401, 403):
                return code, "forbidden"
            return code, f"http_{code}"
    except httpx.TimeoutException:
        return 0, "timeout"
    except Exception:
        return 0, "error"


def _read_cache(cache: CacheBackend | None, key: str) -> tuple[int, str] | None:
    if cache is None:
        return None
    try:
        raw = cache.get(key)
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        return int(data[0]), str(data[1])
    except Exception:
        return None


def _write_cache(
    cache: CacheBackend | None, key: str, ttl_seconds: int, code: int, category: str
) -> None:
    if cache is None:
        return
    try:
        cache.setex(key, ttl_seconds, json.dumps([code, category]))
    except Exception:
        pass


def check_url_cached(
    url: str,
    *,
    cache: CacheBackend | None = None,
    cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
    cache_key_prefix: str = "url_liveness:",
    disabled: bool = False,
) -> tuple[int, str]:
    """Liveness check with optional cache and browser-UA fallback.

    Returns ``(status_code, category)`` where category is one of:
      ``ok``, ``not_found``, ``gone``, ``forbidden``, ``timeout``,
      ``error``, ``http_<code>``, ``disabled``.

    Args:
        url: HTTP(S) URL to check. Empty string returns ``(0, "error")``.
        cache: Optional key-value backend implementing :class:`CacheBackend`.
            ``None`` (default) disables both reading and writing — every
            call goes to the network.
        cache_ttl_seconds: TTL for cached entries. Default 24h.
        cache_key_prefix: Prefix prepended to ``url`` to form the cache key.
            Use a project-namespaced prefix (e.g. ``"myapp:url_liveness:"``)
            to avoid collisions when multiple consumers share a Redis.
        disabled: When ``True``, returns ``(0, "disabled")`` without any
            network or cache activity. Caller derives this boolean however
            it likes (env var, feature flag, runtime toggle).

    Behavior:
        - HEAD via :func:`pf_core.utils.urls.check_url`.
        - On 403 or 401, retries as GET with browser UA + follow_redirects;
          a 200 via GET means real content behind bot-protection (returns
          ``(200, "ok")``); a 403 via GET keeps the original verdict.
        - Successful results are written to cache; ``"disabled"`` and
          ``"error"`` shortcuts are not cached.
    """
    if disabled:
        return 0, "disabled"
    if not url:
        return 0, "error"

    cache_key = cache_key_prefix + url
    cached = _read_cache(cache, cache_key)
    if cached is not None:
        return cached

    code, category = check_url(url)
    if category == "forbidden" or code == 401:
        code, category = _get_with_browser_ua(url)

    _write_cache(cache, cache_key, cache_ttl_seconds, code, category)
    return code, category
