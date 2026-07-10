"""Shared helpers for the llm_admin query modules — windows + row normalize."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any


def default_window(days: int = 7) -> tuple[dt.datetime, dt.datetime]:
    """Return ``(since, until)`` for the default window."""
    now = dt.datetime.now(dt.timezone.utc)
    return now - dt.timedelta(days=days), now


def parse_window(
    since: str | None, until: str | None, *, default_days: int = 7
) -> tuple[dt.datetime, dt.datetime]:
    """Parse ISO strings into a ``(since, until)`` datetime pair."""
    default_since, default_until = default_window(default_days)
    s = dt.datetime.fromisoformat(since) if since else default_since
    u = dt.datetime.fromisoformat(until) if until else default_until
    return s, u


def _normalize(row: Any) -> dict:
    out: dict[str, Any] = {}
    for k, v in dict(row).items():
        if isinstance(v, Decimal):
            out[k] = float(v)
        else:
            out[k] = v
    return out


def _normalize_all(rows) -> list[dict]:
    return [_normalize(r) for r in rows]
