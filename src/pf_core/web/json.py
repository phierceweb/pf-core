"""
JSON serialization helpers for FastAPI responses.

Handles types that the stdlib JSON encoder chokes on: date, datetime,
Decimal, bytes, and SQLAlchemy Row objects.

Usage::

    from pf_core.web.json import safe_json_response

    @router.get("/events")
    async def list_events():
        rows = repo.list_events()
        return safe_json_response(rows)
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi.responses import JSONResponse


def json_default(obj: Any) -> Any:
    """JSON serializer for types not handled by the stdlib encoder.

    Handles: date, datetime, Decimal, bytes, SQLAlchemy Row/RowMapping.
    """
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return int(obj) if obj == int(obj) else float(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    # SQLAlchemy Row objects expose _mapping
    if hasattr(obj, "_mapping"):
        return dict(obj._mapping)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def safe_json_response(data: Any, **kwargs: Any) -> JSONResponse:
    """JSONResponse that handles date/datetime/Decimal/Row objects.

    Args:
        data: The response payload (dict, list, or any JSON-serializable structure).
        **kwargs: Additional kwargs passed to JSONResponse (e.g. status_code).

    Returns:
        A FastAPI JSONResponse with properly serialized content.
    """
    content = json.loads(json.dumps(data, default=json_default))
    return JSONResponse(content=content, **kwargs)
