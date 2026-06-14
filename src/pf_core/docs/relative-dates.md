# `pf_core.utils.relative_dates`

Resolve relative-date phrases ("yesterday", "Tuesday", "March 12") into concrete `datetime.date` values, given a publication date as the reference point.

## Why this exists

LLMs are unreliable at calendar arithmetic. When an article published on Wednesday 2026-04-15 says "the release shipped on Tuesday," asking the LLM to emit `2026-04-14` directly produces wrong dates a meaningful fraction of the time — wrong year, wrong week, sometimes wrong day-of-week.

The reliable pattern is to split the work:

1. **The LLM emits** a `(phrase, qualifier)` pair quoting verbatim what the article said: `{"phrase": "tuesday", "qualifier": null}`.
2. **Python resolves** the pair against the publication date with deterministic arithmetic.

This module is the Python half. It pairs with a prompt instruction along the lines of: *"Emit the time phrase the article uses verbatim (lowercased is fine). Never compute a date yourself. Python resolves it."*

## Usage

```python
from datetime import date
from pf_core.utils.relative_dates import resolve_relative_date

pub = date(2026, 4, 15)  # Wednesday

resolve_relative_date(pub, {"phrase": "yesterday"})
# date(2026, 4, 14)

resolve_relative_date(pub, {"phrase": "monday"})
# date(2026, 4, 13)  — most recent past Monday

resolve_relative_date(pub, {"phrase": "monday", "qualifier": "last"})
# date(2026, 4, 6)   — week before that

resolve_relative_date(pub, {"phrase": "march 12"})
# date(2026, 3, 12)

resolve_relative_date(pub, {"phrase": "december 10"})
# date(2025, 12, 10) — same year would be in the future, so prior year

resolve_relative_date(pub, {"phrase": "this week"})
# None  — too imprecise to resolve safely
```

## Resolution rules

### Absolute single-day offsets

| Phrase | Result |
|---|---|
| `today`, `this morning`, `this afternoon`, `this evening`, `tonight` | `pub_date` |
| `yesterday`, `last night`, `this past night` | `pub_date - 1 day` |
| `tomorrow` | `pub_date + 1 day` |

### Weekday names

Bare weekday → most recent occurrence **strictly before** `pub_date`. If `pub_date` itself falls on the named weekday, step back a full week.

> Why "strictly before"? When an article publishes on a Tuesday and writes
> "on Tuesday X happened," it means *last* Tuesday. If the writer meant
> today they'd write "today." Resolving to `pub_date` itself would
> systematically misattribute publish-day events to "last week's" Tuesday.

| Qualifier | Behavior |
|---|---|
| `None`, `""`, `"this"` | Most recent past weekday |
| `"last"` | Week before the most recent past weekday |
| `"next"` | Returns `None` (article cannot describe the future) |
| Unknown qualifier | Falls back to the no-qualifier behavior |

### Explicit Month + Day

Patterns supported:
- `march 12`
- `march 12, 2025`
- `march 12 2025`

Year inference (when no year is given):
1. Try `pub_date.year` first.
2. If that would put the date in the future, fall back to `pub_date.year - 1`.
3. Both rejected → return `None`.

Explicit future years are always rejected.

### Unresolvable phrases

These return `None` deliberately — too imprecise to guess safely:
- `this week`, `last week`, `next week`, `earlier this week`, `later this week`
- `this month`, `last month`, `next month`
- `this year`, `last year`
- `recently`, `in recent days`, `in recent weeks`
- Bare month names (`february`, `march`, etc.)

The same applies when the qualifier joined to the phrase forms an unresolvable phrase: `{"phrase": "week", "qualifier": "last"}` → `None`.

## Invariant

Every successful resolution satisfies `result <= pub_date`. An article cannot describe events that haven't happened. The test suite enforces this across every supported phrase form.

## Error handling

The function never raises on malformed input. `None` is the universal "don't know" signal:

```python
resolve_relative_date(pub, None)                        # None
resolve_relative_date(pub, {})                          # None
resolve_relative_date(pub, {"phrase": ""})              # None
resolve_relative_date(pub, {"phrase": "the other day"}) # None
resolve_relative_date(pub, {"phrase": "12"})            # None
resolve_relative_date(pub, {"phrase": "february 30"})   # None  (invalid date)
```

Callers should branch on `result is None` and either fall back to another date source (e.g. the article's own metadata) or flag the record for human review. Returning a guessed date when the phrase is genuinely ambiguous is worse than returning nothing.

## DateHint shape

```python
class DateHint(TypedDict, total=False):
    phrase: str               # required — verbatim from the article
    qualifier: str | None     # optional — "last" / "this" / "earlier" / "next" / None
```

Plain dicts work too — the function only reads `.get("phrase")` and `.get("qualifier")`.

## Case and whitespace

Phrases are lowercased and whitespace-collapsed before matching, so `"YESTERDAY"`, `"Tuesday"`, and `"March   12"` all work.

## See also

- `pf_core.utils.dates` — ISO parsing, month labels, date ranges (orthogonal)
- `pf_core/docs/anti-hallucination.md` — broader pattern of constraining LLM input rather than detecting bad LLM output
