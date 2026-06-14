"""Tests for ``pf_core.budget.scheduler``.

The scheduler is a thin daemon-thread wrapper over ``refresh_snapshots()``.
We never start a real network or DB call here — every test patches both
``threading.Timer`` (so nothing actually fires) and ``refresh_snapshots``
(so we observe call shape without touching the DB).
"""

from __future__ import annotations

import threading
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _reset_started_flag(monkeypatch):
    """Each test sees a fresh ``_started`` flag and lock."""
    import pf_core.budget.scheduler as scheduler

    monkeypatch.setattr(scheduler, "_started", False)
    monkeypatch.setattr(scheduler, "_lock", threading.Lock())


class TestStartBudgetRefreshLoop:
    def test_starts_a_timer_on_first_call(self):
        from pf_core.budget.scheduler import start_budget_refresh_loop

        with patch("pf_core.budget.scheduler.threading.Timer") as TimerCls:
            timer = TimerCls.return_value
            start_budget_refresh_loop(interval_seconds=60)
            TimerCls.assert_called_once()
            assert TimerCls.call_args.args[0] == 60
            assert timer.daemon is True
            timer.start.assert_called_once()

    def test_idempotent_second_call_is_noop(self):
        from pf_core.budget.scheduler import start_budget_refresh_loop

        with patch("pf_core.budget.scheduler.threading.Timer") as TimerCls:
            start_budget_refresh_loop(interval_seconds=60)
            start_budget_refresh_loop(interval_seconds=60)
            start_budget_refresh_loop(interval_seconds=60)
            assert TimerCls.call_count == 1

    def test_default_interval(self):
        from pf_core.budget.scheduler import (
            _DEFAULT_INTERVAL_SECONDS,
            start_budget_refresh_loop,
        )

        with patch("pf_core.budget.scheduler.threading.Timer") as TimerCls:
            start_budget_refresh_loop()
            assert TimerCls.call_args.args[0] == _DEFAULT_INTERVAL_SECONDS

    def test_re_export_from_pf_core_budget(self):
        """``start_budget_refresh_loop`` is importable from the package root."""
        from pf_core.budget import start_budget_refresh_loop as exported
        from pf_core.budget.scheduler import start_budget_refresh_loop

        assert exported is start_budget_refresh_loop


class TestTick:
    def test_calls_refresh_snapshots_then_reschedules(self):
        from pf_core.budget.scheduler import _tick

        with patch("pf_core.budget.scheduler.refresh_snapshots") as refresh, \
             patch("pf_core.budget.scheduler.threading.Timer") as TimerCls:
            _tick(60)
            refresh.assert_called_once()
            TimerCls.assert_called_once()
            assert TimerCls.call_args.args[0] == 60

    def test_swallows_refresh_errors_and_reschedules(self):
        """A failed ``refresh_snapshots()`` must not stop the loop."""
        from pf_core.budget.scheduler import _tick

        with patch(
            "pf_core.budget.scheduler.refresh_snapshots",
            side_effect=RuntimeError("boom"),
        ), patch("pf_core.budget.scheduler.threading.Timer") as TimerCls:
            timer = TimerCls.return_value
            # _tick must not raise even when refresh_snapshots raises
            _tick(60)
            # New timer scheduled for the next interval despite the failure
            TimerCls.assert_called_once()
            timer.start.assert_called_once()

    def test_rescheduled_timer_is_daemon(self):
        from pf_core.budget.scheduler import _tick

        with patch("pf_core.budget.scheduler.refresh_snapshots"), \
             patch("pf_core.budget.scheduler.threading.Timer") as TimerCls:
            timer = TimerCls.return_value
            _tick(30)
            assert timer.daemon is True
