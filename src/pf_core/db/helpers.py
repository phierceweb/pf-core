"""
Shared database helper functions.

Small shared database helpers: JSON column coercion, ISO timestamps,
safe dict conversion.

Usage::

    from pf_core.db.helpers import coerce_json_col, dumps_json, now_iso

    data = coerce_json_col(row["json_column"])  # always returns list
    ts = now_iso()                               # "2026-04-12T14:30:00Z"
"""

from __future__ import annotations

import json
from typing import Any

# Re-export from canonical location for backward compatibility
from pf_core.utils.dates import now_iso  # noqa: F401


def coerce_json_col(val: Any) -> list:
    """Coerce a DB column value to a Python list.

    Handles: None, list, JSON string, other iterables.
    Always returns a list — never raises on bad input.
    """
    if val is None:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        if not val.strip():
            return []
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, list) else [parsed]
        except (json.JSONDecodeError, ValueError):
            return []
    if hasattr(val, "__iter__") and not isinstance(val, (str, bytes)):
        return list(val)
    return []


def dumps_json(obj: Any) -> str:
    """Serialize to JSON without ASCII-escaping unicode."""
    return json.dumps(obj, ensure_ascii=False)


def row_to_dict(row: Any) -> dict | None:
    """Convert a SQLAlchemy Row or mapping to a plain dict.

    Accepts: dict, SQLAlchemy Row, RowMapping, or None.
    Returns: dict or None.
    """
    if row is None:
        return None
    if isinstance(row, dict):
        return row
    # SQLAlchemy Row objects support _mapping
    if hasattr(row, "_mapping"):
        return dict(row._mapping)
    # Already a mapping (from .mappings())
    if hasattr(row, "keys"):
        return dict(row)
    return dict(row)
