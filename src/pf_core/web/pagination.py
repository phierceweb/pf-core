"""
Pagination helpers for FastAPI route handlers.

Provides parameter validation and result formatting for paginated
list endpoints — both HTML template and JSON API contexts.

Usage::

    from pf_core.web.pagination import paginate_params, paginate_result

    @router.get("/entries")
    async def list_entries(
        page: int = Query(1, ge=1),
        per_page: int = Query(50, ge=1, le=200),
        sort: str = Query("date"),
        dir: str = Query("desc"),
    ):
        p = paginate_params(
            page, per_page, sort, dir,
            allowed_sorts={"date", "title", "tier"},
        )
        rows = repo.list_entries(
            sort_by=p["sort"], sort_dir=p["dir"],
            limit=p["limit"], offset=p["offset"],
        )
        total = repo.count_entries()
        return paginate_result(rows, total=total, page=p["page"], per_page=p["per_page"])
"""

from __future__ import annotations

import os
from typing import Any


def paginate_params(
    page: int,
    per_page: int,
    sort: str | None = None,
    dir: str | None = None,
    *,
    allowed_sorts: set[str] | None = None,
    default_sort: str = "id",
    default_dir: str = "desc",
    max_per_page: int | None = None,
) -> dict[str, Any]:
    """Validate and normalize pagination query parameters.

    Args:
        page: Current page number (1-based).
        per_page: Items per page.
        sort: Sort field name.
        dir: Sort direction (``"asc"`` or ``"desc"``).
        allowed_sorts: Valid sort field names. If provided and *sort* is not
            in the set, falls back to *default_sort*.
        default_sort: Fallback sort field.
        default_dir: Fallback sort direction.
        max_per_page: Upper bound for *per_page*. Reads ``MAX_PER_PAGE`` from
            env (default 200) when not provided.

    Returns:
        Dict with keys: ``page``, ``per_page``, ``offset``, ``limit``
        (``per_page + 1`` for has-next detection), ``sort``, ``dir``.
    """
    if max_per_page is None:
        max_per_page = int(os.environ.get("MAX_PER_PAGE", "200"))

    page = max(1, page)
    per_page = max(1, min(per_page, max_per_page))

    sort_key = (sort or default_sort).strip().lower()
    if allowed_sorts and sort_key not in allowed_sorts:
        sort_key = default_sort

    sort_dir = "asc" if (dir or "").strip().lower() == "asc" else default_dir

    offset = (page - 1) * per_page

    return {
        "page": page,
        "per_page": per_page,
        "offset": offset,
        "limit": per_page + 1,
        "sort": sort_key,
        "dir": sort_dir,
    }


def paginate_result(
    items: list,
    *,
    total: int,
    page: int,
    per_page: int,
) -> dict[str, Any]:
    """Build pagination metadata from a result set.

    Expects *items* fetched with ``limit = per_page + 1`` so that
    ``has_next`` can be detected without a separate count query for
    that flag.

    Args:
        items: Row list (may contain up to ``per_page + 1`` items).
        total: Total matching row count (from a count query).
        page: Current page (1-based).
        per_page: Items per page.

    Returns:
        Dict with keys: ``items`` (trimmed to *per_page*), ``page``,
        ``per_page``, ``total``, ``total_pages``, ``has_prev``, ``has_next``.
    """
    total_pages = max(1, (total + per_page - 1) // per_page)
    has_next = len(items) > per_page
    trimmed = items[:per_page]

    return {
        "items": trimmed,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": has_next,
    }
