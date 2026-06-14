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
    """

    input: float
    output: float
    cache_read: float | None = None
    cache_write: float | None = None
