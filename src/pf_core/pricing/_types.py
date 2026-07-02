"""Types for pf_core.pricing."""

from __future__ import annotations

from dataclasses import dataclass


class PricingError(Exception):
    """Invalid pricing-table registration (bad provider, model, or rates)."""


@dataclass(frozen=True)
class ModelRates:
    """USD rates per 1,000,000 tokens for one model (or model-id prefix).

    ``cache_read`` / ``cache_write`` are optional — left ``None`` they are
    not modeled, and cached tokens add nothing to the estimate (the
    conservative default that matches a plain input+output calculation).
    ``cache_write`` is the default-TTL (5-minute) write rate;
    ``cache_write_1h`` is the 1-hour-TTL write rate, used when the caller
    passes ``cache_ttl="1h"`` to :func:`estimate_cost` (falls back to
    ``cache_write`` when unset).
    """

    input: float
    output: float
    cache_read: float | None = None
    cache_write: float | None = None
    cache_write_1h: float | None = None
