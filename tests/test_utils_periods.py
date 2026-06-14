"""Tests for pf_core.utils.periods — preset → Period resolver."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from pf_core.utils.periods import (
    Period,
    ROLLING_PRESETS,
    days_in_period,
    parse_anchor_arg,
    parse_period_arg,
    resolve,
)


def _utc(year, month, day, hour=0, minute=0, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


class TestPeriodDataclass:
    def test_period_is_frozen_and_carries_three_fields(self):
        p = Period(start=_utc(2026, 5, 12), end=_utc(2026, 5, 13), label="day:2026-05-12")
        assert p.start == _utc(2026, 5, 12)
        assert p.end == _utc(2026, 5, 13)
        assert p.label == "day:2026-05-12"
        with pytest.raises(FrozenInstanceError):
            p.label = "mutated"  # frozen dataclass

    def test_two_periods_with_same_values_compare_equal(self):
        a = Period(start=_utc(2026, 5, 12), end=_utc(2026, 5, 13), label="x")
        b = Period(start=_utc(2026, 5, 12), end=_utc(2026, 5, 13), label="x")
        assert a == b


class TestYesterday:
    def test_returns_previous_utc_calendar_day(self):
        now = _utc(2026, 5, 13, 9, 30)
        p = resolve("yesterday", now=now)
        assert p.start == _utc(2026, 5, 12)
        assert p.end == _utc(2026, 5, 13)
        assert p.label == "day:2026-05-12"

    def test_uses_utc_regardless_of_now_offset(self):
        # 2026-05-13 03:00 UTC-5 == 2026-05-13 08:00 UTC, so "yesterday"
        # in UTC is 2026-05-12.
        from datetime import timedelta
        now = datetime(2026, 5, 13, 3, 0, tzinfo=timezone(timedelta(hours=-5)))
        p = resolve("yesterday", now=now)
        assert p.label == "day:2026-05-12"


class TestDayPreset:
    def test_specific_day_canonical_form(self):
        p = resolve("day:2026-05-10", now=_utc(2026, 5, 13))
        assert p.start == _utc(2026, 5, 10)
        assert p.end == _utc(2026, 5, 11)
        assert p.label == "day:2026-05-10"

    def test_day_preset_ignores_now(self):
        p1 = resolve("day:2026-01-01", now=_utc(2026, 5, 13))
        p2 = resolve("day:2026-01-01", now=_utc(2030, 1, 1))
        assert p1 == p2


class TestRollingPresets:
    def test_last_24h_no_anchor_ends_at_now(self):
        now = _utc(2026, 5, 13, 9, 30)
        p = resolve("last_24h", now=now)
        assert p.end == now
        assert p.start == _utc(2026, 5, 12, 9, 30)
        assert p.label == "last_24h"

    @pytest.mark.parametrize("preset,days", [
        ("last_3d", 3),
        ("last_7d", 7),
        ("last_14d", 14),
        ("last_30d", 30),
        ("last_90d", 90),
    ])
    def test_rolling_n_days_window(self, preset, days):
        from datetime import timedelta
        now = _utc(2026, 5, 13)
        p = resolve(preset, now=now)
        assert p.end == now
        assert p.end - p.start == timedelta(days=days)
        assert p.label == preset

    def test_unknown_preset_raises(self):
        with pytest.raises(ValueError, match="Unknown period preset"):
            resolve("last_5d", now=_utc(2026, 5, 13))


class TestRollingPresetsRegistry:
    def test_registry_includes_all_six_rolling_presets(self):
        assert set(ROLLING_PRESETS) == {
            "last_24h", "last_3d", "last_7d",
            "last_14d", "last_30d", "last_90d",
        }


class TestAnchor:
    def test_anchor_shifts_window_end(self):
        anchor = _utc(2026, 4, 1)
        p = resolve("last_7d", anchor=anchor, now=_utc(2026, 5, 13))
        assert p.end == anchor
        from datetime import timedelta
        assert p.end - p.start == timedelta(days=7)

    def test_anchor_at_midnight_utc_becomes_date_label(self):
        anchor = _utc(2026, 4, 1)
        p = resolve("last_7d", anchor=anchor, now=_utc(2026, 5, 13))
        assert p.label == "last_7d:2026-04-01"

    def test_anchor_with_time_becomes_iso_label(self):
        anchor = _utc(2026, 4, 1, 14, 30)
        p = resolve("last_7d", anchor=anchor, now=_utc(2026, 5, 13))
        assert p.label == "last_7d:2026-04-01T14:30:00Z"

    def test_anchor_ignored_for_day_preset(self):
        p = resolve("day:2026-04-01", anchor=_utc(2025, 1, 1), now=_utc(2026, 5, 13))
        assert p.label == "day:2026-04-01"
        assert p.start == _utc(2026, 4, 1)


class TestCustomRange:
    def test_custom_start_and_end(self):
        p = resolve(
            preset=None,
            custom_start=_utc(2026, 5, 10, 12),
            custom_end=_utc(2026, 5, 11, 12),
            now=_utc(2026, 5, 13),
        )
        assert p.start == _utc(2026, 5, 10, 12)
        assert p.end == _utc(2026, 5, 11, 12)
        assert p.label == "custom"

    def test_custom_start_only_defaults_end_to_now(self):
        now = _utc(2026, 5, 13)
        p = resolve(preset=None, custom_start=_utc(2026, 5, 10), now=now)
        assert p.end == now
        assert p.start == _utc(2026, 5, 10)
        assert p.label == "custom"

    def test_no_preset_and_no_custom_raises(self):
        with pytest.raises(ValueError, match="Either preset or custom"):
            resolve(preset=None, now=_utc(2026, 5, 13))


class TestParsePeriodArg:
    def test_preset_name_passes_through(self):
        assert parse_period_arg("last_24h") == "last_24h"
        assert parse_period_arg("yesterday") == "yesterday"
        assert parse_period_arg("last_7d") == "last_7d"

    def test_bare_date_becomes_day_preset(self):
        assert parse_period_arg("2026-05-12") == "day:2026-05-12"

    def test_day_canonical_passes_through(self):
        assert parse_period_arg("day:2026-05-12") == "day:2026-05-12"

    def test_strips_whitespace(self):
        assert parse_period_arg("  yesterday  ") == "yesterday"

    def test_invalid_raises_value_error(self):
        with pytest.raises(ValueError, match="Unrecognized period"):
            parse_period_arg("bogus")

    def test_invalid_date_format_raises(self):
        with pytest.raises(ValueError, match="Unrecognized period"):
            parse_period_arg("2026-5-12")  # not zero-padded


class TestParseAnchorArg:
    def test_iso_timestamp_with_z(self):
        assert parse_anchor_arg("2026-05-12T09:00:00Z") == _utc(2026, 5, 12, 9)

    def test_iso_timestamp_without_z(self):
        assert parse_anchor_arg("2026-05-12T09:00:00") == _utc(2026, 5, 12, 9)

    def test_minute_precision(self):
        assert parse_anchor_arg("2026-05-12T09:30") == _utc(2026, 5, 12, 9, 30)

    def test_bare_date_becomes_midnight_utc(self):
        assert parse_anchor_arg("2026-05-12") == _utc(2026, 5, 12)

    def test_strips_whitespace(self):
        assert parse_anchor_arg("  2026-05-12  ") == _utc(2026, 5, 12)

    def test_invalid_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_anchor_arg("not-a-date")


class TestDaysInPeriod:
    def test_24h_returns_one(self):
        p = resolve("last_24h", now=_utc(2026, 5, 13))
        assert days_in_period(p) == 1.0

    def test_7d_returns_seven(self):
        p = resolve("last_7d", now=_utc(2026, 5, 13))
        assert days_in_period(p) == 7.0

    def test_fractional_for_partial_day(self):
        p = Period(
            start=_utc(2026, 5, 12, 12),
            end=_utc(2026, 5, 13),
            label="custom",
        )
        assert days_in_period(p) == 0.5


class TestPublicSurface:
    def test_all_names_exported(self):
        from pf_core.utils import periods as mod
        expected = {
            "Period", "ROLLING_PRESETS",
            "resolve",
            "parse_period_arg", "parse_anchor_arg",
            "days_in_period",
        }
        assert expected.issubset(set(mod.__all__))
        for name in expected:
            assert hasattr(mod, name)

    def test_private_helpers_not_in_all(self):
        from pf_core.utils import periods as mod
        for private in ("_yesterday", "_day", "_anchor_label",
                        "_DAY_PRESET_RE", "_DATE_ONLY_RE", "_ISO_FORMATS"):
            assert private not in mod.__all__
