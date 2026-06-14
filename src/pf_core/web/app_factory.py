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
from html import escape as _html_escape
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware

from pf_core.exceptions import (
    ActionNotAllowedError,
    AppError,
    ConfigurationError,
    FlowException,
    InvalidInputError,
    NotFoundError,
    PreconditionError,
)
from pf_core.log import get_logger, log_exception

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Built-in error page (self-contained HTML — no template dependency)
# ---------------------------------------------------------------------------

_ERROR_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{title}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #f8fafc; color: #334155;
    display: flex; align-items: center; justify-content: center;
    min-height: 100vh; padding: 2rem;
  }}
  .card {{
    text-align: center; max-width: 28rem;
  }}
  .code {{
    font-size: 5rem; font-weight: 700; color: #cbd5e1;
    line-height: 1; margin-bottom: 0.5rem; letter-spacing: -0.02em;
  }}
  h1 {{
    font-size: 1.25rem; font-weight: 600; color: #1e293b; margin-bottom: 0.5rem;
  }}
  p {{
    color: #64748b; margin-bottom: 2rem; line-height: 1.5;
  }}
  .actions {{
    display: flex; gap: 0.75rem; justify-content: center; flex-wrap: wrap;
  }}
  a {{
    display: inline-block; padding: 0.5rem 1.25rem; border-radius: 0.375rem;
    font-size: 0.875rem; font-weight: 500; text-decoration: none;
    transition: background 0.15s, color 0.15s;
  }}
  .primary {{
    background: #2563eb; color: #fff;
  }}
  .primary:hover {{ background: #1d4ed8; }}
  .secondary {{
    background: #f1f5f9; color: #475569;
  }}
  .secondary:hover {{ background: #e2e8f0; }}
</style>
</head>
<body>
<div class="card">
  <div class="code">{code}</div>
  <h1>{heading}</h1>
  <p>{message}</p>
  <div class="actions">
    <a href="/" class="primary">Go home</a>
    <a href="javascript:history.back()" class="secondary">Go back</a>
  </div>
</div>
</body>
</html>"""

_STATUS_HEADINGS = {
    400: "Bad request",
    403: "Forbidden",
    404: "Page not found",
    405: "Method not allowed",
    409: "Conflict",
    422: "Validation error",
    429: "Too many requests",
    500: "Something went wrong",
    502: "Bad gateway",
    503: "Service unavailable",
}

_STATUS_MESSAGES = {
    400: "The request couldn't be processed. Check the input and try again.",
    403: "You don't have permission to access this.",
    404: "The page you're looking for doesn't exist or has been moved.",
    405: "This HTTP method isn't supported for this URL.",
    409: "The request conflicts with the current state of the resource.",
    422: "The submitted data didn't pass validation.",
    429: "You're sending too many requests. Please slow down.",
    500: "An unexpected error occurred. Try again or go back.",
    502: "The upstream server returned an invalid response.",
    503: "The service is temporarily unavailable. Try again in a moment.",
}


def _render_error(
    request: Request,
    code: int,
    heading: str,
    message: str,
    *,
    app: FastAPI,
) -> HTMLResponse | JSONResponse:
    """Render an error as HTML or JSON based on Accept header."""
    accept = request.headers.get("accept", "")

    # If the project has a custom error template, use it
    if "text/html" in accept and hasattr(app.state, "templates"):
        try:
            return app.state.templates.TemplateResponse(
                request,
                "shared/error.html",
                {"title": heading, "code": code, "heading": heading, "message": message},
                status_code=code,
            )
        except Exception:
            pass  # Template missing or broken — fall through to built-in

    # Built-in self-contained error page for HTML requests. Escape all
    # interpolated text — message can carry request-reflected content.
    if "text/html" in accept:
        html = _ERROR_PAGE.format(
            title=_html_escape(f"{code} — {heading}"),
            code=code,
            heading=_html_escape(heading),
            message=_html_escape(message),
        )
        return HTMLResponse(html, status_code=code)

    # JSON for API clients
    return JSONResponse({"detail": message}, status_code=code)


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
                    method=method, path=path, status=status,
                    duration_ms=duration_ms,
                )
            elif status >= 400:
                logger.warning(
                    "http_request",
                    method=method, path=path, status=status,
                    duration_ms=duration_ms,
                )
            else:
                logger.debug(
                    "http_request",
                    method=method, path=path, status=status,
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
        static_dir: Path to static files directory (mounted at /static).
        template_dir: Path to Jinja2 templates directory (stored on app.state).
        log_requests: Enable request logging middleware (default True).
        rate_limit: Enable rate limiting (default True). Reads
            ``API_RATE_LIMIT_PER_MINUTE`` from env. Requires ``pf-core[ratelimit]``.
        **fastapi_kwargs: Additional kwargs passed to FastAPI().

    Returns:
        Configured FastAPI application.
    """
    app = FastAPI(title=title, version=version, **fastapi_kwargs)

    # --- Middleware (order matters: last added = outermost) ---

    # CORS
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
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

    # -- FlowException subclasses: each domain exception → specific HTTP status --

    @app.exception_handler(NotFoundError)
    async def not_found_handler(request: Request, exc: NotFoundError):
        """NotFoundError → 404."""
        return _render_error(
            request, 404,
            heading=_STATUS_HEADINGS[404],
            message=str(exc),
            app=app,
        )

    @app.exception_handler(InvalidInputError)
    async def invalid_input_handler(request: Request, exc: InvalidInputError):
        """InvalidInputError → 422."""
        return _render_error(
            request, 422,
            heading=_STATUS_HEADINGS[422],
            message=str(exc),
            app=app,
        )

    @app.exception_handler(PreconditionError)
    async def precondition_handler(request: Request, exc: PreconditionError):
        """PreconditionError → 409 Conflict."""
        return _render_error(
            request, 409,
            heading="Conflict",
            message=str(exc),
            app=app,
        )

    @app.exception_handler(ActionNotAllowedError)
    async def action_not_allowed_handler(request: Request, exc: ActionNotAllowedError):
        """ActionNotAllowedError → 403."""
        return _render_error(
            request, 403,
            heading=_STATUS_HEADINGS[403],
            message=str(exc),
            app=app,
        )

    @app.exception_handler(ConfigurationError)
    async def configuration_error_handler(request: Request, exc: ConfigurationError):
        """ConfigurationError → 500 (missing config = broken app)."""
        log_exception(exc, message_prepend="configuration error")
        return _render_error(
            request, 500,
            heading=_STATUS_HEADINGS[500],
            message=_STATUS_MESSAGES[500],
            app=app,
        )

    @app.exception_handler(FlowException)
    async def flow_exception_handler(request: Request, exc: FlowException):
        """FlowException catch-all → 400 (for any future subclasses)."""
        return _render_error(
            request, 400,
            heading=_STATUS_HEADINGS[400],
            message=str(exc),
            app=app,
        )

    # -- AppError branch: actual errors, always logged --

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        """AppError → 500 (actual errors, logged with full context)."""
        log_exception(exc, message_prepend="unhandled app error")
        return _render_error(
            request, 500,
            heading=_STATUS_HEADINGS[500],
            message=_STATUS_MESSAGES[500],
            app=app,
        )

    async def _handle_http_exc(request: Request, exc):
        """Shared handler for both FastAPI and Starlette HTTPExceptions."""
        code = exc.status_code
        heading = _STATUS_HEADINGS.get(code, f"Error {code}")
        message = exc.detail or _STATUS_MESSAGES.get(code, "An error occurred.")

        if code >= 500:
            logger.error("http_error", status=code, detail=exc.detail,
                         path=request.url.path)

        return _render_error(request, code, heading=heading, message=message, app=app)

    # Register for both FastAPI and Starlette HTTPException (they're different classes)
    app.exception_handler(HTTPException)(_handle_http_exc)
    app.exception_handler(StarletteHTTPException)(_handle_http_exc)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        """Catch-all for unhandled exceptions — log and show 500 page."""
        logger.error(
            "unhandled_exception",
            exc_type=type(exc).__name__,
            exc_msg=str(exc)[:500],
            path=request.url.path,
            method=request.method,
            exc_info=exc,
        )
        return _render_error(
            request, 500,
            heading=_STATUS_HEADINGS[500],
            message=_STATUS_MESSAGES[500],
            app=app,
        )

    # --- Rate limiting ---
    if rate_limit:
        from pf_core.web.rate_limit import setup_rate_limit
        setup_rate_limit(app)

    return app
