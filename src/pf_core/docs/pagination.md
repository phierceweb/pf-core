# Pagination

Helpers for paginated list endpoints — works with both HTML templates and JSON APIs.

## Functions

### `paginate_params`

Validates and normalizes pagination query parameters.

```python
from pf_core.web.pagination import paginate_params

p = paginate_params(
    page, per_page, sort, dir,
    allowed_sorts={"date", "title", "tier"},
    default_sort="date",
)
# p = {"page": 1, "per_page": 50, "offset": 0, "limit": 51, "sort": "date", "dir": "desc"}
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `page` | `int` | — | Current page (1-based). Clamped to minimum 1. |
| `per_page` | `int` | — | Items per page. Clamped to 1–`max_per_page`. |
| `sort` | `str \| None` | `None` | Sort field. Falls back to `default_sort` if missing or not in `allowed_sorts`. |
| `dir` | `str \| None` | `None` | Sort direction. Accepts `"asc"` (case-insensitive); anything else falls back to `default_dir`. |
| `allowed_sorts` | `set[str] \| None` | `None` | Valid sort fields. If `None`, any sort value is accepted. |
| `default_sort` | `str` | `"id"` | Fallback sort field. |
| `default_dir` | `str` | `"desc"` | Fallback sort direction. |
| `max_per_page` | `int \| None` | `None` | Upper bound for `per_page`. Reads `MAX_PER_PAGE` from env (default 200) when not provided. |

**Returns** a dict with: `page`, `per_page`, `offset`, `limit` (`per_page + 1` for has-next detection), `sort`, `dir`.

The `limit` is `per_page + 1` so you can detect whether there's a next page by checking if the query returned more rows than `per_page` — without needing a separate count query just for that flag.

### `paginate_result`

Builds pagination metadata from a result set.

```python
from pf_core.web.pagination import paginate_result

rows = repo.list_entries(sort_by=p["sort"], sort_dir=p["dir"], limit=p["limit"], offset=p["offset"])
total = repo.count_entries()
result = paginate_result(rows, total=total, page=p["page"], per_page=p["per_page"])
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `items` | `list` | Row list (may contain up to `per_page + 1` items). |
| `total` | `int` | Total matching row count. |
| `page` | `int` | Current page (1-based). |
| `per_page` | `int` | Items per page. |

**Returns** a dict with: `items` (trimmed to `per_page`), `page`, `per_page`, `total`, `total_pages`, `has_prev`, `has_next`.

## Full example — HTML template endpoint

```python
from fastapi import Query, Request
from pf_core.web.pagination import paginate_params, paginate_result

@router.get("/incidents")
async def incidents_page(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=1, le=200),
    sort: str = Query("date"),
    dir: str = Query("desc"),
):
    p = paginate_params(
        page, per_page, sort, dir,
        allowed_sorts={"date", "tier", "section", "type"},
        default_sort="date",
    )
    rows = repo.list_incidents(
        sort_by=p["sort"], sort_dir=p["dir"],
        limit=p["limit"], offset=p["offset"],
    )
    total = repo.count_incidents()
    pg = paginate_result(rows, total=total, page=p["page"], per_page=p["per_page"])

    return templates.TemplateResponse(request, "pages/incidents.html", {
        "incidents": pg["items"],
        "sort": p["sort"],
        "dir": p["dir"],
        **pg,  # page, per_page, total, total_pages, has_prev, has_next
    })
```

## Full example — JSON API endpoint

```python
from pf_core.web.pagination import paginate_params, paginate_result
from pf_core.web.json import safe_json_response

@router.get("/api/entries")
async def list_entries(
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    sort: str = Query("created_at"),
    dir: str = Query("desc"),
):
    p = paginate_params(page, per_page, sort, dir, allowed_sorts={"created_at", "title"})
    rows = repo.list_entries(sort_by=p["sort"], sort_dir=p["dir"], limit=p["limit"], offset=p["offset"])
    total = repo.count_entries()
    return safe_json_response(paginate_result(rows, total=total, page=p["page"], per_page=p["per_page"]))
```

Response:
```json
{
  "items": [...],
  "page": 1,
  "per_page": 25,
  "total": 142,
  "total_pages": 6,
  "has_prev": false,
  "has_next": true
}
```
