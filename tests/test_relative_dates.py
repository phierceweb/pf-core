"""Tests for pf_core.utils.relative_dates — deterministic date resolution."""

from __future__ import annotations

from datetime import date

import pytest

from pf_core.utils.relative_dates import resolve_relative_date


# A Wednesday for reference-date tests.
WED_2026_04_15 = date(2026, 4, 15)
# A Tuesday for weekday-matches-pub-date tests.
TUE_2026_04_14 = date(2026, 4, 14)


class TestEmptyInput:
    def test_empty_hint_returns_none(self):
        assert resolve_relative_date(WED_2026_04_15, {}) is None

    def test_empty_phrase_returns_none(self):
        assert resolve_relative_date(WED_2026_04_15, {"phrase": ""}) is None

    def test_none_phrase_returns_none(self):
        assert resolve_relative_date(
            WED_2026_04_15, {"phrase": None}  # type: ignore[typeddict-item]
        ) is None

    def test_none_hint_returns_none(self):
        assert resolve_relative_date(WED_2026_04_15, None) is None


class TestAbsoluteOffsets:
    def test_today(self):
        assert resolve_relative_date(WED_2026_04_15, {"phrase": "today"}) == WED_2026_04_15

    def test_today_case_insensitive(self):
        assert resolve_relative_date(WED_2026_04_15, {"phrase": "Today"}) == WED_2026_04_15

    def test_this_morning(self):
        assert resolve_relative_date(WED_2026_04_15, {"phrase": "this morning"}) == WED_2026_04_15

    def test_tonight(self):
        assert resolve_relative_date(WED_2026_04_15, {"phrase": "tonight"}) == WED_2026_04_15

    def test_yesterday(self):
        assert resolve_relative_date(WED_2026_04_15, {"phrase": "yesterday"}) == date(2026, 4, 14)

    def test_last_night(self):
        assert resolve_relative_date(WED_2026_04_15, {"phrase": "last night"}) == date(2026, 4, 14)

    def test_tomorrow(self):
        assert resolve_relative_date(WED_2026_04_15, {"phrase": "tomorrow"}) == date(2026, 4, 16)

    def test_whitespace_around_phrase(self):
        assert resolve_relative_date(
            WED_2026_04_15, {"phrase": "  yesterday  "}
        ) == date(2026, 4, 14)


class TestWeekdayNames:
    def test_bare_weekday_returns_most_recent_past(self):
        assert resolve_relative_date(
            WED_2026_04_15, {"phrase": "monday"}
        ) == date(2026, 4, 13)

    def test_bare_weekday_later_in_week(self):
        assert resolve_relative_date(
            WED_2026_04_15, {"phrase": "tuesday"}
        ) == date(2026, 4, 14)

    def test_weekday_matches_pub_date_goes_back_a_week(self):
        assert resolve_relative_date(
            TUE_2026_04_14, {"phrase": "tuesday"}
        ) == date(2026, 4, 7)

    def test_weekday_earlier_in_same_week(self):
        assert resolve_relative_date(
            WED_2026_04_15, {"phrase": "sunday"}
        ) == date(2026, 4, 12)

    def test_this_weekday_equivalent_to_bare(self):
        assert resolve_relative_date(
            WED_2026_04_15, {"phrase": "monday", "qualifier": "this"}
        ) == date(2026, 4, 13)

    def test_last_weekday_steps_back_a_week(self):
        assert resolve_relative_date(
            WED_2026_04_15, {"phrase": "monday", "qualifier": "last"}
        ) == date(2026, 4, 6)

    def test_last_weekday_same_day_pub(self):
        assert resolve_relative_date(
            TUE_2026_04_14, {"phrase": "tuesday", "qualifier": "last"}
        ) == date(2026, 3, 31)

    def test_next_weekday_is_unresolvable(self):
        assert resolve_relative_date(
            WED_2026_04_15, {"phrase": "monday", "qualifier": "next"}
        ) is None

    def test_weekday_year_wraparound(self):
        assert resolve_relative_date(
            date(2026, 1, 2), {"phrase": "monday"}
        ) == date(2025, 12, 29)


class TestExplicitMonthDay:
    def test_month_day_same_year(self):
        assert resolve_relative_date(
            WED_2026_04_15, {"phrase": "march 12"}
        ) == date(2026, 3, 12)

    def test_month_day_with_explicit_year(self):
        assert resolve_relative_date(
            WED_2026_04_15, {"phrase": "march 12, 2025"}
        ) == date(2025, 3, 12)

    def test_month_day_year_no_comma(self):
        assert resolve_relative_date(
            WED_2026_04_15, {"phrase": "march 12 2025"}
        ) == date(2025, 3, 12)

    def test_month_day_in_future_same_year_falls_back_prior_year(self):
        assert resolve_relative_date(
            WED_2026_04_15, {"phrase": "december 10"}
        ) == date(2025, 12, 10)

    def test_month_day_explicit_future_year_rejected(self):
        assert resolve_relative_date(
            WED_2026_04_15, {"phrase": "december 10, 2027"}
        ) is None

    def test_invalid_calendar_date_returns_none(self):
        assert resolve_relative_date(
            WED_2026_04_15, {"phrase": "february 30"}
        ) is None

    def test_leap_year_feb_29_valid(self):
        assert resolve_relative_date(
            date(2024, 6, 1), {"phrase": "february 29"}
        ) == date(2024, 2, 29)

    def test_non_leap_year_feb_29_falls_back_to_last_leap(self):
        assert resolve_relative_date(
            date(2025, 6, 1), {"phrase": "february 29"}
        ) == date(2024, 2, 29)


class TestUnresolvablePhrases:
    @pytest.mark.parametrize("phrase", [
        "this week", "last week", "earlier this week", "later this week",
        "this month", "last month", "this year", "last year",
        "recently", "in recent days",
    ])
    def test_vague_phrase_returns_none(self, phrase):
        assert resolve_relative_date(WED_2026_04_15, {"phrase": phrase}) is None

    def test_qualifier_plus_week_phrase(self):
        assert resolve_relative_date(
            WED_2026_04_15, {"phrase": "week", "qualifier": "last"}
        ) is None

    def test_bare_month_name_returns_none(self):
        assert resolve_relative_date(WED_2026_04_15, {"phrase": "february"}) is None


class TestUnknownInput:
    def test_garbage_phrase_returns_none(self):
        assert resolve_relative_date(
            WED_2026_04_15, {"phrase": "the other day"}
        ) is None

    def test_number_only_returns_none(self):
        assert resolve_relative_date(WED_2026_04_15, {"phrase": "12"}) is None

    def test_unknown_qualifier_falls_back_to_base(self):
        assert resolve_relative_date(
            WED_2026_04_15, {"phrase": "monday", "qualifier": "soon"}
        ) == date(2026, 4, 13)


class TestResolvedDatesNeverExceedPubDate:
    """Every successful resolution must satisfy result <= pub_date.

    An article cannot describe events that haven't happened yet.
    """

    @pytest.mark.parametrize("phrase,qualifier", [
        ("today", None),
        ("yesterday", None),
        ("monday", None),
        ("tuesday", None),
        ("wednesday", None),
        ("thursday", None),
        ("friday", None),
        ("saturday", None),
        ("sunday", None),
        ("monday", "last"),
        ("march 12", None),
        ("december 10", None),
    ])
    def test_result_not_in_future(self, phrase, qualifier):
        hint: dict = {"phrase": phrase}
        if qualifier is not None:
            hint["qualifier"] = qualifier
        result = resolve_relative_date(WED_2026_04_15, hint)
        if result is not None:
            assert result <= WED_2026_04_15, (
                f"{phrase!r}/{qualifier!r} → {result} exceeds pub_date {WED_2026_04_15}"
            )


class TestCaseAndWhitespace:
    def test_uppercase_phrase(self):
        assert resolve_relative_date(
            WED_2026_04_15, {"phrase": "YESTERDAY"}
        ) == date(2026, 4, 14)

    def test_mixed_case_with_extra_spaces(self):
        assert resolve_relative_date(
            WED_2026_04_15, {"phrase": "  Tuesday  "}
        ) == date(2026, 4, 14)

    def test_mixed_case_month_day(self):
        assert resolve_relative_date(
            WED_2026_04_15, {"phrase": "March   12"}
        ) == date(2026, 3, 12)
