"""__NAME__ — FastAPI application factory.

``create_app`` from pf-core ships request logging, self-contained error pages
with JSON content negotiation, CORS, rate limiting, and exception→status
mapping. Register routers on the returned app.
"""

from __future__ import annotations

from pf_core.web.app_factory import create_app

from app.api.pages import router as pages_router


def make_app():
    application = create_app(title="__NAME__", version="0.1.0")
    application.include_router(pages_router)
    return application


app = make_app()
