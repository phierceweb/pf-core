"""Tests for pf_core.utils.throttle.Throttle.

The clock and sleep are monkeypatched so the timing logic (slot reservation + staggering) is
verified deterministically, with no real time spent.
"""

from __future__ import annotations

import pytest

from pf_core.utils.throttle import Throttle


class _Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture()
def fake_time(monkeypatch):
    """Patch the module-local ``monotonic``/``sleep`` — returns (clock, slept-durations)."""
    clock = _Clock()
    slept: list[float] = []
    monkeypatch.setattr("pf_core.utils.throttle.monotonic", clock)
    monkeypatch.setattr("pf_core.utils.throttle.sleep", lambda s: slept.append(s))
    return clock, slept


def test_per_second_sets_interval():
    assert Throttle.per_second(2).min_interval_s == 0.5
    assert Throttle.per_second(0).min_interval_s == 0.0  # unthrottled


def test_negative_interval_clamped_to_zero():
    assert Throttle(min_interval_s=-5).min_interval_s == 0.0


def test_disabled_never_sleeps(fake_time):
    _, slept = fake_time
    t = Throttle(min_interval_s=0)
    assert t.acquire() == 0.0
    assert t.acquire() == 0.0
    assert slept == []


def test_first_acquire_does_not_wait(fake_time):
    _, slept = fake_time
    t = Throttle(min_interval_s=1.0)
    assert t.acquire() == 0.0
    assert slept == []


def test_rapid_acquires_are_staggered(fake_time):
    """Three calls at the same instant get slots t, t+Δ, t+2Δ — the concurrent-reservation path."""
    clock, slept = fake_time  # clock frozen at 1000.0
    t = Throttle(min_interval_s=2.0)
    assert t.acquire() == 0.0   # slot 1000, next_allowed 1002
    assert t.acquire() == 2.0   # slot 1002, next_allowed 1004
    assert t.acquire() == 4.0   # slot 1004, next_allowed 1006
    assert slept == [2.0, 4.0]


def test_wait_resets_after_interval_elapses(fake_time):
    clock, slept = fake_time
    t = Throttle(min_interval_s=1.0)
    assert t.acquire() == 0.0   # slot 1000, next_allowed 1001
    clock.t = 1005.0            # well past the next slot
    assert t.acquire() == 0.0   # already due → no wait
    assert slept == []
