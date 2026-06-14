"""
Date and time utilities.

Consolidates date helpers that were duplicated across consumer projects:
ISO timestamps, date parsing, month labels, date ranges.

Usage::

    from pf_core.utils.dates import now_iso, parse_date, month_label

    ts = now_iso()                    # "2026-04-14T14:30:00Z"
    d = parse_date("2026-04-14")      # date(2026, 4, 14)
    label = month_label("2026-04")    # "April 2026"
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

from pf_core.exceptions import InvalidInputError

_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_YEAR_MONTH = re.compile(r"^(\d{4})-(\d{2})$")

# Accepted input forms for parse_timestamp, in priority order. All are
# interpreted as UTC — these helpers are UTC-only by design (consumers
# store and compare timestamps in UTC; a naive local time would be a bug).
_ISO_DATETIME_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d",
)


def now_iso() -> str:
    """Current UTC time as ISO 8601 string: ``YYYY-MM-DDTHH:MM:SSZ``."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_date(s: str | None) -> date:
    """Parse a ``YYYY-MM-DD`` string into a :class:`datetime.date`.

    Rejects day=00, month=00, and impossible calendar dates.

    Args:
        s: Date string in ISO format.

    Returns:
        A ``date`` object.

    Raises:
        InvalidInputError: If the string is ``None``, empty, or not a valid
            calendar date.
    """
    if s is None or not isinstance(s, str):
        raise InvalidInputError("Date is required")
    raw = s.strip()
    if not raw:
        raise InvalidInputError("Date is required")
    m = _ISO_DATE.match(raw)
    if not m:
        raise InvalidInputError(f"Invalid date format (expected YYYY-MM-DD): {raw!r}")
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return date(y, mo, d)
    except ValueError as e:
        raise InvalidInputError(f"Invalid calendar date: {raw!r}") from e


def try_parse_date(s: str | None) -> date | None:
    """Like :func:`parse_date` but returns ``None`` instead of raising.

    Useful for best-effort parsing where invalid dates should be silently skipped.
    """
    try:
        return parse_date(s)
    except InvalidInputError:
        return None


def month_label(ym: str) -> str:
    """Convert ``YYYY-MM`` to a human-readable label like ``"April 2026"``.

    Args:
        ym: Year-month string (e.g. ``"2026-04"``).

    Returns:
        Formatted month label, or the original string if parsing fails.
    """
    m = _YEAR_MONTH.match((ym or "").strip())
    if not m:
        return ym or ""
    y, mo = int(m.group(1)), int(m.group(2))
    try:
        return date(y, mo, 1).strftime("%B %Y")
    except ValueError:
        return ym


def date_range(start: date, end: date) -> list[date]:
    """Return a list of dates from *start* to *end*, inclusive.

    Args:
        start: First date in the range.
        end: Last date in the range (inclusive).

    Returns:
        List of ``date`` objects. Empty if ``start > end``.
    """
    if start > end:
        return []
    days = (end - start).days + 1
    return [start + timedelta(days=i) for i in range(days)]


def month_range(start: str, end: str) -> list[str]:
    """Return a list of ``YYYY-MM`` strings from *start* to *end*, inclusive.

    Args:
        start: First month (e.g. ``"2026-01"``).
        end: Last month (e.g. ``"2026-04"``).

    Returns:
        List of ``YYYY-MM`` strings. Empty if ``start > end``.
    """
    ms = _YEAR_MONTH.match((start or "").strip())
    me = _YEAR_MONTH.match((end or "").strip())
    if not ms or not me:
        return []
    sy, sm = int(ms.group(1)), int(ms.group(2))
    ey, em = int(me.group(1)), int(me.group(2))
    result: list[str] = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        result.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return result


def parse_timestamp(s: str | None) -> datetime:
    """Parse an ISO-ish timestamp into a timezone-aware UTC ``datetime``.

    Complements :func:`parse_date` (which is date-only): this accepts a
    time component. Tries, in order, ``YYYY-MM-DDTHH:MM:SS`` (optional
    trailing ``Z``), ``YYYY-MM-DDTHH:MM``, and bare ``YYYY-MM-DD``
    (midnight). Every result is stamped ``timezone.utc`` — these helpers
    are UTC-only by design.

    Args:
        s: Timestamp string in one of the accepted ISO forms.

    Returns:
        A timezone-aware ``datetime`` in UTC.

    Raises:
        InvalidInputError: If the string is ``None``, empty, or matches
            none of the accepted forms.
    """
    if s is None or not isinstance(s, str):
        raise InvalidInputError("Timestamp is required")
    raw = s.strip()
    if not raw:
        raise InvalidInputError("Timestamp is required")
    for fmt in _ISO_DATETIME_FORMATS:
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise InvalidInputError(f"Invalid timestamp (expected ISO 8601): {raw!r}")


def to_iso(dt: datetime) -> str:
    """Format a ``datetime`` as ``YYYY-MM-DDTHH:MM:SSZ`` (UTC).

    The inverse of :func:`parse_timestamp` and the arbitrary-datetime
    counterpart to :func:`now_iso`. A timezone-aware input is converted
    to UTC first; a naive input is assumed to already be UTC.
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
