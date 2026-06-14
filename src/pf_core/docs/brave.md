# `pf_core.clients.brave`

HTTP client for the Brave Web Search API. A real search backend with a real index — useful as an alternative or fallback to LLM-with-search agents (Perplexity, etc.) which can hallucinate URLs from training data.

## Why use Brave (vs LLM search)

LLM-with-search hybrids (Perplexity Sonar, etc.) synthesize their output from retrieval signals. They can confidently emit URLs to articles that never existed, especially when retrieval came up empty and the model didn't want to return nothing. Bolt-on URL verifiers catch some fabrications post-hoc, but the better architecture constrains the *input*: real search API → real URLs → fetch → LLM extracts from the fetched content.

Brave is the cheapest reasonable option for that pattern:

- ~$0.005/call on the paid tier (vs ~$0.05–$0.15 for LLM search calls)
- Free tier: 2,000 calls/month, 1 query/second
- Structured JSON results — no source-block parser needed
- Explicit pricing — easy to budget against

The pattern: **`brave.search()` → list of real URLs → `article_fetch.fetch_article()` → article text → LLM extract**. URLs come from Brave's index; LLM never invents them.

## Installation

Brave API access requires a subscription token from [brave.com/search/api](https://brave.com/search/api/). Set:

```bash
BRAVE_API_KEY=<your-token>
```

Requires the `[llm]` extra (it ships the `httpx` transport this client uses): `pip install 'pf-core[llm]'`. Importing `pf_core.clients.brave` without it raises a friendly `ImportError` naming the extra.

## Usage

### One-shot

```python
from pf_core.clients.brave import get_client

client = get_client()  # reads BRAVE_API_KEY from env

results, usage = client.search("james webb telescope latest images", count=5)

for r in results:
    print(r["url"])
    print(r["title"])
    print(r["description"])
    print(r["page_age"])  # ISO date if Brave knows publish date
```

### With recency filter

```python
results, _ = client.search(
    "new graphics card launch",
    freshness="pw",     # past week
    count=10,
)

# Or absolute range:
results, _ = client.search(
    "march madness bracket results",
    freshness="2025-03-01to2025-03-31",
)
```

### Forwarding usage into an agent-run logger

The `usage` dict carries `cost_usd`, `duration_ms`, `prompt_tokens`, and `completion_tokens` (the token counts are always `0` — Brave bills per-call). Those are the keys an agent-run logger expects, so it forwards directly:

```python
results, usage = client.search(query)

db.log_agent_run(
    job_id, "brave_searcher", "brave-web-search-v1",
    cost_usd=usage["cost_usd"],
    duration_ms=usage["duration_ms"],
    prompt_tokens=usage["prompt_tokens"],       # always 0
    completion_tokens=usage["completion_tokens"], # always 0
    status="success",
)
```

### Custom cost rate

If your account is on a different pricing tier, set the rate in env or on the client:

```python
client = get_client(cost_per_call_usd=0.003)

# Or via env:
# BRAVE_COST_PER_CALL_USD=0.003
```

## Result shape

Each result dict has:

| Key | Type | Meaning |
|---|---|---|
| `url` | `str` | The result URL |
| `title` | `str` | Page title |
| `description` | `str` | Snippet (may contain HTML highlight tags `<strong>`) |
| `age` | `str | None` | Human-readable, e.g. "2 days ago" |
| `page_age` | `str | None` | ISO-8601 publish date if Brave knows it |

## Errors

Every failure raises `BraveSearchError` (subclass of `pf_core.exceptions.ClientError`):

- Empty query
- Network timeout (after `request_timeout` seconds, no retry)
- Transport error (DNS, connection refused, etc.)
- HTTP 401/403 → "auth failed"
- HTTP 429 → "rate-limited"
- Other non-2xx → wrapped with status code
- Non-JSON response body

Callers should catch and treat as a hard failure — the client doesn't retry automatically. If you need retries (transient 5xx, network flakes), wrap with `tenacity` at the call site:

```python
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)
from pf_core.clients.brave import get_client, BraveSearchError

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=2, max=30),
    retry=retry_if_exception_type(BraveSearchError),
)
def search_with_retry(query):
    return get_client().search(query)
```

## Rate limiting

Brave free tier is **1 query per second**. The client never auto-sleeps — callers manage QPS. For batched workloads, either:

1. Use the paid tier (higher QPS).
2. Add a `time.sleep(1)` between calls in your loop.
3. Use a token bucket (e.g. `pf_core.utils.ratelimit` if available, or `pyrate-limiter`).

## Env vars

| Var | Default | Purpose |
|---|---|---|
| `BRAVE_API_KEY` | — (required) | Subscription token |
| `BRAVE_BASE_URL` | `https://api.search.brave.com/res/v1` | API base URL |
| `BRAVE_REQUEST_TIMEOUT` | `30` | Per-request socket timeout (seconds) |
| `BRAVE_COST_PER_CALL_USD` | `0.005` | Logged cost per call |

## See also

- `pf_core.utils.article_fetch` — the natural pairing: Brave gives you URLs, article_fetch turns them into structured content
