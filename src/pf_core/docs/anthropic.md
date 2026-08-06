# Anthropic Client

Wraps the official `anthropic` Python SDK's `messages.create()` with pf-core's `(content, usage)` return convention. Implements the same `.chat(messages, model, ...) -> (content, usage)` interface as [`OpenRouterClient`](openrouter.md) and [`ClaudeCodeClient`](claude-code.md), so the three are drop-in interchangeable.

The Anthropic backend is one of the three built-in backends in the [model router](model-router.md#client-registry) — useful when you want the official SDK's vision support and direct usage / cache-token reporting (rather than going through OpenRouter, which charges a markup, or Claude Code, which uses a Claude Max session and has no per-call cost / token reporting).

## Install

Optional dependency — install the extra:

```bash
pip install 'pf-core[anthropic]'
```

Pulls in the `anthropic` SDK (`>=0.105`, for structured-outputs support).

## Usage

```python
from pf_core.clients.anthropic import get_client

client = get_client(model="claude-haiku-4-5-20251001")
content, usage = client.chat(
    messages=[
        {"role": "user", "content": "Hello"},
    ],
)
```

## Multimodal (vision)

Pass Anthropic-format content blocks in the messages list. The wrapper forwards the `messages` payload to the SDK as-is — no validation or transformation:

```python
content, usage = client.chat(
    messages=[
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": image_base64,
                    },
                },
                {"type": "text", "text": "What's in this image?"},
            ],
        }
    ],
    model="claude-haiku-4-5-20251001",
)
```

OpenRouter and Claude Code don't take this exact payload shape, so a service that wants to stay backend-portable should either pass simpler text-only messages or fork the call site by backend.

## Structured output (`response_format`)

`chat(response_format=...)` accepts the OpenAI/OpenRouter shapes and maps them onto Anthropic-native features:

| You pass | What happens |
|---|---|
| `{"type": "json_schema", "schema": {...}}` — or the OpenAI nesting `{"type": "json_schema", "json_schema": {"schema": {...}}}` | Native structured outputs: `output_config={"format": {"type": "json_schema", "schema": ...}}`. Response text is API-guaranteed valid JSON matching the schema. |
| `{"type": "json_object"}` | No native equivalent (Anthropic schemas require `additionalProperties: false`, so a permissive object schema is inexpressible). Enforced by appending a JSON-only instruction to the system prompt; logged once per process (`anthropic_json_object_prompt_enforced`). Pair with `pf_core.llm.parse` downstream. |
| Any other `type` | Warned once (`anthropic_response_format_ignored`), then ignored. |

Schema restrictions (API-side): every object needs `additionalProperties: false`; no recursive schemas; no numeric/string bounds (`minimum`, `maxLength`, …). A refusal (`stop_reason: "refusal"`) or `max_tokens` truncation can still yield non-conforming output — keep `pf_core.llm.validate` as the downstream net. First use of a new schema pays a one-time server-side compilation cost (cached 24h).

## System messages and prompt caching

Leading `{"role": "system", ...}` messages are extracted into the API's top-level `system=` parameter (Anthropic rejects a system role inside `messages`), so OpenAI-style message lists work unchanged across all three transports.

Prompt caching is opt-in per call:

```python
content, usage = client.chat(
    messages=[{"role": "system", "content": BIG_SYSTEM}, {"role": "user", "content": q}],
    cache_system=True,   # mark the system prompt as a cache breakpoint
    cache_ttl="5m",      # or "1h" (2x write premium, longer reuse window)
)
```

- Caches tools+system as the stable prefix — the right shape for batch runs (shared system prompt, varying user content). Cache reads bill ~0.1x the input rate; writes ~1.25x (`5m`) / 2x (`1h`).
- Verify via the returned usage dict: `cache_write_tokens` on the first call, `cache_read_tokens` on later calls inside the TTL. `usage["cost_usd"]` includes cache read/write costs — the built-in Anthropic rates carry cache pricing (read 0.1x input; write 1.25x input for `5m`, 2x for `1h`), and the call's `cache_ttl` selects the write rate. Override per model with `pf_core.pricing.register_rates()`.
- Silently no-ops below the model-dependent minimum prefix (1024–4096 tokens). Any byte change before the breakpoint invalidates the cache.
- `cache_system=True` with no system message is a no-op — safe to set per-agent in router config across agents with and without system prompts.

## Class

### AnthropicClient

```python
AnthropicClient(
    *,
    api_key: str,                 # required
    model: str | None = None,
    request_timeout: int = 120,
    retry: int = 0,
)
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `api_key` | `str` | required | Anthropic API key |
| `model` | `str \| None` | `None` | Default model passed on every call. Per-call `chat(model=...)` overrides this. If neither is set, `chat()` raises `AnthropicError` |
| `request_timeout` | `int` | `120` | Per-request socket timeout in seconds (set on the SDK client at construction time). Per-call `chat(timeout=N)` overrides for one call via the SDK's `with_options(timeout=N)` derived-client. |
| `retry` | `int` | `0` | Auto-retry count on transient failures, with backoff between attempts. The SDK's own retries are disabled (`max_retries=0`) so this is the only retry loop. Deterministic failures (4xx other than 408/429, validation errors, read timeouts) are NOT retried — see **Retry on transient failure** below. |

#### chat

```python
client.chat(
    messages: list[dict],
    model: str = "",
    temperature: float | None = 0.2,      # pass None to omit (reasoning models)
    max_tokens: int = 4096,
    top_p: float | None = 1.0,            # pass None to omit (reasoning models)
    response_format: dict | None = None,  # json_schema / json_object — see below
    timeout: int | None = None,           # per-call override (honored)
    cache_system: bool = False,           # opt in to an Anthropic prompt-cache breakpoint
    cache_ttl: str = "5m",                # "5m" or "1h"
    **kwargs: Any,                         # forwarded to SDK
) -> tuple[str, dict]
```

`temperature` and `top_p` are sent only when non-`None`. Pass `None` for either to omit it from the request — needed for reasoning models (Opus 4.7+) that reject these params. Per-model sampling knobs belong in the caller's config (the consumer's `model_router.yaml`), not hardcoded.

Returns `(content, usage)`. `content` is the concatenation of all text blocks in the response (non-text blocks like tool_use are skipped — callers needing them should call the SDK directly). `usage` carries the same key set as [`OpenRouterClient.chat`](openrouter.md):

```python
{
    "prompt_tokens": <int, from response.usage.input_tokens>,
    "completion_tokens": <int, from response.usage.output_tokens>,
    "cache_read_tokens": <int, from response.usage.cache_read_input_tokens>,
    "cache_write_tokens": <int, from response.usage.cache_creation_input_tokens>,
    "reasoning_tokens": <int, from response.usage.thinking_tokens or 0>,
    "cost_usd": <float, estimated from the model's pricing prefix>,
    "duration_ms": <int, wall-clock>,
    "system_fingerprint": None,
}
```

`reasoning_tokens` is populated from `response.usage.thinking_tokens` for reasoning models (Opus 4.7+); older SDK responses lack the field and it falls back to `0`. These tokens are billed at the output rate and the SDK already counts them inside `output_tokens`, so the cost estimate does not add them a second time.

`cost_usd` is a best-effort estimate from [`pf_core.pricing`](pricing.md): the model id is matched against a prefix pricing table (`claude-opus-4`, `claude-sonnet-4`, `claude-haiku-4`, plus legacy 3.x families) using input + output rates per 1M tokens. Cache-read/write costs are modeled TTL-aware via the built-in cache rates — see the [prompt-caching section](#system-messages-and-prompt-caching); batch pricing is not modeled. A model id matching no prefix yields `cost_usd == 0.0` and a one-shot `pricing_unknown_model` WARNING — callers can treat `0.0` as "unpriced". Add or correct rates with `pf_core.pricing.register_rates(...)`, no framework edit needed.

`response_format` maps onto Anthropic-native features — see [Structured output](#structured-output-response_format) below.

`timeout` (per-call) IS honored — overrides the constructor-time timeout for one call via the SDK's `with_options(timeout=N)` derived-client pattern.

`cache_system` / `cache_ttl` opt into Anthropic prompt caching — see [System messages and prompt caching](#system-messages-and-prompt-caching) below.

## Preflight check

Before launching a long batch of calls, run `client.preflight()` to catch a missing API key or expired credential in single-digit seconds — hits the cheap `models.list()` endpoint instead of burning an LLM call.

```python
from pf_core.clients.anthropic import get_client, AnthropicError

client = get_client()
try:
    client.preflight()
except AnthropicError as e:
    # Message names ANTHROPIC_API_KEY; carries context["preflight"] = True
    # so log filters can distinguish preflight from per-call failures.
    print(f"Cannot start batch: {e}")
    sys.exit(1)
```

`preflight(timeout=N)` overrides the default 30-second timeout via `with_options(timeout=N)`. Raises `AnthropicError` on any SDK failure during the smoke call.

## Retry on transient failure

`retry=N` on the constructor / `get_client()` enables auto-retry, sleeping `0.5 * attempt` seconds between attempts. The SDK client is constructed with `max_retries=0`: pf-core owns the only retry loop, so `retry=2` means at most 3 HTTP calls, not 3 × 3.

Only failures that can plausibly succeed on a re-send are retried:

| Failure | Retried? | Why |
|---|---|---|
| 408, 429, 500, 502, 503, 504, 529 (Anthropic's "overloaded") | yes | Transient server/rate-limit state |
| Other 4xx and 5xx (400, 401, 403, 404, 409, 422, 501, 505, 507, …) | no | Deterministic — fails identically every attempt, burning budget |
| Connect / pool timeout, connection error | yes | Request never reached the model |
| Read or write timeout | no | The request was on the wire; a completion may already be generated **and billed**, so a retry pays for the same prompt twice |
| Validation errors (no model specified) | no | Raised before any request |
| Anything else (non-SDK exception) | yes | Unclassifiable — assumed transient |

```python
client = get_client(retry=2)  # up to 3 total attempts
content, usage = client.chat(messages, model="claude-haiku-4-5-20251001")
# Each retry logs warning event anthropic_retry with attempt count.
```

## Singleton

```python
from pf_core.clients.anthropic import get_client, new_client, reset_client
```

| Function | Description |
|---|---|
| `get_client(*, api_key=None, model=None, request_timeout=None, retry=0)` | Module-level singleton. First call's args win; later calls return the cached instance. On first call, reads `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, and `REQUEST_TIMEOUT` from env if not provided. A caching wrapper over `new_client()`. |
| `new_client(*, api_key=None, model=None, request_timeout=None, retry=0)` | Fresh instance with the same env-var resolution as `get_client()`, but no caching. The escape hatch when different agents need differently-tuned clients in one process (also used by the model router's per-backend `client_kwargs`). |
| `reset_client()` | Drop the singleton. Useful in tests. |

The singleton requires `ANTHROPIC_API_KEY` to be set somewhere (env var or first-call kwarg) and raises `AnthropicError` if it's missing.

## Errors

`AnthropicError` (subclass of `pf_core.exceptions.ClientError`) wraps any failure in the SDK call, the API call, or constructor validation. Carries `context={"model": ...}` for diagnostic logging via [`log_exception`](logging.md).

The constructor raises `ImportError` (not `AnthropicError`) when the `anthropic` SDK isn't installed — that's a config problem, not a runtime client error. The error message names the install command: `pip install 'pf-core[anthropic]'`.

## See also

- [`openrouter.md`](openrouter.md) — paid HTTP transport across many providers
- [`claude-code.md`](claude-code.md) — local Claude Max subprocess (free, no token counts)
- [`exceptions.md`](exceptions.md) — `ClientError` and the framework's error hierarchy
