"""Tests for pf_core.eval._compare (comparator registry + structured_diff)."""

from __future__ import annotations

import pytest

from pf_core.eval._compare import (
    _field_score,
    get_comparator,
    list_comparators,
    register_comparator,
    structured_diff,
)
from pf_core.exceptions import ConfigurationError


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_structured_diff_registered():
    assert "structured_diff" in list_comparators()


def test_get_comparator_by_name():
    fn = get_comparator("structured_diff")
    assert callable(fn)


def test_get_comparator_custom_prefix():
    @register_comparator("my_custom")
    def _my(golden, replay, *, context):
        return 0.5

    fn = get_comparator("custom:my_custom")
    assert fn is _my

    fn2 = get_comparator("my_custom")
    assert fn2 is _my


def test_get_comparator_unknown():
    with pytest.raises(ConfigurationError, match="Unknown comparator"):
        get_comparator("no_such_comparator_xyz")


# ---------------------------------------------------------------------------
# _field_score
# ---------------------------------------------------------------------------


def test_field_score_both_none():
    assert _field_score(None, None, tolerance=None) == 1.0


def test_field_score_one_none():
    assert _field_score(1.0, None, tolerance=None) == 0.0
    assert _field_score(None, 1.0, tolerance=None) == 0.0


def test_field_score_type_mismatch():
    assert _field_score("hello", 42, tolerance=None) == 0.0


def test_field_score_exact_string():
    assert _field_score("foo", "foo", tolerance=None) == 1.0
    assert _field_score("foo", "bar", tolerance=None) == 0.0


def test_field_score_numeric_exact():
    assert _field_score(3.14, 3.14, tolerance=None) == 1.0
    assert _field_score(3.14, 3.15, tolerance=None) == 0.0


def test_field_score_numeric_with_tolerance():
    assert _field_score(10.0, 10.0, tolerance=2.0) == 1.0
    assert _field_score(10.0, 11.5, tolerance=2.0) == 1.0  # within tolerance
    score = _field_score(10.0, 15.0, tolerance=2.0)
    assert 0.0 <= score < 1.0


def test_field_score_int_float_coercion():
    assert _field_score(10, 10.0, tolerance=None) == 1.0
    assert _field_score(10.0, 10, tolerance=None) == 1.0


def test_field_score_list_iou():
    assert _field_score(["a", "b", "c"], ["a", "b", "c"], tolerance=None) == 1.0
    assert _field_score(["a", "b"], ["a", "b", "c"], tolerance=None) == pytest.approx(2 / 3)
    assert _field_score(["a"], ["z"], tolerance=None) == 0.0
    assert _field_score([], [], tolerance=None) == 1.0


# ---------------------------------------------------------------------------
# structured_diff
# ---------------------------------------------------------------------------


def test_structured_diff_perfect_match():
    golden = {"category": "analysis", "confidence": 0.9}
    replay = {"category": "analysis", "confidence": 0.9}
    score = structured_diff(golden, replay, context={})
    assert score == 1.0


def test_structured_diff_partial_match():
    golden = {"a": "x", "b": "y"}
    replay = {"a": "x", "b": "z"}
    score = structured_diff(golden, replay, context={})
    assert score == pytest.approx(0.5)


def test_structured_diff_specific_fields():
    golden = {"score": 85.0, "category": "analysis", "ignored": "foo"}
    replay = {"score": 85.0, "category": "wrong", "ignored": "bar"}
    score = structured_diff(golden, replay, context={"diff_fields": ["score"]})
    assert score == 1.0


def test_structured_diff_with_tolerance():
    golden = {"score": 85.0}
    replay = {"score": 87.0}
    score_exact = structured_diff(golden, replay, context={"diff_fields": ["score"]})
    assert score_exact == 0.0

    score_tolerant = structured_diff(
        golden, replay, context={"diff_fields": ["score"], "tolerances": {"score": 3.0}}
    )
    assert score_tolerant == 1.0


def test_structured_diff_missing_field_in_replay():
    golden = {"a": "x", "b": "y"}
    replay = {"a": "x"}  # b missing
    score = structured_diff(golden, replay, context={})
    assert score == pytest.approx(0.5)


def test_structured_diff_empty_golden():
    score = structured_diff({}, {}, context={})
    assert score == 1.0


def test_structured_diff_via_get_comparator():
    fn = get_comparator("structured_diff")
    score = fn({"x": 1}, {"x": 1}, context={})
    assert score == 1.0
