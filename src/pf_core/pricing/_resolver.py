"""Resolution + cost estimation for pf_core.pricing."""

from __future__ import annotations

from pf_core.log import get_logger
from pf_core.pricing._data import ANTHROPIC, GOOGLE, OPENAI
from pf_core.pricing._types import ModelRates, PricingError

logger = get_logger(__name__)

# Built-in tables keyed by provider name.
_BUILTIN: dict[str, dict[str, ModelRates]] = {
    "anthropic": ANTHROPIC,
    "openai": OPENAI,
    "google": GOOGLE,
}

# Consumer-registered rates, checked before the built-ins.
_REGISTERED: dict[str, dict[str, ModelRates]] = {}

# Models already warned about (one log line per provider:model per process).
_unknown_warned: set[str] = set()


def register_rates(provider: str, model_prefix: str, rates: ModelRates) -> None:
    """Register a model's rates, overriding any built-in with the same prefix.

    Lets a consumer price models pf-core doesn't ship (or correct a stale
    built-in) at startup, without editing the framework::

        from pf_core.pricing import ModelRates, register_rates
        register_rates("openai", "gpt-4o-mini", ModelRates(input=0.15, output=0.60))

    Raises:
        PricingError: if ``provider`` / ``model_prefix`` is empty or
            ``rates`` is not a :class:`ModelRates`.
    """
    if not provider or not model_prefix:
        raise PricingError("provider and model_prefix are required")
    if not isinstance(rates, ModelRates):
        raise PricingError("rates must be a ModelRates instance")
    _REGISTERED.setdefault(provider.lower(), {})[model_prefix] = rates


def _normalize(provider: str, model: str) -> tuple[str, str]:
    """Resolve a namespaced model id to (provider, bare-model).

    An OpenRouter-style ``anthropic/claude-...`` id (whatever ``provider``
    was passed) is split so the underlying provider's table is used.
    """
    provider = (provider or "").lower()
    if "/" in model:
        ns, rest = model.split("/", 1)
        ns = ns.lower()
        if ns in _BUILTIN or ns in _REGISTERED:
            return ns, rest
    return provider, model


def get_rates(provider: str, model: str) -> ModelRates | None:
    """Return the :class:`ModelRates` for ``model``, or ``None`` if unpriced.

    Registered rates win over built-ins; within a table the first
    prefix match (insertion order) wins.
    """
    provider, model = _normalize(provider, model)
    for table in (_REGISTERED.get(provider, {}), _BUILTIN.get(provider, {})):
        for prefix, rates in table.items():
            if model.startswith(prefix):
                return rates
    return None


def estimate_cost(
    provider: str,
    model: str,
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_ttl: str = "5m",
) -> float:
    """Estimate the USD cost of one call.

    Returns ``0.0`` for an unpriced model, logging ``pricing_unknown_model``
    once per ``provider:model`` per process. Cache token costs are added
    only when the model's :class:`ModelRates` defines cache rates.
    ``cache_ttl="1h"`` prices cache writes at the model's ``cache_write_1h``
    rate when defined (falling back to ``cache_write``); any other value
    uses ``cache_write``.
    """
    rates = get_rates(provider, model)
    if rates is None:
        key = f"{(provider or '').lower()}:{model}"
        if key not in _unknown_warned:
            _unknown_warned.add(key)
            logger.warning(
                "pricing_unknown_model",
                provider=provider,
                model=model,
                message=(
                    "no pricing entry — cost_usd will be 0.0; register one "
                    "with pf_core.pricing.register_rates"
                ),
            )
        return 0.0
    cost = (
        prompt_tokens * rates.input / 1_000_000
        + completion_tokens * rates.output / 1_000_000
    )
    if rates.cache_read is not None:
        cost += cache_read_tokens * rates.cache_read / 1_000_000
    cache_write_rate = rates.cache_write
    if cache_ttl == "1h" and rates.cache_write_1h is not None:
        cache_write_rate = rates.cache_write_1h
    if cache_write_rate is not None:
        cost += cache_write_tokens * cache_write_rate / 1_000_000
    return cost
