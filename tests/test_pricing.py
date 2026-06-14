"""Tests for pf_core.pricing — shared LLM cost estimation."""

from __future__ import annotations

import pytest

from pf_core.pricing import (
    ModelRates,
    PricingError,
    estimate_cost,
    get_rates,
    register_rates,
)
from pf_core.pricing import _resolver


@pytest.fixture(autouse=True)
def _reset_pricing_state():
    _resolver._REGISTERED.clear()
    _resolver._unknown_warned.clear()
    yield
    _resolver._REGISTERED.clear()
    _resolver._unknown_warned.clear()


# ---------------------------------------------------------------------------
# Known models
# ---------------------------------------------------------------------------


def test_known_anthropic_model_cost():
    # opus: input 15/1M, output 75/1M -> 1M each = 90.0
    cost = estimate_cost(
        "anthropic", "claude-opus-4-7",
        prompt_tokens=1_000_000, completion_tokens=1_000_000,
    )
    assert cost == pytest.approx(90.0)


def test_prefix_match_handles_date_and_dot_suffixes():
    # date-suffixed id resolves to the haiku-4 prefix (0.80/1M input)
    a = estimate_cost(
        "anthropic", "claude-haiku-4-5-20251001",
        prompt_tokens=1_000_000, completion_tokens=0,
    )
    assert a == pytest.approx(0.80)
    # dotted version resolves to the sonnet-4 prefix (15.0/1M output)
    b = estimate_cost(
        "anthropic", "claude-sonnet-4.6",
        prompt_tokens=0, completion_tokens=1_000_000,
    )
    assert b == pytest.approx(15.0)


def test_openrouter_namespace_strips_to_underlying_provider():
    cost = estimate_cost(
        "openrouter", "anthropic/claude-opus-4-7",
        prompt_tokens=1_000_000, completion_tokens=0,
    )
    assert cost == pytest.approx(15.0)


def test_get_rates_returns_modelrates_or_none():
    assert get_rates("anthropic", "claude-opus-4-7") == ModelRates(input=15.0, output=75.0)
    assert get_rates("anthropic", "no-such-model") is None


# ---------------------------------------------------------------------------
# Unknown models / providers
# ---------------------------------------------------------------------------


def test_unknown_model_returns_zero_and_warns_once():
    assert estimate_cost("anthropic", "mystery-x", prompt_tokens=1000, completion_tokens=1000) == 0.0
    # second call still 0.0; the model is recorded so the warning won't re-fire
    assert estimate_cost("anthropic", "mystery-x", prompt_tokens=1000, completion_tokens=1000) == 0.0
    assert "anthropic:mystery-x" in _resolver._unknown_warned


def test_unknown_provider_returns_zero():
    # built-in openai/google tables ship empty; unknown -> 0.0
    assert estimate_cost("openai", "gpt-4o-mini", prompt_tokens=1000, completion_tokens=1000) == 0.0


# ---------------------------------------------------------------------------
# Consumer registration
# ---------------------------------------------------------------------------


def test_register_rates_adds_model():
    register_rates("openai", "gpt-4o-mini", ModelRates(input=0.15, output=0.60))
    cost = estimate_cost(
        "openai", "gpt-4o-mini",
        prompt_tokens=1_000_000, completion_tokens=1_000_000,
    )
    assert cost == pytest.approx(0.75)


def test_register_rates_overrides_builtin():
    register_rates("anthropic", "claude-opus-4", ModelRates(input=1.0, output=2.0))
    cost = estimate_cost(
        "anthropic", "claude-opus-4-7",
        prompt_tokens=1_000_000, completion_tokens=0,
    )
    assert cost == pytest.approx(1.0)  # registered beats the 15.0 built-in


def test_register_rates_validates():
    with pytest.raises(PricingError):
        register_rates("", "m", ModelRates(input=1.0, output=2.0))
    with pytest.raises(PricingError):
        register_rates("openai", "m", "not-a-modelrates")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Cache rates
# ---------------------------------------------------------------------------


def test_cache_tokens_ignored_when_rates_undefined():
    # built-in anthropic entries leave cache_read/write None -> cache tokens free
    cost = estimate_cost(
        "anthropic", "claude-opus-4-7",
        prompt_tokens=0, completion_tokens=0,
        cache_read_tokens=1_000_000, cache_write_tokens=1_000_000,
    )
    assert cost == 0.0


def test_cache_rates_applied_when_defined():
    register_rates("custom", "m", ModelRates(input=10.0, output=20.0, cache_read=1.0, cache_write=2.0))
    cost = estimate_cost(
        "custom", "m",
        prompt_tokens=1_000_000, completion_tokens=1_000_000,
        cache_read_tokens=1_000_000, cache_write_tokens=1_000_000,
    )
    assert cost == pytest.approx(33.0)  # 10 + 20 + 1 + 2
