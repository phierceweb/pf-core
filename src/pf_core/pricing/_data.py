"""Built-in per-model pricing tables, USD per 1,000,000 tokens.

Keyed by model-id **prefix**; the resolver takes the first match in
insertion order, so list more-specific prefixes first. Input + output only
— cache and batch surcharges are not modeled (``cache_read`` / ``cache_write``
left ``None``), which keeps the estimate conservative and stable.

These are a convenience baseline, not a maintained price feed. Verify
against each provider's pricing page and add entries as models ship — or,
without editing the framework, register them at startup with
:func:`pf_core.pricing.register_rates`.

The ``openai`` and ``google`` tables ship empty deliberately: shipping a
stale price is worse than returning ``0.0`` (which is visible and warns).
"""

from __future__ import annotations

from pf_core.pricing._types import ModelRates

ANTHROPIC: dict[str, ModelRates] = {
    # Claude 4.x families.
    "claude-opus-4": ModelRates(input=15.0, output=75.0),
    "claude-sonnet-4": ModelRates(input=3.0, output=15.0),
    "claude-haiku-4": ModelRates(input=0.80, output=4.0),
    # Legacy 3.x families (still requestable).
    "claude-3-opus": ModelRates(input=15.0, output=75.0),
    "claude-3-5-sonnet": ModelRates(input=3.0, output=15.0),
    "claude-3-5-haiku": ModelRates(input=1.0, output=5.0),
    "claude-3-haiku": ModelRates(input=0.25, output=1.25),
}

OPENAI: dict[str, ModelRates] = {}

GOOGLE: dict[str, ModelRates] = {}
