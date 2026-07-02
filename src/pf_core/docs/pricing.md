# Pricing

Shared LLM cost estimation. One home for per-model rates, used by the clients to populate `usage["cost_usd"]` and callable directly for a pre-call budget estimate. Foundation-tier — pure-Python, no dependencies, imports on the base install.

## Usage

```python
from pf_core.pricing import estimate_cost

cost = estimate_cost(
    "anthropic", "claude-opus-4-7",
    prompt_tokens=51_096, completion_tokens=2_628,
)
```

`estimate_cost(provider, model, *, prompt_tokens=0, completion_tokens=0, cache_read_tokens=0, cache_write_tokens=0, cache_ttl="5m") -> float` returns USD. Rates are per 1,000,000 tokens.

## Resolution

- **Prefix match.** The model id is matched against the provider's table by prefix; the first match (insertion order) wins. So `claude-opus-4-7`, `claude-opus-4-20250101`, and `claude-opus-4.1` all resolve to the `claude-opus-4` entry.
- **Namespaced ids.** An OpenRouter-style `anthropic/claude-opus-4-7` is split on `/` and priced against the underlying provider's table — so `estimate_cost("openrouter", "anthropic/claude-...")` works.
- **Unknown model → `0.0`**, with a one-shot `pricing_unknown_model` log warning per `provider:model` per process. Treat `0.0` as "unpriced", not "free".
- **Cache tokens** are added only when the model's `ModelRates` defines `cache_read` / `cache_write`. The built-in Anthropic entries carry them at the provider's published multipliers of the input rate (read 0.1x; write 1.25x for the 5-minute TTL, 2x for 1-hour). `cache_ttl="1h"` selects `cache_write_1h` when defined, falling back to `cache_write`; any other value uses `cache_write`.

## Built-in rates

The `anthropic` table ships with the Claude 4.x and legacy 3.x families (input + output rates). The `openai` and `google` tables ship **empty on purpose** — a stale price is worse than a visible `0.0`. These are a baseline, not a maintained price feed; verify against each provider's pricing page.

## Adding or correcting rates

Register at startup, without editing the framework:

```python
from pf_core.pricing import ModelRates, register_rates

register_rates("openai", "gpt-4o-mini", ModelRates(input=0.15, output=0.60))
```

Registered rates override a built-in with the same prefix. `ModelRates(input, output, cache_read=None, cache_write=None, cache_write_1h=None)` — rates per 1M tokens; `cache_write` is the 5-minute-TTL write rate, `cache_write_1h` the 1-hour one. `get_rates(provider, model)` returns the resolved `ModelRates` (or `None`) for inspection.

## Client integration

`AnthropicClient.chat()` populates `usage["cost_usd"]` via `estimate_cost`. `OpenRouterClient.chat()` prefers OpenRouter's server-reported `usage.cost` and falls back to `estimate_cost` only when a route doesn't surface one. The Claude Code client reports `0.0` (a Claude Max session has no per-call price). See [cost & budget](cost-budget.md) for using estimates in pre-call budget gating.
