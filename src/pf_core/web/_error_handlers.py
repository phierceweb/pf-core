"""The exception handlers ``create_app`` registers: each pf-core exception to its HTTP status."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from starlette.exceptions import HTTPException as StarletteHTTPException

from pf_core.budget.check import CostBudgetExceeded
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
from pf_core.web._error_page import STATUS_HEADINGS, STATUS_MESSAGES, render_error

logger = get_logger("pf_core.web.app_factory")  # the logger these events have always named


def register_error_handlers(app: FastAPI) -> None:
    """Register the FlowException, AppError, HTTPException and catch-all handlers on ``app``."""
    # -- FlowException subclasses: each domain exception → specific HTTP status --
    # Starlette picks the handler for the most specific class in the
    # exception's MRO, so these always beat the FlowException catch-all.

    @app.exception_handler(NotFoundError)
    async def not_found_handler(request: Request, exc: NotFoundError):
        """NotFoundError → 404."""
        return render_error(
            request,
            404,
            heading=STATUS_HEADINGS[404],
            message=str(exc),
            app=app,
        )

    @app.exception_handler(InvalidInputError)
    async def invalid_input_handler(request: Request, exc: InvalidInputError):
        """InvalidInputError → 422."""
        return render_error(
            request,
            422,
            heading=STATUS_HEADINGS[422],
            message=str(exc),
            app=app,
        )

    @app.exception_handler(PreconditionError)
    async def precondition_handler(request: Request, exc: PreconditionError):
        """PreconditionError → 409 Conflict."""
        return render_error(
            request,
            409,
            heading="Conflict",
            message=str(exc),
            app=app,
        )

    @app.exception_handler(ActionNotAllowedError)
    async def action_not_allowed_handler(request: Request, exc: ActionNotAllowedError):
        """ActionNotAllowedError → 403."""
        return render_error(
            request,
            403,
            heading=STATUS_HEADINGS[403],
            message=str(exc),
            app=app,
        )

    @app.exception_handler(ConfigurationError)
    async def configuration_error_handler(request: Request, exc: ConfigurationError):
        """ConfigurationError → 500 (missing config = broken app)."""
        log_exception(exc, message_prepend="configuration error")
        return render_error(
            request,
            500,
            heading=STATUS_HEADINGS[500],
            message=STATUS_MESSAGES[500],
            app=app,
        )

    @app.exception_handler(CostBudgetExceeded)
    async def cost_budget_exceeded_handler(request: Request, exc: CostBudgetExceeded):
        """CostBudgetExceeded → 429 (spend cap hit)."""
        return render_error(
            request,
            429,
            heading=STATUS_HEADINGS[429],
            message=str(exc),
            app=app,
        )

    @app.exception_handler(FlowException)
    async def flow_exception_handler(request: Request, exc: FlowException):
        """FlowException catch-all → 400 (for any future subclasses)."""
        return render_error(
            request,
            400,
            heading=STATUS_HEADINGS[400],
            message=str(exc),
            app=app,
        )

    # -- AppError branch: actual errors, always logged --

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        """AppError → 500 (actual errors, logged with full context)."""
        log_exception(exc, message_prepend="unhandled app error")
        return render_error(
            request,
            500,
            heading=STATUS_HEADINGS[500],
            message=STATUS_MESSAGES[500],
            app=app,
        )

    async def _handle_http_exc(request: Request, exc):
        """Shared handler for both FastAPI and Starlette HTTPExceptions."""
        code = exc.status_code
        heading = STATUS_HEADINGS.get(code, f"Error {code}")
        message = exc.detail or STATUS_MESSAGES.get(code, "An error occurred.")

        if code >= 500:
            logger.error("http_error", status=code, detail=exc.detail, path=request.url.path)

        return render_error(request, code, heading=heading, message=message, app=app)

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
        return render_error(
            request,
            500,
            heading=STATUS_HEADINGS[500],
            message=STATUS_MESSAGES[500],
            app=app,
        )
