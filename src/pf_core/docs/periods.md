# `pf_core.utils.periods`

Resolve named period presets (`yesterday`, `last_7d`, `day:2026-05-12`) into concrete `(start, end, label)` tuples for reporting-style CLIs.

## Why this exists

Reporting CLIs (`report.py`, `trends.py`, daily-stats jobs, …) re-implement the same handful of preset names. Centralizing the resolution here keeps the label format consistent across consumers — which matters because the label is the natural cohort key on output rows (`WHERE period_label = 'day:2026-05-12'`).

## Usage

```python
from datetime import datetime, timezone

from pf_core.utils.periods import (
    resolve, parse_period_arg, parse_anchor_arg, days_in_period,
)

now = datetime.now(timezone.utc)

period = resolve("yesterday", now=now)
# Period(start=2026-05-12 00:00Z, end=2026-05-13 00:00Z, label='day:2026-05-12')

period = resolve("last_7d", now=now)
# Period(start=now-7d, end=now, label='last_7d')

period = resolve("day:2026-05-10", now=now)
# Period(start=2026-05-10 00:00Z, end=2026-05-11 00:00Z, label='day:2026-05-10')

# Anchored — shift the rolling window's end back in time.
anchor = parse_anchor_arg("2026-04-01")
period = resolve("last_7d", anchor=anchor, now=now)
# Period(start=2026-03-25, end=2026-04-01, label='last_7d:2026-04-01')
```

## CLI integration

```python
import argparse
from datetime import datetime, timezone
from pf_core.utils.periods import parse_period_arg, parse_anchor_arg, resolve

p = argparse.ArgumentParser()
p.add_argument("--period", default="yesterday")
p.add_argument("--end", help="Anchor for rolling presets")
args = p.parse_args()

preset = parse_period_arg(args.period)
anchor = parse_anchor_arg(args.end) if args.end else None
period = resolve(preset, anchor=anchor, now=datetime.now(timezone.utc))
```

## Project-specific whitelists

Each consumer typically restricts which presets are valid for which CLI (e.g. report = short ranges only; trends = multi-day only). Build a small whitelist alongside your CLI:

```python
from pf_core.utils.periods import parse_period_arg, resolve, days_in_period

REPORT_PRESETS = ("yesterday", "last_24h", "last_3d", "last_7d")
REPORT_MAX_DAYS = 7

preset = parse_period_arg(args.period)
if preset.startswith("last_") and preset not in REPORT_PRESETS:
    raise SystemExit(f"--period {preset!r} not allowed for this command")

period = resolve(preset, anchor=anchor, now=now)
if days_in_period(period) > REPORT_MAX_DAYS:
    raise SystemExit(f"--period spans more than {REPORT_MAX_DAYS} days")
```

The framework owns the **vocabulary** (`ROLLING_PRESETS`, `day:*`, `yesterday`, `custom`); the project owns the **policy** (which subset is legal, what the cap is). Keep policy code in the project.

## Label format

| Preset                          | Label                              |
|---------------------------------|------------------------------------|
| `yesterday`                     | `day:YYYY-MM-DD` (resolved date)   |
| `day:YYYY-MM-DD`                | `day:YYYY-MM-DD`                   |
| `last_Nd` (no anchor)           | `last_Nd`                          |
| `last_Nd` + midnight-UTC anchor | `last_Nd:YYYY-MM-DD`               |
| `last_Nd` + non-midnight anchor | `last_Nd:YYYY-MM-DDTHH:MM:SSZ`     |
| `custom`                        | `custom`                           |

Anchored labels round-trip: feed the suffix back through `parse_anchor_arg` to recover the anchor.
