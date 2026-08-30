"""
FastAPI application factory.

Creates a configured FastAPI app with:
  - CORS middleware
  - Request logging middleware (method, path, status, duration)
  - Structured error handlers with HTML fallback pages
  - Static file serving
  - Optional Jinja2 template setup

Usage::

    from pf_core.web.app_factory import create_app

    app = create_app(
        title="My App",
        cors_origins=["http://localhost:3000"],
        static_dir=Path("app/static"),
    )
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware

from pf_core.exceptions import ConfigurationError
from pf_core.log import get_logger
from pf_core.web._error_handlers import register_error_handlers
from pf_core.web._validation_errors import FlowAwareFastAPI

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Request logging middleware
# ---------------------------------------------------------------------------


class _RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log every request with method, path, status code, and duration."""

    async def dispatch(self, request: Request, call_next):
        t0 = time.monotonic()
        response = None
        try:
            response = await call_next(request)
            return response
        finally:
            duration_ms = int((time.monotonic() - t0) * 1000)
            status = response.status_code if response else 500
            path = request.url.path
            method = request.method

            if status >= 500:
                logger.error(
                    "http_request",
                    method=method,
                    path=path,
                    status=status,
                    duration_ms=duration_ms,
                )
            elif status >= 400:
                logger.warning(
                    "http_request",
                    method=method,
                    path=path,
                    status=status,
                    duration_ms=duration_ms,
                )
            else:
                logger.debug(
                    "http_request",
                    method=method,
                    path=path,
                    status=status,
                    duration_ms=duration_ms,
                )


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    *,
    title: str = "App",
    version: str = "0.1.0",
    cors_origins: list[str] | None = None,
    cors_allow_credentials: bool = True,
    static_dir: Path | str | None = None,
    template_dir: Path | str | None = None,
    log_requests: bool = True,
    rate_limit: bool = True,
    **fastapi_kwargs: Any,
) -> FastAPI:
    """Create a FastAPI app with standard framework middleware and error handlers.

    Args:
        title: Application title.
        version: Application version.
        cors_origins: List of allowed CORS origins (empty = no CORS middleware).
            A ``"*"`` entry is refused while credentials are enabled — list
            explicit origins, or set ``cors_allow_credentials=False``.
        cors_allow_credentials: Send ``Access-Control-Allow-Credentials``
            (default True).
        static_dir: Path to static files directory (mounted at /static).
        template_dir: Path to Jinja2 templates directory (stored on app.state).
        log_requests: Enable request logging middleware (default True).
        rate_limit: Enable rate limiting (default True). Reads
            ``API_RATE_LIMIT_PER_MINUTE`` from env. Requires ``pf-core[ratelimit]``.
        **fastapi_kwargs: Additional kwargs passed to FastAPI().

    Returns:
        Configured FastAPI application.
    """
    app = FlowAwareFastAPI(title=title, version=version, **fastapi_kwargs)

    # --- Middleware (order matters: last added = outermost) ---

    # CORS
    if cors_origins:
        # Starlette echoes the requesting origin once credentials are on, so the
        # browser's wildcard-plus-credentials protection never engages.
        # Membership, not equality: ["*", "https://ok"] is still wildcard.
        if cors_allow_credentials and any(o.strip() == "*" for o in cors_origins):
            raise ConfigurationError(
                "cors_origins contains '*' with credentials enabled — every origin "
                "would be allowed to read authenticated responses. List explicit "
                "origins, or pass cors_allow_credentials=False."
            )
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=cors_allow_credentials,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # Request logging (outermost so it wraps everything including errors)
    if log_requests:
        app.add_middleware(_RequestLoggingMiddleware)

    # --- Static files ---
    if static_dir:
        sd = Path(static_dir)
        if sd.is_dir():
            app.mount("/static", StaticFiles(directory=str(sd)), name="static")

    # --- Template dir on app state ---
    if template_dir:
        app.state.template_dir = str(Path(template_dir))

    # --- Error handlers ---
    register_error_handlers(app)

    # --- Rate limiting ---
    if rate_limit:
        from pf_core.web.rate_limit import setup_rate_limit

        setup_rate_limit(app)

    return app
