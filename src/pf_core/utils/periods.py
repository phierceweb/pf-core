"""Period preset resolution for reporting-style CLIs.

Maps named presets and optional date anchors into concrete
`Period(start, end, label)` values. The `label` is the canonical
descriptor a caller stores on an output row so cohorts can be filtered
(e.g. every `day:*` row in a daily archive).

Preset taxonomy:

| Preset           | Meaning                                              | Label form           |
|------------------|------------------------------------------------------|----------------------|
| `yesterday`      | Most recent fully-elapsed UTC calendar day           | `day:YYYY-MM-DD`     |
| `day:YYYY-MM-DD` | That specific UTC calendar day, midnight-to-midnight | `day:YYYY-MM-DD`     |
| `last_24h`       | 24 hours ending at `anchor` (default `now`)          | `last_24h` (no anchor) / `last_24h:<iso>` (with anchor) |
| `last_3d`        | 3 days ending at `anchor`                            | (same pattern)       |
| `last_7d`        | 7 days ending at `anchor`                            | (same pattern)       |
| `last_14d`       | 14 days ending at `anchor`                           | (same pattern)       |
| `last_30d`       | 30 days ending at `anchor`                           | (same pattern)       |
| `last_90d`       | 90 days ending at `anchor`                           | (same pattern)       |
| `custom`         | Explicit `(start, end)` pair                         | `custom`             |
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


_DAY_PRESET_RE = re.compile(r"^day:(\d{4}-\d{2}-\d{2})$")
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d",
)


# Public registry of rolling presets and their lookback windows. The
# value is the `timedelta` subtracted from `anchor` (or `now`) to compute
# the period's `start`. Consumer projects may build their own whitelist
# subsets (e.g. "only `last_24h` and `last_7d` are allowed for stage 2").
ROLLING_PRESETS: dict[str, timedelta] = {
    "last_24h": timedelta(hours=24),
    "last_3d": timedelta(days=3),
    "last_7d": timedelta(days=7),
    "last_14d": timedelta(days=14),
    "last_30d": timedelta(days=30),
    "last_90d": timedelta(days=90),
}


@dataclass(frozen=True)
class Period:
    """A resolved analysis period.

    `start` and `end` are timezone-aware UTC datetimes (`end` is
    exclusive). `label` is the canonical preset descriptor — stable
    enough to be used as a cohort key on output rows.
    """
    start: datetime
    end: datetime
    label: str


def resolve(
    preset: str | None,
    *,
    anchor: datetime | None = None,
    now: datetime,
    custom_start: datetime | None = None,
    custom_end: datetime | None = None,
) -> Period:
    """Resolve a preset (or custom range) into a concrete `Period`.

    Exactly one of `preset` or `custom_start` must be supplied. When
    `custom_start` is given, returns a `Period` with `label="custom"`.

    `anchor` shifts rolling-preset windows to end at a past timestamp
    rather than `now`. Ignored for `day:*` and `yesterday` (which are
    fixed by their date string).
    """
    if custom_start is not None:
        end = custom_end if custom_end is not None else now
        return Period(start=custom_start, end=end, label="custom")

    if preset is None:
        raise ValueError("Either preset or custom_start must be provided")

    if preset == "yesterday":
        return _yesterday(now)

    day_match = _DAY_PRESET_RE.match(preset)
    if day_match:
        return _day(day_match.group(1))

    if preset in ROLLING_PRESETS:
        lookback = ROLLING_PRESETS[preset]
        end = anchor if anchor is not None else now
        start = end - lookback
        label = preset if anchor is None else f"{preset}:{_anchor_label(anchor)}"
        return Period(start=start, end=end, label=label)

    raise ValueError(f"Unknown period preset: {preset!r}")


def _yesterday(now: datetime) -> Period:
    today_utc = now.astimezone(timezone.utc).date()
    yesterday = today_utc - timedelta(days=1)
    return _day(yesterday.isoformat())


def _day(date_str: str) -> Period:
    day = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
    return Period(start=day, end=day + timedelta(days=1), label=f"day:{date_str}")


def parse_period_arg(raw: str) -> str:
    """Normalize a CLI period argument into a canonical preset string.

    Accepts:
      - A preset name (`last_24h`, `yesterday`, `last_7d`, ...)
      - A bare date `YYYY-MM-DD` (rewritten to `day:YYYY-MM-DD`)
      - The canonical `day:YYYY-MM-DD` form (passed through)

    Returns the normalized string ready to feed into `resolve()`.

    Raises:
        ValueError: anything that isn't one of the accepted forms.
    """
    s = raw.strip()
    if _DATE_ONLY_RE.match(s):
        return f"day:{s}"
    if s == "yesterday" or s in ROLLING_PRESETS:
        return s
    if _DAY_PRESET_RE.match(s):
        return s
    raise ValueError(f"Unrecognized period: {raw!r}")


def parse_anchor_arg(raw: str) -> datetime:
    """Parse a `--end` / anchor CLI argument into a UTC datetime.

    Accepts the same ISO-ish forms the period module produces in
    `label`s (so anchored labels round-trip):
      - `YYYY-MM-DD` → midnight UTC
      - `YYYY-MM-DDTHH:MM` → that minute, UTC
      - `YYYY-MM-DDTHH:MM:SS` (optional trailing `Z`) → that second, UTC

    Raises `ValueError` on anything that doesn't match.
    """
    s = raw.strip()
    for fmt in _ISO_FORMATS:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Could not parse anchor timestamp: {raw!r}")


def days_in_period(period: Period) -> float:
    """Length of `period` in days (fractional).

    Useful for hard caps — e.g. a CLI that rejects ranges spanning more
    than N days.
    """
    return (period.end - period.start).total_seconds() / 86400.0


def _anchor_label(anchor: datetime) -> str:
    """Compact anchor descriptor for the period label suffix.

    Returns the bare date (`YYYY-MM-DD`) when `anchor` is exactly
    midnight UTC; otherwise returns the full ISO timestamp
    (`YYYY-MM-DDTHH:MM:SSZ`). The shorter form keeps the common case
    (a backfilled daily run) readable in archives.
    """
    if (
        anchor.tzinfo is not None
        and anchor.utcoffset() == timedelta(0)
        and anchor.hour == 0
        and anchor.minute == 0
        and anchor.second == 0
        and anchor.microsecond == 0
    ):
        return anchor.date().isoformat()
    return anchor.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "Period",
    "ROLLING_PRESETS",
    "resolve",
    "parse_period_arg",
    "parse_anchor_arg",
    "days_in_period",
]
