"""Deterministic resolution of relative-date phrases against a reference date.

LLM agents are unreliable at calendar arithmetic. When source text says
"the release shipped on Tuesday" the model often guesses wrong
which Tuesday. This module moves the math out of the LLM and into Python:
the LLM emits a ``(phrase, qualifier)`` pair quoting what the text said,
and :func:`resolve_relative_date` resolves it against the publication date.

The output is a concrete ``datetime.date`` (or ``None`` when the phrase is
too imprecise for a safe guess — e.g. "earlier this week", "last month",
bare month names). ``None`` means "don't know"; callers should treat
unresolved dates as missing rather than guessing.

Usage::

    from datetime import date
    from pf_core.utils.relative_dates import resolve_relative_date

    pub = date(2026, 4, 15)  # Wednesday
    resolve_relative_date(pub, {"phrase": "yesterday"})           # 2026-04-14
    resolve_relative_date(pub, {"phrase": "monday"})              # 2026-04-13
    resolve_relative_date(pub, {"phrase": "monday", "qualifier": "last"})  # 2026-04-06
    resolve_relative_date(pub, {"phrase": "march 12"})            # 2026-03-12
    resolve_relative_date(pub, {"phrase": "december 10"})         # 2025-12-10  (year wrap)
    resolve_relative_date(pub, {"phrase": "this week"})           # None  (unresolvable)

Design rules:
  - "Strictly before pub_date" for bare weekdays — when an article
    publishes on a Tuesday and writes "on Tuesday X happened," it means
    last Tuesday (a week ago), not the publish day itself. If the writer
    meant today they'd write "today."
  - Bare month names ("February") are unresolvable — too imprecise.
  - Future dates are unresolvable — a dated source cannot describe events
    that haven't happened.
  - Year inference for "Month Day" without a year: prefer pub_date's
    year unless that would put the date in the future, then fall back
    to prior year.

The function never raises on malformed input — callers use ``None`` as
the "don't know" signal.

Generalized from production use where an extractor LLM emits these
phrases verbatim from source prose.
The pattern generalizes to any tool that reads dated articles and asks
an LLM to identify event dates.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import TypedDict


class DateHint(TypedDict, total=False):
    """Shape emitted by the LLM when it encounters a relative date phrase.

    ``phrase``    — verbatim text the model copied from the article,
                    lowercased by convention (e.g. "tuesday",
                    "yesterday", "march 12"). Required.
    ``qualifier`` — modifier the model identified around the phrase, one
                    of ``"last"``, ``"this"``, ``"next"``, ``"earlier"``,
                    or ``None``. Optional.
    """
    phrase: str
    qualifier: str | None


# Weekday names → Python weekday() integer (Mon=0).
_WEEKDAYS: dict[str, int] = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

# Matches "march 12", "march 12, 2025", "march 12 2025" (lowercase input).
_MONTH_DAY_RE = re.compile(
    r"^(?P<month>january|february|march|april|may|june|july|august|"
    r"september|october|november|december)\s+(?P<day>\d{1,2})"
    r"(?:,?\s+(?P<year>\d{4}))?$"
)

_MONTHS: dict[str, int] = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

# Phrases that map directly to a single-day offset from pub_date.
_ABSOLUTE_OFFSETS: dict[str, int] = {
    "today": 0,
    "this morning": 0,
    "this afternoon": 0,
    "this evening": 0,
    "tonight": 0,
    "yesterday": -1,
    "last night": -1,
    "this past night": -1,
    "tomorrow": 1,
}

# Phrases we deliberately refuse to resolve — callers get ``None`` and
# must either recover the date elsewhere or flag the input. Better to
# miss than to guess.
_UNRESOLVABLE_PHRASES: frozenset[str] = frozenset({
    "this week",
    "last week",
    "next week",
    "earlier this week",
    "later this week",
    "this month",
    "last month",
    "next month",
    "this year",
    "last year",
    "recently",
    "in recent days",
    "in recent weeks",
})


def _normalize(text: str) -> str:
    """Lowercase, strip, collapse internal whitespace."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _most_recent_weekday(pub_date: date, target_weekday: int) -> date:
    """Most recent occurrence of ``target_weekday`` strictly before ``pub_date``.

    "Strictly before" — when an article publishes on a Tuesday and writes
    "on Tuesday X happened," it means last Tuesday (a week ago), not the
    publish day. If the writer meant today they'd write "today."
    """
    diff = (pub_date.weekday() - target_weekday) % 7
    if diff == 0:
        diff = 7  # step back a full week rather than return pub_date
    return pub_date - timedelta(days=diff)


def _resolve_month_day(
    month: int, day: int, year: int | None, pub_date: date
) -> date | None:
    """Build a date from explicit month + day, inferring year from ``pub_date``.

    When the year is given, honor it. When not, prefer the same year as
    ``pub_date`` unless that would be in the future — in which case fall
    back to the prior year. Rejects dates strictly after ``pub_date`` in
    the same year (an article can't describe the future).
    """
    if year is not None:
        try:
            d = date(year, month, day)
        except ValueError:
            return None
        return d if d <= pub_date else None

    # No year → try pub_date's year first.
    try:
        d = date(pub_date.year, month, day)
    except ValueError:
        d = None
    if d is not None and d <= pub_date:
        return d
    # Fall back to previous year.
    try:
        d_prev = date(pub_date.year - 1, month, day)
    except ValueError:
        return None
    return d_prev if d_prev <= pub_date else None


def resolve_relative_date(
    pub_date: date,
    hint: DateHint | dict | None,
) -> date | None:
    """Resolve a relative-date phrase against a publication date.

    Args:
        pub_date: the article's publication date — the reference point
            for "today", "yesterday", weekday names, etc.
        hint: a :class:`DateHint` dict with ``phrase`` (required) and
            optional ``qualifier`` ("last" / "this" / "earlier" /
            "next" / None). Pass ``None`` or an empty dict to get
            ``None`` back.

    Returns:
        ``datetime.date`` on a confident resolution; ``None`` when the
        phrase is empty, unresolvable, or would require guessing beyond
        what the text supports.

    The function never raises on malformed input — callers use ``None``
    as the "don't know" signal.
    """
    if not hint:
        return None
    phrase = _normalize(hint.get("phrase", "") or "")
    qualifier = _normalize(hint.get("qualifier", "") or "") or None
    if not phrase:
        return None

    # 1. Explicit unresolvable — "last week", "recently", etc.
    if phrase in _UNRESOLVABLE_PHRASES:
        return None
    # A qualifier like "last" or "earlier" applied to a vague phrase
    # ("last week", "earlier this week") is also unresolvable.
    if qualifier and f"{qualifier} {phrase}" in _UNRESOLVABLE_PHRASES:
        return None

    # 2. Absolute single-day offsets ("today", "yesterday", "tonight").
    if phrase in _ABSOLUTE_OFFSETS:
        return pub_date + timedelta(days=_ABSOLUTE_OFFSETS[phrase])

    # 3. Weekday names ("tuesday", with optional "last" / "this" qualifier).
    if phrase in _WEEKDAYS:
        target = _WEEKDAYS[phrase]
        base = _most_recent_weekday(pub_date, target)
        if qualifier == "last":
            # "last Tuesday" = the Tuesday before the most recent one.
            return base - timedelta(days=7)
        # "this Tuesday" and bare "Tuesday" both resolve to the most recent.
        if qualifier in (None, "", "this"):
            return base
        # "next Tuesday" is in the future from pub_date — unresolvable
        # since the article cannot describe events that haven't happened.
        if qualifier == "next":
            return None
        return base  # unknown qualifier — fall through to the base resolution

    # 4. Explicit "Month Day" or "Month Day, Year".
    m = _MONTH_DAY_RE.match(phrase)
    if m:
        month = _MONTHS.get(m.group("month"))
        day = int(m.group("day"))
        year_raw = m.group("year")
        year = int(year_raw) if year_raw else None
        if month is None:
            return None
        return _resolve_month_day(month, day, year, pub_date)

    # 5. Bare month name with no day — too imprecise.
    if phrase in _MONTHS:
        return None

    # Unrecognized phrase — caller treats as None.
    return None


__all__ = ["DateHint", "resolve_relative_date"]
