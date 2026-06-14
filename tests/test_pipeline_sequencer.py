"""Tests for pf_core.pipeline.sequencer."""

from __future__ import annotations

import pytest

from pf_core.pipeline.sequencer import Phase, UnknownStageError, run_pipeline


class _Rec:
    """A phase that records the order of run() calls into a shared list."""

    def __init__(self, name: str, log: list[str]) -> None:
        self.name = name
        self._log = log

    def run(self, ctx: object) -> None:
        self._log.append(self.name)


def _pipeline(log: list[str]) -> list[Phase]:
    return [_Rec(n, log) for n in ("a", "b", "c", "d")]


# --- full run ------------------------------------------------------------


def test_no_args_runs_every_phase_in_order() -> None:
    log: list[str] = []
    ran = run_pipeline(_pipeline(log), ctx=None)
    assert ran == ["a", "b", "c", "d"]
    assert log == ["a", "b", "c", "d"]


def test_ctx_is_threaded_verbatim_to_each_phase() -> None:
    seen: list[object] = []

    class _Capture:
        name = "x"

        def run(self, ctx: object) -> None:
            seen.append(ctx)

    sentinel = object()
    run_pipeline([_Capture()], ctx=sentinel)
    assert seen == [sentinel]


# --- start / stop_after / single phase -----------------------------------


def test_start_begins_at_named_phase() -> None:
    log: list[str] = []
    ran = run_pipeline(_pipeline(log), ctx=None, start="c")
    assert ran == ["c", "d"]
    assert log == ["c", "d"]


def test_stop_after_halts_after_named_phase() -> None:
    log: list[str] = []
    ran = run_pipeline(_pipeline(log), ctx=None, stop_after="b")
    assert ran == ["a", "b"]


def test_start_equals_stop_after_runs_exactly_one_phase() -> None:
    log: list[str] = []
    ran = run_pipeline(_pipeline(log), ctx=None, start="b", stop_after="b")
    assert ran == ["b"]
    assert log == ["b"]


def test_stop_after_before_start_raises_value_error() -> None:
    with pytest.raises(ValueError, match="resolves before start"):
        run_pipeline(_pipeline([]), ctx=None, start="c", stop_after="a")


# --- rerun_from ----------------------------------------------------------


def test_rerun_from_begins_at_named_phase() -> None:
    log: list[str] = []
    ran = run_pipeline(_pipeline(log), ctx=None, rerun_from="b")
    assert ran == ["b", "c", "d"]


def test_start_takes_precedence_over_rerun_from() -> None:
    log: list[str] = []
    ran = run_pipeline(_pipeline(log), ctx=None, start="d", rerun_from="a")
    assert ran == ["d"]


# --- skip_fresh (resume) -------------------------------------------------


def test_skip_fresh_skips_leading_fresh_phases() -> None:
    log: list[str] = []
    fresh = {"a", "b"}
    ran = run_pipeline(
        _pipeline(log), ctx=None, skip_fresh=lambda p: p.name in fresh
    )
    assert ran == ["c", "d"]


def test_skip_fresh_only_skips_the_leading_run() -> None:
    """A fresh phase AFTER the first stale one still runs — only the
    leading contiguous fresh prefix is skipped."""
    log: list[str] = []
    fresh = {"a", "c"}  # 'a' leading-fresh, 'c' fresh but mid-run
    ran = run_pipeline(
        _pipeline(log), ctx=None, skip_fresh=lambda p: p.name in fresh
    )
    assert ran == ["b", "c", "d"]


def test_skip_fresh_all_fresh_returns_empty_and_runs_nothing() -> None:
    log: list[str] = []
    ran = run_pipeline(_pipeline(log), ctx=None, skip_fresh=lambda p: True)
    assert ran == []
    assert log == []


def test_start_takes_precedence_over_skip_fresh() -> None:
    log: list[str] = []
    ran = run_pipeline(
        _pipeline(log), ctx=None, start="b", skip_fresh=lambda p: True
    )
    assert ran == ["b", "c", "d"]


def test_rerun_from_takes_precedence_over_skip_fresh() -> None:
    log: list[str] = []
    ran = run_pipeline(
        _pipeline(log), ctx=None, rerun_from="c", skip_fresh=lambda p: True
    )
    assert ran == ["c", "d"]


def test_skip_fresh_combines_with_stop_after() -> None:
    log: list[str] = []
    ran = run_pipeline(
        _pipeline(log),
        ctx=None,
        skip_fresh=lambda p: p.name == "a",
        stop_after="c",
    )
    assert ran == ["b", "c"]


# --- unknown stage errors ------------------------------------------------


@pytest.mark.parametrize("kwarg", ["start", "stop_after", "rerun_from"])
def test_unknown_stage_name_raises(kwarg: str) -> None:
    with pytest.raises(UnknownStageError, match=f"unknown {kwarg} stage: 'zzz'"):
        run_pipeline(_pipeline([]), ctx=None, **{kwarg: "zzz"})


def test_unknown_stage_error_is_a_value_error() -> None:
    assert issubclass(UnknownStageError, ValueError)


# --- protocol ------------------------------------------------------------


def test_phase_protocol_is_runtime_checkable() -> None:
    assert isinstance(_Rec("a", []), Phase)

    class _NotAPhase:
        name = "x"  # missing run()

    assert not isinstance(_NotAPhase(), Phase)
