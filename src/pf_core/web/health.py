"""
Health check endpoint for FastAPI applications.

Provides a ``/health`` router that checks database and (optionally) Redis
connectivity.  Returns 200 with check results or 503 if any check fails.

Usage::

    from pf_core.web.health import health_router

    app = create_app(title="My App")
    app.include_router(health_router())

    # With Redis check:
    app.include_router(health_router(check_redis=True))

    # As a FastAPI dependency on other routes:
    from pf_core.web.health import require_db

    @app.get("/data", dependencies=[Depends(require_db)])
    async def get_data(): ...
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from pf_core.log import get_logger

logger = get_logger(__name__)


def _check_db() -> str:
    """Ping the database; return ``"ok"`` or an error message."""
    try:
        from pf_core.db import ping
        ping()
        return "ok"
    except Exception as e:
        logger.warning("health_check_db_failed", error=str(e))
        return f"error: {e}"


def _check_redis() -> str:
    """Ping Redis; return ``"ok"`` or an error message."""
    try:
        import redis as redis_lib
        import os

        url = os.environ.get("REDIS_URL", "").strip()
        if not url:
            return "skipped: REDIS_URL not set"
        client = redis_lib.from_url(url, socket_connect_timeout=2)
        client.ping()
        return "ok"
    except Exception as e:
        logger.warning("health_check_redis_failed", error=str(e))
        return f"error: {e}"


def health_router(
    *,
    check_db: bool = True,
    check_redis: bool = False,
    prefix: str = "",
) -> APIRouter:
    """Create an ``APIRouter`` with a ``GET /health`` endpoint.

    Args:
        check_db: Include a database connectivity check (default True).
        check_redis: Include a Redis connectivity check (default False).
        prefix: Optional URL prefix for the router.

    Returns:
        An ``APIRouter`` ready to be included in a FastAPI app.
    """
    router = APIRouter(prefix=prefix)

    @router.get("/health")
    async def health():
        checks: dict[str, str] = {}
        if check_db:
            checks["db"] = _check_db()
        if check_redis:
            checks["redis"] = _check_redis()

        all_ok = all(v == "ok" for v in checks.values())
        status = "ok" if all_ok else "degraded"
        code = 200 if all_ok else 503

        return JSONResponse(
            {"status": status, "checks": checks},
            status_code=code,
        )

    return router


async def require_db() -> None:
    """FastAPI dependency that raises 503 if the database is unreachable.

    Usage::

        from pf_core.web.health import require_db

        @app.get("/data", dependencies=[Depends(require_db)])
        async def get_data(): ...
    """
    result = _check_db()
    if result != "ok":
        raise HTTPException(status_code=503, detail=f"Database unavailable: {result}")
