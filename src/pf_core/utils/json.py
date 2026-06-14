"""JSON parsing and canonicalization utilities.

Replaces the repetitive ``try: json.loads(x) except: fallback`` pattern
with concise, structured helpers that handle None, empty strings, and
already-parsed SQLite JSON columns, plus a canonical serializer for
equality comparison and hashing.

Usage::

    from pf_core.utils.json import safe_json_loads, safe_json_col, canonical_json

    data = safe_json_loads(raw_string, fallback={}, label="config_blob")
    col  = safe_json_col(row["metadata"])
    key  = canonical_json({"b": 1, "a": 2})   # '{"a":2,"b":1}'
"""

from __future__ import annotations

import json
from typing import Any

from pf_core.log import get_logger

_logger = None


def _get_logger():
    global _logger
    if _logger is None:
        _logger = get_logger(__name__)
    return _logger


def safe_json_loads(
    val: str | None,
    *,
    fallback: Any = None,
    label: str | None = None,
) -> Any:
    """Parse a JSON string, returning *fallback* on failure.

    Args:
        val: Raw JSON string, ``None``, or empty string.
        fallback: Value returned when *val* is missing or unparseable.
        label: If provided, a WARNING is logged on parse failure with
            this label and a preview of the raw value (first 100 chars).

    Returns:
        Parsed Python object, or *fallback* if parsing fails.
    """
    if val is None or val == "":
        return fallback

    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError, ValueError):
        if label is not None:
            preview = val[:100] if isinstance(val, str) else repr(val)[:100]
            _get_logger().warning(
                "json_parse_failed",
                label=label,
                preview=preview,
            )
        return fallback


def safe_json_col(
    val: Any,
    *,
    fallback: Any = None,
    label: str | None = None,
) -> Any:
    """Normalise a value that may be a JSON string or already-parsed object.

    SQLite JSON columns return parsed ``dict``/``list`` objects directly,
    while other backends return raw strings. This function handles both.

    Args:
        val: Column value -- ``None``, ``str``, ``dict``, ``list``, or
            other type.
        fallback: Value returned when *val* is ``None``, an unrecognised
            type, or an unparseable string.
        label: Forwarded to :func:`safe_json_loads` when *val* is a
            string.

    Returns:
        Parsed Python object, or *fallback*.
    """
    if val is None:
        return fallback

    if isinstance(val, (dict, list)):
        return val

    if isinstance(val, str):
        return safe_json_loads(val, fallback=fallback, label=label)

    return fallback


def canonical_json(obj: Any) -> str:
    """Serialize *obj* to canonical JSON: sorted keys, compact separators.

    Two semantically-equal objects produce byte-identical output, so the result
    is safe to compare for equality or feed to a hash (see
    :func:`pf_core.utils.hashing.content_hash`). Values JSON can't represent
    natively (``datetime``, ``Decimal``, …) fall back to ``str`` so the function
    never raises.

    Args:
        obj: Any JSON-serializable value (with ``str`` fallback for the rest).

    Returns:
        A deterministic JSON string — ``sort_keys`` with ``","``/``":"``
        separators (no insignificant whitespace).
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
