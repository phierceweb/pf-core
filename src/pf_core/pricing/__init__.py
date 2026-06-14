"""Shared LLM cost estimation.

One home for per-model pricing, used by the clients to populate
``usage["cost_usd"]`` and available directly for pre-call budget estimates.
Foundation-tier: pure-Python, no dependencies, imports on the base install.

    from pf_core.pricing import estimate_cost

    cost = estimate_cost(
        "anthropic", "claude-opus-4-7",
        prompt_tokens=51_096, completion_tokens=2_628,
    )

Built-in rates cover the Anthropic families; price other providers (or
correct a built-in) at startup with :func:`register_rates`. An unpriced
model returns ``0.0`` and logs once. See ``docs/pricing.md``.
"""

from __future__ import annotations

from pf_core.pricing._resolver import estimate_cost, get_rates, register_rates
from pf_core.pricing._types import ModelRates, PricingError

__all__ = [
    "ModelRates",
    "PricingError",
    "estimate_cost",
    "get_rates",
    "register_rates",
]
