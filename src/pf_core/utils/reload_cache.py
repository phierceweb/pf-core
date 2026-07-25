"""Single-slot TTL reload cache for hot-reloadable config loaders.

``loader(key)`` produces the value; ``ttl()`` is re-read on every call so
env-driven TTLs land without a restart; a key change invalidates
immediately. Exception types in ``stale_on`` serve the last good value
with a warning instead of raising; anything else raises through.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any, Generic, TypeVar

from pf_core.log import get_logger

logger = get_logger(__name__)

K = TypeVar("K")
T = TypeVar("T")

_UNSET: Any = object()


class ReloadCache(Generic[K, T]):
    """Thread-safe TTL cache holding one value (double-checked lock)."""

    def __init__(
        self,
        loader: Callable[[K], T],
        *,
        ttl: Callable[[], int],
        stale_on: tuple[type[BaseException], ...] = (),
    ) -> None:
        self._loader = loader
        self._ttl = ttl
        self._stale_on = stale_on
        self._lock = threading.Lock()
        self._value: T = _UNSET
        self._key: K | None = None
        self._loaded_at = 0.0

    def get(self, key: K | None = None, *, force: bool = False) -> T:
        """Return the value for *key*, reloading when stale.

        ``ttl() <= 0`` reloads on every call. A ``stale_on`` failure with a
        prior value for the same key warns (``reload_cache_kept_stale``),
        refreshes the age — throttling retries to once per TTL — and serves
        the prior value; with no prior value the exception raises through.
        """
        ttl = self._ttl()
        now = time.monotonic()
        value = self._value
        if (
            not force
            and value is not _UNSET
            and self._key == key
            and ttl > 0
            and (now - self._loaded_at) < ttl
        ):
            return value

        with self._lock:
            now = time.monotonic()
            value = self._value
            if (
                not force
                and value is not _UNSET
                and self._key == key
                and ttl > 0
                and (now - self._loaded_at) < ttl
            ):
                return value

            try:
                loaded = self._loader(key)
            except self._stale_on as exc:
                if self._value is not _UNSET and self._key == key:
                    logger.warning(
                        "reload_cache_kept_stale", key=key, error=str(exc)
                    )
                    self._loaded_at = now
                    return self._value
                raise

            self._value = loaded
            self._key = key
            self._loaded_at = now
            return loaded

    def clear(self) -> None:
        """Drop the cached value; the next ``get()`` reloads."""
        with self._lock:
            self._value = _UNSET
            self._key = None
            self._loaded_at = 0.0


__all__ = ["ReloadCache"]
