"""Shared helpers for the job repository modules — UTC coercion, model
serialization, lease defaults, and the step-index creation lock."""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from typing import Any

_step_creation_lock = threading.Lock()

# Columns on jobs/job_steps/job_events that are stored as naive UTC (SQLite
# and MySQL) or aware UTC (Postgres TIMESTAMPTZ). See ``_coerce_row_utc``
# for why we stamp the naive variants as aware UTC on read.
_JOB_DT_COLS: tuple[str, ...] = (
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
    "claimed_at",
)


def _default_lease_seconds() -> int:
    try:
        return int(os.environ.get("JOB_LEASE_SECONDS", "300"))
    except (ValueError, TypeError):
        return 300


def _dump_model(value: Any) -> Any:
    """Coerce a Pydantic model to a plain JSON-serializable dict.

    Leaves dicts/lists/primitives untouched. Required because Pydantic
    models don't survive JSON serialization round-trip across all DB
    dialects.
    """
    if value is None:
        return None
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    return value


def _normalize_input_dt(value: datetime | None) -> datetime | None:
    """Normalize a caller-supplied datetime to naive UTC for binding.

    Naive input is treated as already-UTC and returned unchanged; aware
    input is converted to UTC and stripped of tzinfo so MySQL/SQLite
    (naive-UTC TIMESTAMP columns, session pinned to UTC) compare
    correctly. Safe for Postgres TIMESTAMPTZ too. ``None`` passes through.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _stamp_utc(value: datetime | None) -> datetime | None:
    """Stamp a naive datetime with ``tzinfo=timezone.utc``.

    Naive values returned from ``JobRepo`` reads are guaranteed to be UTC
    by the schema contract (SQLite's ``CURRENT_TIMESTAMP`` is UTC; MySQL's
    session is pinned to UTC in ``pf_core.db.connection``). Aware values
    from Postgres TIMESTAMPTZ are returned unchanged.
    """
    if value is None:
        return None
    if value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _coerce_row_utc(row: dict | None) -> dict | None:
    """Stamp every known datetime column in a row dict as aware UTC.

    Mutates the dict in place and returns it for chaining. Safe to call
    on rows that lack some of the columns (``get_events`` only has
    ``created_at``, for instance).
    """
    if row is None:
        return None
    for col in _JOB_DT_COLS:
        if col in row:
            row[col] = _stamp_utc(row[col])
    return row
