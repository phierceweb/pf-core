"""
Jinja2 templates setup helper.

Usage::

    from pf_core.web.templates import setup_templates

    templates = setup_templates(
        app,
        template_dir=Path("app/templates"),
        extra_globals={"app_name": "My App"},
    )

    # In route handlers:
    return templates.TemplateResponse(request, "pages/home.html", {...})
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates


def setup_templates(
    app: FastAPI,
    template_dir: Path | str,
    *,
    extra_globals: dict[str, Any] | None = None,
    extra_filters: dict[str, Any] | None = None,
) -> Jinja2Templates:
    """Create and register Jinja2Templates on the FastAPI app.

    The templates instance is stored on app.state.templates so that
    error handlers can access it for HTML error pages.

    Args:
        app: The FastAPI application.
        template_dir: Path to the templates directory.
        extra_globals: Additional Jinja2 globals to register.
        extra_filters: Additional Jinja2 filters to register.

    Returns:
        The configured Jinja2Templates instance.
    """
    templates = Jinja2Templates(directory=str(template_dir))

    if extra_globals:
        templates.env.globals.update(extra_globals)

    if extra_filters:
        templates.env.filters.update(extra_filters)

    # Store on app state for error handlers
    app.state.templates = templates

    return templates
