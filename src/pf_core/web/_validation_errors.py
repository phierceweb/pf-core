"""Validation errors that wrap a validator's InvalidInputError are answered as that error.

Before InvalidInputError was a ValueError, one raised in a validator escaped validation and
reached the app's FlowException handlers. pydantic now wraps it — in its own ``ValidationError``,
or FastAPI's request, websocket or response validation error — so the handler for each of those
four classes is wrapped: a wrapped InvalidInputError goes to the handler for its own class, and
anything else to the handler that answered it before. The wrapping happens when the middleware
stack is built, so a handler the app registers after ``create_app`` is wrapped too.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import (
    RequestValidationError,
    ResponseValidationError,
    WebSocketRequestValidationError,
)
from pydantic import ValidationError
from starlette._utils import is_async_callable  # FastAPI's own routing imports it
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp

from pf_core.exceptions import unwrap_flow_exception

_WRAPPERS = (
    ValidationError,
    RequestValidationError,
    WebSocketRequestValidationError,
    ResponseValidationError,
)


class _Unwrapping:
    """A wrapped InvalidInputError to its own class's handler; the rest to ``previous``."""

    def __init__(self, app: FastAPI, previous: Any) -> None:
        self.app = app
        self.previous = previous

    async def __call__(self, conn: Any, exc: Exception) -> Any:
        flow = unwrap_flow_exception(exc)
        target: Exception = exc if flow is None else flow
        handler = (
            self.previous if flow is None and self.previous else _handler_for(self.app, target)
        )
        if handler is None:
            raise target
        if is_async_callable(handler):
            return await handler(conn, target)
        return await run_in_threadpool(handler, conn, target)


def _handler_for(app: FastAPI, exc: Exception) -> Any:
    """The handler Starlette's ExceptionMiddleware picks for ``exc``, the unwrappers left out.

    ``Exception`` is answered by ServerErrorMiddleware after the error propagates, so no handler
    here is the same as none at all: the caller re-raises.
    """
    for cls in type(exc).__mro__:
        handler = app.exception_handlers.get(cls)
        if cls is not Exception and handler is not None and not isinstance(handler, _Unwrapping):
            return handler
    return None


def wrap_validation_handlers(app: FastAPI) -> None:
    """Wrap the handler for each validation error class, once."""
    for cls in _WRAPPERS:
        current = app.exception_handlers.get(cls)
        if not isinstance(current, _Unwrapping):
            app.exception_handlers[cls] = _Unwrapping(app, current)


class FlowAwareFastAPI(FastAPI):
    """A FastAPI app whose validation-error handlers answer a wrapped InvalidInputError as one."""

    def build_middleware_stack(self) -> ASGIApp:
        wrap_validation_handlers(self)
        return super().build_middleware_stack()
