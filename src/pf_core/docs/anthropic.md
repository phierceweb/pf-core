# Anthropic Client

Wraps the official `anthropic` Python SDK's `messages.create()` with pf-core's `(content, usage)` return convention. Implements the same `.chat(messages, model, ...) -> (content, usage)` interface as [`OpenRouterClient`](openrouter.md) and [`ClaudeCodeClient`](claude-code.md), so the three are drop-in interchangeable.

The Anthropic backend is one of the three built-in backends in the [model router](model-router.md#client-registry) — useful when you want the official SDK's vision support and direct usage / cache-token reporting (rather than going through OpenRouter, which charges a markup, or Claude Code, which uses a Claude Max session and has no per-call cost / token reporting).

## Install

Optional dependency — install the extra:

```bash
pip install 'pf-core[anthropic]'
```

Pulls in the `anthropic` SDK (`>=0.30`).

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
| `retry` | `int` | `0` | Auto-retry count on transient failures. Layered on top of the SDK's own internal retries — pf-core retries kick in once the SDK has exhausted its own. Validation errors (no model specified) are NOT retried. |

#### chat

```python
client.chat(
    messages: list[dict],
    model: str = "",
    temperature: float | None = 0.2,      # pass None to omit (reasoning models)
    max_tokens: int = 4096,
    top_p: float | None = 1.0,            # pass None to omit (reasoning models)
    response_format: dict | None = None,  # ignored
    timeout: int | None = None,           # per-call override (honored)
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

`cost_usd` is a best-effort estimate from [`pf_core.pricing`](pricing.md): the model id is matched against a prefix pricing table (`claude-opus-4`, `claude-sonnet-4`, `claude-haiku-4`, plus legacy 3.x families) using input + output rates per 1M tokens. Cache-read/write discounts and batch pricing are NOT modeled. A model id matching no prefix yields `cost_usd == 0.0` and a one-shot `pricing_unknown_model` WARNING — callers can treat `0.0` as "unpriced". Add or correct rates with `pf_core.pricing.register_rates(...)`, no framework edit needed.

`response_format` is accepted for API parity with OpenRouter but ignored — Anthropic has no direct equivalent. Use Anthropic's documented JSON-output techniques (tool-use, ` ```json ` framing in the prompt) instead.

`timeout` (per-call) IS honored — overrides the constructor-time timeout for one call via the SDK's `with_options(timeout=N)` derived-client pattern.

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

`retry=N` on the constructor / `get_client()` enables auto-retry on any `Exception` from the SDK. The SDK has its own internal retry on transient HTTP failures; pf-core retry is layered on top and kicks in once the SDK has exhausted its own. Validation errors (no model specified) raise immediately — retry won't help when the input is wrong.

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
