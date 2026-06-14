"""Tests for pf_core.utils.dates."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone

import pytest

from pf_core.exceptions import InvalidInputError
from pf_core.utils.dates import (
    date_range,
    month_label,
    month_range,
    now_iso,
    parse_date,
    parse_timestamp,
    to_iso,
    try_parse_date,
)


class TestNowIso:
    def test_format(self):
        result = now_iso()
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", result)

    def test_backward_compat(self):
        """now_iso is still importable from pf_core.db.helpers."""
        from pf_core.db.helpers import now_iso as db_now_iso
        assert db_now_iso is now_iso


class TestParseDate:
    def test_valid(self):
        assert parse_date("2026-04-14") == date(2026, 4, 14)

    def test_with_whitespace(self):
        assert parse_date("  2026-04-14  ") == date(2026, 4, 14)

    def test_none_raises(self):
        with pytest.raises(InvalidInputError, match="required"):
            parse_date(None)

    def test_empty_raises(self):
        with pytest.raises(InvalidInputError, match="required"):
            parse_date("")

    def test_bad_format_raises(self):
        with pytest.raises(InvalidInputError, match="Invalid date format"):
            parse_date("04-14-2026")

    def test_impossible_date_raises(self):
        with pytest.raises(InvalidInputError, match="Invalid calendar date"):
            parse_date("2026-02-30")

    def test_month_zero_raises(self):
        with pytest.raises(InvalidInputError, match="Invalid calendar date"):
            parse_date("2026-00-15")

    def test_day_zero_raises(self):
        with pytest.raises(InvalidInputError, match="Invalid calendar date"):
            parse_date("2026-04-00")

    def test_month_13_raises(self):
        with pytest.raises(InvalidInputError, match="Invalid calendar date"):
            parse_date("2026-13-01")

    def test_leap_day_valid(self):
        assert parse_date("2024-02-29") == date(2024, 2, 29)

    def test_leap_day_invalid(self):
        with pytest.raises(InvalidInputError):
            parse_date("2025-02-29")


class TestTryParseDate:
    def test_valid(self):
        assert try_parse_date("2026-04-14") == date(2026, 4, 14)

    def test_invalid_returns_none(self):
        assert try_parse_date("nope") is None

    def test_none_returns_none(self):
        assert try_parse_date(None) is None


class TestMonthLabel:
    def test_normal(self):
        assert month_label("2026-04") == "April 2026"

    def test_january(self):
        assert month_label("2025-01") == "January 2025"

    def test_december(self):
        assert month_label("2025-12") == "December 2025"

    def test_invalid_returns_input(self):
        assert month_label("bad") == "bad"

    def test_none_returns_empty(self):
        assert month_label(None) == ""

    def test_empty_returns_empty(self):
        assert month_label("") == ""


class TestDateRange:
    def test_single_day(self):
        d = date(2026, 4, 14)
        assert date_range(d, d) == [d]

    def test_three_days(self):
        result = date_range(date(2026, 4, 1), date(2026, 4, 3))
        assert result == [date(2026, 4, 1), date(2026, 4, 2), date(2026, 4, 3)]

    def test_start_after_end(self):
        assert date_range(date(2026, 4, 5), date(2026, 4, 1)) == []

    def test_crosses_month(self):
        result = date_range(date(2026, 1, 30), date(2026, 2, 2))
        assert len(result) == 4
        assert result[0] == date(2026, 1, 30)
        assert result[-1] == date(2026, 2, 2)


class TestMonthRange:
    def test_single_month(self):
        assert month_range("2026-04", "2026-04") == ["2026-04"]

    def test_several_months(self):
        result = month_range("2026-01", "2026-04")
        assert result == ["2026-01", "2026-02", "2026-03", "2026-04"]

    def test_crosses_year(self):
        result = month_range("2025-11", "2026-02")
        assert result == ["2025-11", "2025-12", "2026-01", "2026-02"]

    def test_start_after_end(self):
        assert month_range("2026-05", "2026-01") == []

    def test_invalid_input(self):
        assert month_range("bad", "2026-01") == []


class TestParseTimestamp:
    def test_full_iso_with_z(self):
        assert parse_timestamp("2026-04-14T09:30:00Z") == datetime(
            2026, 4, 14, 9, 30, 0, tzinfo=timezone.utc
        )

    def test_full_iso_without_z(self):
        assert parse_timestamp("2026-04-14T09:30:00") == datetime(
            2026, 4, 14, 9, 30, 0, tzinfo=timezone.utc
        )

    def test_minute_precision(self):
        assert parse_timestamp("2026-04-14T09:30") == datetime(
            2026, 4, 14, 9, 30, tzinfo=timezone.utc
        )

    def test_bare_date_is_midnight_utc(self):
        assert parse_timestamp("2026-04-14") == datetime(
            2026, 4, 14, tzinfo=timezone.utc
        )

    def test_whitespace_stripped(self):
        assert parse_timestamp("  2026-04-14T09:30:00Z  ") == datetime(
            2026, 4, 14, 9, 30, 0, tzinfo=timezone.utc
        )

    def test_result_is_timezone_aware_utc(self):
        assert parse_timestamp("2026-04-14").tzinfo == timezone.utc

    def test_none_raises(self):
        with pytest.raises(InvalidInputError, match="required"):
            parse_timestamp(None)

    def test_empty_raises(self):
        with pytest.raises(InvalidInputError, match="required"):
            parse_timestamp("")

    def test_bad_format_raises(self):
        with pytest.raises(InvalidInputError, match="Invalid timestamp"):
            parse_timestamp("14-04-2026 09:30")


class TestToIso:
    def test_utc_aware_datetime(self):
        dt = datetime(2026, 4, 14, 9, 30, 0, tzinfo=timezone.utc)
        assert to_iso(dt) == "2026-04-14T09:30:00Z"

    def test_naive_datetime_assumed_utc(self):
        assert to_iso(datetime(2026, 4, 14, 9, 30, 0)) == "2026-04-14T09:30:00Z"

    def test_non_utc_offset_converted(self):
        from datetime import timedelta

        tz_plus2 = timezone(timedelta(hours=2))
        dt = datetime(2026, 4, 14, 11, 30, 0, tzinfo=tz_plus2)
        assert to_iso(dt) == "2026-04-14T09:30:00Z"

    def test_round_trips_with_parse_timestamp(self):
        s = "2026-04-14T09:30:00Z"
        assert to_iso(parse_timestamp(s)) == s
