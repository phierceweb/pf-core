"""Tests for pf_core.utils.reload_cache — single-slot TTL reload cache."""

from __future__ import annotations

import os
import threading
import time as real_time

import pytest

from pf_core.utils import reload_cache as rc_module
from pf_core.utils.reload_cache import ReloadCache


class _BoomError(Exception):
    pass


class _OtherError(Exception):
    pass


class _FakeTime:
    """Controllable stand-in for the module's ``time.monotonic``."""

    def __init__(self) -> None:
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now


class _RecordingLogger:
    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict]] = []

    def warning(self, event: str, **kwargs) -> None:
        self.warnings.append((event, kwargs))


class _CountingLoader:
    """Returns a fresh dict per call; raises ``self.exc`` when set."""

    def __init__(self) -> None:
        self.calls: list[object] = []
        self.exc: Exception | None = None

    def __call__(self, key: str) -> dict:
        self.calls.append(key)
        if self.exc is not None:
            raise self.exc
        return {"key": key, "load": len(self.calls)}


@pytest.fixture()
def fake_time(monkeypatch):
    ft = _FakeTime()
    monkeypatch.setattr(rc_module, "time", ft)
    return ft


@pytest.fixture()
def log_recorder(monkeypatch):
    rec = _RecordingLogger()
    monkeypatch.setattr(rc_module, "logger", rec)
    return rec


# ---------------------------------------------------------------------------
# TTL behavior
# ---------------------------------------------------------------------------


def test_hit_within_ttl_returns_same_object(fake_time):
    loader = _CountingLoader()
    cache = ReloadCache(loader, ttl=lambda: 60)

    first = cache.get("alpha")
    fake_time.now += 59
    second = cache.get("alpha")

    assert second is first
    assert len(loader.calls) == 1


def test_expired_ttl_reloads(fake_time):
    loader = _CountingLoader()
    cache = ReloadCache(loader, ttl=lambda: 60)

    first = cache.get("alpha")
    fake_time.now += 61
    second = cache.get("alpha")

    assert second is not first
    assert len(loader.calls) == 2


def test_ttl_zero_reloads_every_call(fake_time):
    loader = _CountingLoader()
    cache = ReloadCache(loader, ttl=lambda: 0)

    first = cache.get("alpha")
    second = cache.get("alpha")

    assert second is not first
    assert len(loader.calls) == 2


def test_ttl_read_per_call_env_change_lands(fake_time, monkeypatch):
    monkeypatch.setenv("PF_TEST_RELOAD_SECONDS", "60")
    loader = _CountingLoader()
    cache = ReloadCache(
        loader, ttl=lambda: int(os.environ["PF_TEST_RELOAD_SECONDS"])
    )

    cache.get("alpha")
    cache.get("alpha")
    assert len(loader.calls) == 1

    monkeypatch.setenv("PF_TEST_RELOAD_SECONDS", "0")
    cache.get("alpha")
    assert len(loader.calls) == 2


# ---------------------------------------------------------------------------
# Invalidation — force, clear, key change
# ---------------------------------------------------------------------------


def test_force_reloads_within_ttl(fake_time):
    loader = _CountingLoader()
    cache = ReloadCache(loader, ttl=lambda: 60)

    first = cache.get("alpha")
    second = cache.get("alpha", force=True)

    assert second is not first
    assert len(loader.calls) == 2


def test_clear_drops_value(fake_time):
    loader = _CountingLoader()
    cache = ReloadCache(loader, ttl=lambda: 60)

    first = cache.get("alpha")
    cache.clear()
    second = cache.get("alpha")

    assert second is not first
    assert len(loader.calls) == 2


def test_key_change_invalidates(fake_time):
    loader = _CountingLoader()
    cache = ReloadCache(loader, ttl=lambda: 60)

    cache.get("alpha")
    beta = cache.get("beta")

    assert beta["key"] == "beta"
    assert loader.calls == ["alpha", "beta"]


# ---------------------------------------------------------------------------
# stale_on — serve prior value on covered failures
# ---------------------------------------------------------------------------


def test_stale_on_serves_prior_value_and_warns(fake_time, log_recorder):
    loader = _CountingLoader()
    cache = ReloadCache(loader, ttl=lambda: 10, stale_on=(_BoomError,))

    first = cache.get("alpha")
    loader.exc = _BoomError("unreadable")

    fake_time.now += 11
    stale = cache.get("alpha")

    assert stale is first
    assert len(loader.calls) == 2
    assert [event for event, _ in log_recorder.warnings] == ["reload_cache_kept_stale"]
    assert log_recorder.warnings[0][1]["key"] == "alpha"


def test_stale_on_throttles_retry_to_once_per_ttl(fake_time):
    loader = _CountingLoader()
    cache = ReloadCache(loader, ttl=lambda: 10, stale_on=(_BoomError,))

    first = cache.get("alpha")
    loader.exc = _BoomError("unreadable")

    fake_time.now += 11
    assert cache.get("alpha") is first  # failing reload serves stale
    fake_time.now += 5
    assert cache.get("alpha") is first  # within refreshed TTL: no retry
    assert len(loader.calls) == 2

    fake_time.now += 6  # past the refreshed TTL: retries once more
    assert cache.get("alpha") is first
    assert len(loader.calls) == 3


def test_non_matching_exception_raises_through(fake_time):
    loader = _CountingLoader()
    cache = ReloadCache(loader, ttl=lambda: 10, stale_on=(_BoomError,))

    cache.get("alpha")
    loader.exc = _OtherError("not covered")

    fake_time.now += 11
    with pytest.raises(_OtherError):
        cache.get("alpha")


def test_stale_on_without_prior_value_raises(fake_time):
    loader = _CountingLoader()
    loader.exc = _BoomError("first load fails")
    cache = ReloadCache(loader, ttl=lambda: 10, stale_on=(_BoomError,))

    with pytest.raises(_BoomError):
        cache.get("alpha")
    assert len(loader.calls) == 1


def test_stale_on_with_prior_value_for_other_key_raises(fake_time):
    loader = _CountingLoader()
    cache = ReloadCache(loader, ttl=lambda: 10, stale_on=(_BoomError,))

    cache.get("alpha")
    loader.exc = _BoomError("unreadable")

    with pytest.raises(_BoomError):
        cache.get("beta")


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


def test_two_concurrent_threads_load_once():
    calls: list[object] = []
    barrier = threading.Barrier(2)
    results: list[dict] = []

    def loader(key: str) -> dict:
        calls.append(key)
        real_time.sleep(0.05)  # hold the lock so the second thread must wait
        return {"key": key}

    cache = ReloadCache(loader, ttl=lambda: 60)

    def worker() -> None:
        barrier.wait()
        results.append(cache.get("alpha"))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(calls) == 1
    assert results[0] is results[1]
