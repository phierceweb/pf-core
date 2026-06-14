# Date Utilities

Date and time helpers consolidated from consumer projects. Provides ISO timestamps, date and timestamp parsing, month labels, and range generation.

For resolving relative-date phrases (`yesterday`, `Tuesday`, `March 12`) emitted by an LLM reading dated articles, see [relative-dates.md](relative-dates.md).

## Functions

### now_iso

Current UTC time as ISO 8601 string:

```python
from pf_core.utils.dates import now_iso

now_iso()  # "2026-04-14T14:30:00Z"
```

Also re-exported from `pf_core.db.helpers` for backward compatibility.

### parse_date

Parse a `YYYY-MM-DD` string into a `date` object. Raises `InvalidInputError` on bad input:

```python
from pf_core.utils.dates import parse_date

d = parse_date("2026-04-14")       # date(2026, 4, 14)
d = parse_date("  2026-04-14  ")   # whitespace stripped
parse_date("04-14-2026")           # InvalidInputError: Invalid date format
parse_date("2026-02-30")           # InvalidInputError: Invalid calendar date
parse_date(None)                   # InvalidInputError: Date is required
```

Rejects month=00, day=00, impossible calendar dates (e.g. Feb 30), and non-leap-year Feb 29.

### try_parse_date

Like `parse_date` but returns `None` instead of raising:

```python
from pf_core.utils.dates import try_parse_date

try_parse_date("2026-04-14")   # date(2026, 4, 14)
try_parse_date("nope")         # None
try_parse_date(None)           # None
```

Useful for best-effort parsing where invalid dates should be silently skipped.

### parse_timestamp

The date-*time* counterpart to `parse_date`. Parses an ISO-ish timestamp into a timezone-aware UTC `datetime`. Raises `InvalidInputError` on bad input:

```python
from pf_core.utils.dates import parse_timestamp

parse_timestamp("2026-04-14T09:30:00Z")   # datetime(2026,4,14,9,30, tzinfo=utc)
parse_timestamp("2026-04-14T09:30:00")    # same — trailing Z optional
parse_timestamp("2026-04-14T09:30")       # minute precision
parse_timestamp("2026-04-14")             # midnight UTC
parse_timestamp("14-04-2026 09:30")       # InvalidInputError: Invalid timestamp
parse_timestamp(None)                     # InvalidInputError: Timestamp is required
```

UTC-only by design — every result is stamped `timezone.utc`. A naive local time would be a bug in a system that stores and compares timestamps in UTC.

### to_iso

The inverse of `parse_timestamp`, and the arbitrary-`datetime` counterpart to `now_iso`. Formats a `datetime` as `YYYY-MM-DDTHH:MM:SSZ`:

```python
from datetime import datetime, timezone
from pf_core.utils.dates import to_iso

to_iso(datetime(2026, 4, 14, 9, 30, tzinfo=timezone.utc))  # "2026-04-14T09:30:00Z"
to_iso(datetime(2026, 4, 14, 9, 30))                       # naive → assumed UTC
# tz-aware non-UTC input is converted to UTC first
```

`to_iso(parse_timestamp(s)) == s` for any `s` in full-second ISO-Z form.

### month_label

Convert `YYYY-MM` to a human-readable label:

```python
from pf_core.utils.dates import month_label

month_label("2026-04")   # "April 2026"
month_label("2025-01")   # "January 2025"
month_label("bad")       # "bad" (returns input on failure)
month_label("")          # ""
```

### date_range

List of dates from start to end, inclusive:

```python
from datetime import date
from pf_core.utils.dates import date_range

date_range(date(2026, 4, 1), date(2026, 4, 3))
# [date(2026, 4, 1), date(2026, 4, 2), date(2026, 4, 3)]

date_range(date(2026, 4, 5), date(2026, 4, 1))
# [] (start > end)
```

### month_range

List of `YYYY-MM` strings from start to end, inclusive:

```python
from pf_core.utils.dates import month_range

month_range("2025-11", "2026-02")
# ["2025-11", "2025-12", "2026-01", "2026-02"]

month_range("2026-04", "2026-04")
# ["2026-04"]
```

## Import paths

| Function | Preferred import | Backward-compat import |
|----------|-----------------|----------------------|
| `now_iso` | `pf_core.utils.dates` | `pf_core.db.helpers` / `pf_core.db` |
| All others | `pf_core.utils.dates` | — |
