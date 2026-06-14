"""
Rate limiting for FastAPI applications via slowapi.

Reads ``API_RATE_LIMIT_PER_MINUTE`` from :class:`~pf_core.config.AppConfig`
(env var, default 60). Falls back gracefully: if slowapi is not installed the
app runs without rate limiting; if a configured Redis backend is unreachable at
startup, it falls back to in-memory storage rather than 500-ing every request.

Usage::

    from pf_core.web.rate_limit import setup_rate_limit

    app = create_app(title="My App")
    limiter = setup_rate_limit(app)

    # Per-route override:
    @app.get("/expensive")
    @limiter.limit("5/minute")
    async def expensive(request: Request):
        ...
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI

from pf_core.log import get_logger

logger = get_logger(__name__)

try:
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    from slowapi.middleware import SlowAPIMiddleware
    from slowapi.util import get_remote_address

    _HAS_SLOWAPI = True
except ImportError:
    _HAS_SLOWAPI = False


def setup_rate_limit(
    app: FastAPI,
    *,
    redis_url: str | None = None,
    key_func: Any | None = None,
) -> Any | None:
    """Configure rate limiting on a FastAPI application.

    Reads ``API_RATE_LIMIT_PER_MINUTE`` from the environment (default 60).

    Args:
        app: The FastAPI application.
        redis_url: Redis URL for distributed limiting. Falls back to
            ``REDIS_URL`` env var, then to in-memory storage.
        key_func: Callable to extract the rate-limit key from a request.
            Defaults to ``get_remote_address`` (client IP).

    Returns:
        The ``Limiter`` instance (for per-route ``@limiter.limit()`` overrides),
        or ``None`` if slowapi is not installed.
    """
    if not _HAS_SLOWAPI:
        logger.warning("rate_limit_skipped", reason="slowapi not installed")
        return None

    rpm = int(os.environ.get("API_RATE_LIMIT_PER_MINUTE", "60"))
    default_limit = f"{rpm}/minute"

    url = redis_url or os.environ.get("REDIS_URL", "").strip() or "memory://"

    # A configured-but-unreachable Redis must not take the app down. slowapi
    # raises the storage ConnectionError lazily on the first request and then
    # misroutes it to the RateLimitExceeded handler (which crashes on
    # ``exc.detail``), 500-ing every request. Probe the backend up front and
    # fall back to in-memory storage so rate limiting degrades gracefully.
    if url.startswith(("redis://", "rediss://")):
        try:
            from limits.storage import storage_from_string

            if not storage_from_string(url).check():
                raise ConnectionError(f"storage check failed for {url}")
        except Exception as exc:
            logger.warning(
                "rate_limit_redis_unreachable",
                uri=url,
                error=str(exc),
                fallback="memory://",
            )
            url = "memory://"

    func = key_func or get_remote_address

    limiter = Limiter(
        key_func=func,
        storage_uri=url,
        default_limits=[default_limit],
    )

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)

    logger.info(
        "rate_limit_configured",
        default_limit=default_limit,
        storage="redis" if url.startswith("redis") else "memory",
    )

    return limiter
