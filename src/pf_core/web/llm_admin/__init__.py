"""
Mountable admin sub-app for reading LLM tracking + jobs + cache + budget tables.

Usage::

    from pf_core.web.llm_admin import make_admin_router

    app.include_router(
        make_admin_router(
            auth_dep=require_admin,      # FastAPI dependency supplied by consumer
            prefix="/admin/llm",
            config_resolvers={"essay_config": lambda cid: f"Essay {cid}"},
        ),
    )

See ``docs/llm-admin.md``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.templating import Jinja2Templates

# Templates live alongside this package
_TEMPLATE_DIR = Path(__file__).parent / "templates"


def make_admin_router(
    *,
    auth_dep: Callable | None = None,
    prefix: str = "/admin/llm",
    config_resolvers: dict[str, Callable[[int], str]] | None = None,
    templates: Jinja2Templates | None = None,
) -> APIRouter:
    """Build a FastAPI ``APIRouter`` wiring all admin pages + JSON endpoints.

    Args:
        auth_dep: FastAPI dependency that protects every route. When ``None``,
            the admin is public (dev only).
        prefix: Mount path. Defaults to ``/admin/llm``.
        config_resolvers: Map of ``config_kind`` → callable that takes a
            config id and returns a human-readable label. Unregistered kinds
            render as ``kind:id``.
        templates: Optional pre-configured Jinja2Templates. When omitted, a
            fresh instance is built from the packaged template directory.

    Returns:
        A configured ``APIRouter`` ready to pass to ``app.include_router()``.
    """
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
