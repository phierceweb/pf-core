# OpenRouter Client

Thin wrapper around the OpenRouter chat completions API (OpenAI-compatible). Provides timeout management, provider routing, usage tracking, and Perplexity citation handling.

## Quick start

```python
from pf_core.clients.openrouter import get_client

client = get_client()
content, usage = client.chat(
    messages=[{"role": "user", "content": "Hello"}],
    model="anthropic/claude-sonnet-4.6",
)
```

`get_client()` is a module-level singleton — reads env vars on first call, returns the cached instance after that. For a fresh, independently-configured instance (different timeout, retry, etc. in the same process), use `new_client()`, which has the same env-var resolution but no caching. `get_client()` is a caching wrapper over `new_client()`.

## Configuration

Set via environment variables or pass explicitly to `get_client()`:

| Env var | Default | Description |
|---------|---------|-------------|
| `OPENROUTER_API_KEY` | (required) | API key |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | API base URL |
| `APP_NAME` | `""` | Sent as `X-Title` header |
| `APP_URL` | `""` | Sent as `HTTP-Referer` header |
| `OPENROUTER_PROVIDER_IGNORE` | `""` | Comma-separated providers to exclude |
| `REQUEST_TIMEOUT` | `120` | Per-request socket timeout (seconds) |

Plus a constructor / `get_client()` kwarg:

| Kwarg | Default | Description |
|---|---|---|
| `retry` | `0` | Auto-retry count for transient failures (timeout, 429, 5xx). `retry=0` raises on the first failure. `retry=1` makes up to 2 total attempts. 4xx other than 429 are caller errors and NOT retried. |

## chat()

```python
content, usage = client.chat(
    messages=[
        {"role": "system", "content": "You are a research assistant."},
        {"role": "user", "content": "Summarize this document."},
    ],
    model="anthropic/claude-sonnet-4.6",
    temperature=0.2,       # default 0.2
    max_tokens=4096,       # default 4096
    top_p=1.0,             # default 1.0
    response_format=None,  # optional structured output format
    timeout=None,          # override per-request timeout
)
```

### Return values

**content** (`str`): The assistant's response text. For Perplexity models, citation URLs are appended automatically:

```
The policy was enacted in January 2025.

CITATIONS:
[1] https://example.com/article1
[2] https://example.com/article2
```

**usage** (`dict`): Token counts and cost. Carries the same key set as [`AnthropicClient.chat`](anthropic.md) and [`ClaudeCodeClient.chat`](claude-code.md):

```python
{
    "prompt_tokens": 1200,
    "completion_tokens": 450,
    "cache_read_tokens": 0,       # from prompt_tokens_details.cached_tokens when present
    "cache_write_tokens": 0,
    "reasoning_tokens": 0,        # from completion_tokens_details.reasoning_tokens
    "cost_usd": 0.0023,           # from OpenRouter's reported `cost` field
    "duration_ms": 3400,
    "system_fingerprint": None,
}
```

`cost_usd` is OpenRouter's own reported cost for the call (the `usage.cost` field) — not a local estimate.

## Provider routing

Exclude unreliable providers:

```bash
# In .env
OPENROUTER_PROVIDER_IGNORE=Together,DeepInfra
```

Or via code:

```python
client = OpenRouterClient(
    api_key="...",
    provider_ignore=["Together", "DeepInfra"],
)
```

The ignore list is merged with any per-request `provider` config passed in `**kwargs`.

## Preflight check

Before launching a long batch of calls, run `client.preflight()` to catch a missing API key or unreachable host in single-digit seconds — hits the cheap `GET /models` endpoint instead of burning an LLM call.

```python
from pf_core.clients.openrouter import get_client, OpenRouterError

client = get_client()
try:
    client.preflight()
except OpenRouterError as e:
    # Message names OPENROUTER_API_KEY for 401/403; carries
    # context["preflight"] = True so log filters can distinguish
    # preflight failures from per-call failures.
    print(f"Cannot start batch: {e}")
    sys.exit(1)
```

`preflight(timeout=N)` overrides the default 30-second timeout. Raises `OpenRouterError` on any failure — auth (401/403), unreachable host, network timeout, 5xx server error.

## Retry on transient failure

`retry=N` on the constructor / `get_client()` enables auto-retry on transient failures. Retried: `httpx.TimeoutException`, status 429 (rate limit), status 5xx. NOT retried: 4xx other than 429 (caller errors that won't get better on the next try).

```python
client = get_client(retry=2)  # up to 3 total attempts
content, usage = client.chat(messages, model="...")
# Each retry logs warning event openrouter_retry_status / _retry_timeout
# with attempt count, status, and body head.
```

## Timeout handling

`request_timeout` is passed straight to `httpx.post(..., timeout=...)` — it governs connect, read, write, and pool waits. A timeout raises `OpenRouterError` with the model and timeout in `context`.

There is no separate wall-clock cap. Python threads can't be cancelled, so a sync wrapper around the call could only notify-early, not actually stop the request. If a true wall-clock deadline matters for your workload, run the call inside `asyncio.timeout(...)` against `httpx.AsyncClient`.

## Error handling

All errors raise `OpenRouterError` (a subclass of `ClientError` → `AppError`):

```python
from pf_core.clients.openrouter import OpenRouterError

try:
    content, usage = client.chat(messages, model=model)
except OpenRouterError as e:
    # e.context has {"model": "...", "status_code": 429, ...}
    log_exception(e)
```

## Multiple clients

For projects that need different configurations (e.g. two agents with different timeouts), use `new_client()` — it bypasses the singleton cache and reads any unset args from env:

```python
from pf_core.clients.openrouter import new_client

search_client = new_client(request_timeout=60)
summary_client = new_client(request_timeout=120)
```

Or construct `OpenRouterClient` directly when you want to pass every arg explicitly:

```python
from pf_core.clients.openrouter import OpenRouterClient

search_client = OpenRouterClient(
    api_key=cfg.OPENROUTER_API_KEY,
    request_timeout=60,
)
```

## Testing

### Reset the singleton

```python
from pf_core.clients.openrouter import reset_client

# Reset the singleton between tests
reset_client()
```

### Injecting a fake via the ChatClient protocol

For unit tests that shouldn't hit OpenRouter at all, type-annotate against `pf_core.clients.ChatClient` and inject a fake. No inheritance needed — any object with a matching `chat()` method satisfies the protocol.

```python
from pf_core.clients import ChatClient

class SummaryService:
    def __init__(self, llm: ChatClient) -> None:
        self._llm = llm

    def summarize(self, text: str) -> str:
        content, _usage = self._llm.chat(
            messages=[{"role": "user", "content": f"Summarize: {text}"}],
            model="anthropic/claude-sonnet-4.6",
        )
        return content
```

In production, pass the real client:

```python
from pf_core.clients.openrouter import get_client

service = SummaryService(llm=get_client())
```

In tests, pass a fake:

```python
class FakeChatClient:
    def chat(self, messages, model, **kwargs):
        return "fake summary", {
            "prompt_tokens": 10, "completion_tokens": 5,
            "cost_usd": 0.0, "duration_ms": 1,
        }

def test_summarize():
    service = SummaryService(llm=FakeChatClient())
    assert service.summarize("hello") == "fake summary"
```

The `ChatClient` protocol is `@runtime_checkable`, so `isinstance(fake, ChatClient)` works if you need it — but prefer static type-checking over runtime checks.
