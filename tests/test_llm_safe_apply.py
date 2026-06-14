"""Tests for pf_core.llm.safe_apply — gather/apply with drift detection."""

from __future__ import annotations

import dataclasses
import logging

import pytest

from pf_core.llm.safe_apply import (
    DriftReport,
    GatherResult,
    detect_drift,
    safe_apply,
)


# ---------------------------------------------------------------------------
# GatherResult
# ---------------------------------------------------------------------------


class TestGatherResult:
    def test_holds_data_and_targets(self) -> None:
        g = GatherResult(
            target_count=3,
            target_texts=("a", "b", "c"),
            data={"transform": "rename"},
        )
        assert g.target_count == 3
        assert g.target_texts == ("a", "b", "c")
        assert g.data == {"transform": "rename"}

    def test_is_frozen(self) -> None:
        """GatherResult must be immutable so the snapshot can't be
        mutated between gather and apply."""
        g = GatherResult(target_count=1, target_texts=("x",), data=42)
        with pytest.raises(dataclasses.FrozenInstanceError):
            g.target_count = 99  # type: ignore[misc]

    def test_data_is_generic(self) -> None:
        """Consumers store their own transform plan in `data` — could be
        a dict, a dataclass, a list, anything."""
        g_int: GatherResult[int] = GatherResult(target_count=0, target_texts=(), data=42)
        g_dict: GatherResult[dict[int, int]] = GatherResult(
            target_count=2, target_texts=("a", "b"), data={1: 2, 3: 4}
        )
        assert g_int.data == 42
        assert g_dict.data == {1: 2, 3: 4}


# ---------------------------------------------------------------------------
# detect_drift
# ---------------------------------------------------------------------------


class TestDetectDrift:
    def test_no_drift_when_count_and_texts_match(self) -> None:
        g = GatherResult(target_count=3, target_texts=("a", "b", "c"), data=None)
        report = detect_drift(g, ["a", "b", "c"])
        assert report.has_drift is False
        assert report.count_changed is False
        assert report.drifted_indices == ()

    def test_count_drift_reported(self) -> None:
        g = GatherResult(target_count=2, target_texts=("a", "b"), data=None)
        report = detect_drift(g, ["a", "b", "c"])
        assert report.has_drift is True
        assert report.count_changed is True
        assert report.gathered_count == 2
        assert report.current_count == 3

    def test_count_drift_short_circuits_text_check(self) -> None:
        """When counts differ there's no meaningful per-index text
        comparison — the drifted_indices list stays empty rather than
        containing a misleading "everything beyond index N changed"."""
        g = GatherResult(target_count=2, target_texts=("a", "b"), data=None)
        report = detect_drift(g, ["a"])
        assert report.has_drift is True
        assert report.count_changed is True
        assert report.drifted_indices == ()

    def test_text_drift_at_one_index(self) -> None:
        g = GatherResult(target_count=3, target_texts=("a", "b", "c"), data=None)
        report = detect_drift(g, ["a", "BBB", "c"])
        assert report.has_drift is True
        assert report.count_changed is False
        assert report.drifted_indices == (1,)

    def test_text_drift_at_multiple_indices(self) -> None:
        g = GatherResult(target_count=3, target_texts=("a", "b", "c"), data=None)
        report = detect_drift(g, ["A", "B", "c"])
        assert report.has_drift is True
        assert report.drifted_indices == (0, 1)

    def test_empty_lists_no_drift(self) -> None:
        g = GatherResult(target_count=0, target_texts=(), data=None)
        report = detect_drift(g, [])
        assert report.has_drift is False

    def test_accepts_any_sequence_for_current(self) -> None:
        """Caller may pass list, tuple, or anything sequence-shaped."""
        g = GatherResult(target_count=2, target_texts=("a", "b"), data=None)
        assert detect_drift(g, ["a", "b"]).has_drift is False
        assert detect_drift(g, ("a", "b")).has_drift is False


# ---------------------------------------------------------------------------
# DriftReport.has_drift
# ---------------------------------------------------------------------------


class TestDriftReport:
    def test_has_drift_true_on_count_change(self) -> None:
        r = DriftReport(
            count_changed=True, gathered_count=2, current_count=3, drifted_indices=()
        )
        assert r.has_drift is True

    def test_has_drift_true_on_text_drift(self) -> None:
        r = DriftReport(
            count_changed=False, gathered_count=3, current_count=3, drifted_indices=(1,)
        )
        assert r.has_drift is True

    def test_has_drift_false_when_clean(self) -> None:
        r = DriftReport(
            count_changed=False, gathered_count=3, current_count=3, drifted_indices=()
        )
        assert r.has_drift is False


# ---------------------------------------------------------------------------
# safe_apply
# ---------------------------------------------------------------------------


class TestSafeApply:
    def test_returns_apply_fn_result_when_no_drift(self) -> None:
        g = GatherResult(target_count=2, target_texts=("a", "b"), data="payload")

        def transform(data: str) -> str:
            return data.upper()

        assert safe_apply(g, ["a", "b"], transform) == "PAYLOAD"

    def test_apply_fn_receives_gathered_data(self) -> None:
        g = GatherResult(target_count=1, target_texts=("x",), data={"k": 42})
        captured: dict[str, dict[str, int]] = {}

        def transform(data: dict[str, int]) -> int:
            captured["got"] = data
            return data["k"]

        result = safe_apply(g, ["x"], transform)
        assert result == 42
        assert captured["got"] == {"k": 42}

    def test_returns_none_on_count_drift(self, caplog) -> None:
        g = GatherResult(target_count=2, target_texts=("a", "b"), data="payload")

        def transform(data: str) -> str:
            return data.upper()

        with caplog.at_level(logging.WARNING, logger="pf_core.llm.safe_apply"):
            result = safe_apply(g, ["a", "b", "c"], transform)
        assert result is None
        assert any("safe_apply_drift" in r.getMessage() for r in caplog.records)

    def test_returns_none_on_text_drift(self, caplog) -> None:
        g = GatherResult(target_count=2, target_texts=("a", "b"), data="payload")

        def transform(data: str) -> str:
            return data.upper()

        with caplog.at_level(logging.WARNING, logger="pf_core.llm.safe_apply"):
            result = safe_apply(g, ["a", "BBB"], transform)
        assert result is None
        assert any("safe_apply_drift" in r.getMessage() for r in caplog.records)

    def test_apply_fn_not_called_on_drift(self) -> None:
        """Critical safety property: the transform must NOT run if drift
        was detected. Otherwise the whole point is defeated."""
        g = GatherResult(target_count=2, target_texts=("a", "b"), data=None)
        called = []

        def transform(data: object) -> str:
            called.append("yes")
            return "ran"

        safe_apply(g, ["a", "different"], transform)
        assert called == []

    def test_label_appears_in_log(self, caplog) -> None:
        """Multiple safe_apply call sites in the same log stream are
        distinguishable by the `label` kwarg."""
        g = GatherResult(target_count=1, target_texts=("a",), data=None)

        def transform(data: object) -> None:
            return None

        with caplog.at_level(logging.WARNING, logger="pf_core.llm.safe_apply"):
            safe_apply(g, ["b"], transform, label="heading_normalize")
        assert any("heading_normalize" in r.getMessage() for r in caplog.records)

    def test_default_label_is_transform(self, caplog) -> None:
        g = GatherResult(target_count=1, target_texts=("a",), data=None)

        def transform(data: object) -> None:
            return None

        with caplog.at_level(logging.WARNING, logger="pf_core.llm.safe_apply"):
            safe_apply(g, ["b"], transform)  # no label kwarg
        assert any("transform" in r.getMessage() for r in caplog.records)

    def test_no_log_when_clean(self, caplog) -> None:
        """No warning emitted on the happy path — log noise control."""
        g = GatherResult(target_count=2, target_texts=("a", "b"), data="ok")

        def transform(data: str) -> str:
            return data

        with caplog.at_level(logging.WARNING, logger="pf_core.llm.safe_apply"):
            safe_apply(g, ["a", "b"], transform)
        assert not any("safe_apply_drift" in r.getMessage() for r in caplog.records)
