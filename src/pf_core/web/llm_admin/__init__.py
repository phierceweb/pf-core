"""
Mountable admin sub-app for reading LLM tracking + jobs + cache + budget tables.

Usage::

    from pf_core.web.llm_admin import make_admin_router

    app.include_router(
        make_admin_router(
            auth_dep=require_admin,      # FastAPI dependency supplied by consumer
            prefix="/admin/llm",
            config_resolvers={"report_config": lambda cid: f"Report {cid}"},
        ),
    )

See ``docs/llm-admin.md``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.templating import Jinja2Templates

from pf_core.exceptions import ConfigurationError

# Templates live alongside this package
_TEMPLATE_DIR = Path(__file__).parent / "templates"


def make_admin_router(
    *,
    auth_dep: Callable | None = None,
    prefix: str = "/admin/llm",
    config_resolvers: dict[str, Callable[[int], str]] | None = None,
    templates: Jinja2Templates | None = None,
    allow_unauthenticated: bool = False,
) -> APIRouter:
    """Build a FastAPI ``APIRouter`` wiring all admin pages + JSON endpoints.

    The admin surfaces rendered prompts, raw LLM responses, cost tables, and a
    state-changing ``POST /api/job/{id}/cancel`` — so it requires auth. Pass an
    ``auth_dep``; to run it open (local dev only) you must opt in explicitly with
    ``allow_unauthenticated=True``.

    Args:
        auth_dep: FastAPI dependency that protects every route.
        prefix: Mount path. Defaults to ``/admin/llm``.
        config_resolvers: Map of ``config_kind`` → callable that takes a
            config id and returns a human-readable label. Unregistered kinds
            render as ``kind:id``.
        templates: Optional pre-configured Jinja2Templates. When omitted, a
            fresh instance is built from the packaged template directory.
        allow_unauthenticated: Explicit opt-in to mount with no ``auth_dep``
            (dev only). Default ``False``.

    Returns:
        A configured ``APIRouter`` ready to pass to ``app.include_router()``.

    Raises:
        ConfigurationError: If ``auth_dep`` is ``None`` and
            ``allow_unauthenticated`` was not set — refuses to mount an
            unauthenticated admin by accident.
    """
    if auth_dep is None and not allow_unauthenticated:
        raise ConfigurationError(
            "make_admin_router requires auth_dep; the admin exposes prompts, "
            "responses, and a job-cancel POST. Pass auth_dep, or set "
            "allow_unauthenticated=True to run it open (dev only)."
        )
    if templates is None:
        templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

    resolvers = dict(config_resolvers or {})

    def _resolve_config(kind: str, cid: int) -> str:
        fn = resolvers.get(kind)
        if fn is None:
            return f"{kind}:{cid}"
        try:
            return fn(cid)
        except Exception:
            return f"{kind}:{cid}"

    templates.env.globals["resolve_config"] = _resolve_config
    templates.env.globals["admin_prefix"] = prefix

    dependencies = [Depends(auth_dep)] if auth_dep is not None else []
    router = APIRouter(prefix=prefix, dependencies=dependencies)

    # Register page + JSON routes (imported lazily to avoid circular imports)
    from pf_core.web.llm_admin.api import register_api_routes
    from pf_core.web.llm_admin.pages import register_page_routes

    register_page_routes(router, templates)
    register_api_routes(router)

    return router


__all__ = ["make_admin_router"]
