"""Client-side request throttle — enforce a minimum interval between operations.

Many open-data / public APIs cap callers at a fixed rate (MusicBrainz ~1 req/s, OSM Nominatim,
Wikipedia, OpenLibrary, …) and return ``503``/``429`` if you exceed it. Rather than have every
client reimplement the sleep-between-calls dance, share this:

    from pf_core.utils.throttle import Throttle

    throttle = Throttle.per_second(1)          # or Throttle(min_interval_s=1.0)
    for name in names:
        throttle.acquire()                     # blocks until the next slot is due
        resp = httpx.get(...)

It is **thread-safe**: N worker threads calling ``acquire()`` are handed staggered slots
``t, t+Δ, t+2Δ, …``, so the aggregate outbound rate still respects the interval even when the
calls fan out through :func:`pf_core.parallel.run_parallel`. ``min_interval_s <= 0`` disables
throttling entirely (e.g. when pointed at a local mirror).

This is the *outbound* client-pacing counterpart to :mod:`pf_core.web.rate_limit` (which limits
*inbound* requests per IP on a FastAPI app).
"""

from __future__ import annotations

import threading
from time import monotonic, sleep


class Throttle:
    """Enforce a minimum interval between successive ``acquire()`` calls."""

    def __init__(self, *, min_interval_s: float) -> None:
        """
        Args:
            min_interval_s: Minimum seconds between grants. ``<= 0`` disables throttling.
        """
        self.min_interval_s = max(0.0, float(min_interval_s))
        self._lock = threading.Lock()
        self._next_allowed = 0.0  # monotonic() time the next acquire may proceed

    @classmethod
    def per_second(cls, rate: float) -> Throttle:
        """Build a throttle capped at ``rate`` operations per second (``rate <= 0`` → unthrottled)."""
        return cls(min_interval_s=1.0 / rate if rate > 0 else 0.0)

    def acquire(self) -> float:
        """Block until this caller's slot is due.

        Reserves the slot under a lock (so concurrent callers get distinct, staggered slots), then
        sleeps outside the lock.

        Returns:
            The seconds actually slept — ``0.0`` when the slot was already due.
        """
        if self.min_interval_s <= 0:
            return 0.0
        with self._lock:
            now = monotonic()
            slot = self._next_allowed if self._next_allowed > now else now
            self._next_allowed = slot + self.min_interval_s
        wait = slot - now
        if wait > 0:
            sleep(wait)
        return wait


__all__ = ["Throttle"]
