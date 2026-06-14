# Claude Code Client

Thin wrapper around the local `claude --print` CLI. Implements the same `.chat(messages, model, ...) -> (content, usage)` interface as [`OpenRouterClient`](openrouter.md), so the two are drop-in interchangeable. Uses the machine's active Claude Max session and consumes no API credits.

Pair with the [model router](#routing) (`resolve_agent`) to flip a single env var and swap an agent's backend without touching call-site code.

## Usage

```python
from pf_core.clients.claude_code import get_client

# Pin to haiku for batch work — protects Claude Max quota.
client = get_client(model="haiku")
content, usage = client.chat(
    messages=[
        {"role": "system", "content": "You are a summarizer."},
        {"role": "user", "content": "Summarize this..."},
    ],
)
```

## What it does (and doesn't) honor

- **Model** — honored as of v0.22. Passed as `--model X` to the CLI. An OpenRouter-style `provider/model` prefix is stripped automatically (e.g. `anthropic/claude-3.7-sonnet` → `claude-3.7-sonnet`) so a legacy single model string in your config still works on this backend. Resolution order, highest wins:
  1. Per-call `chat(model="opus")`
  2. Constructor arg `ClaudeCodeClient(model="haiku")` / `get_client(model="haiku")`
  3. Env var `$PF_CORE_CLAUDE_CODE_MODEL`
  4. No `--model` flag → CLI uses the active interactive session model (the pre-v0.22 behavior). For Claude Max users this can silently route batch work onto Sonnet/Opus and chew through quota — pin a model whenever you batch.
- **Temperature / max_tokens / top_p / response_format** — accepted as kwargs for API parity with `OpenRouterClient` but **ignored**. The active Claude Code session controls sampling. Passing them won't error; they just don't reach the CLI.
- **Token counts** — always `0`. The CLI doesn't expose them.
- **Cost** — always `0.0`. Claude Max sessions don't bill per call.
- **Duration** — wall-clock from invocation, in milliseconds.
- **`system_fingerprint`** — always `None`.
- **Authentication** — the child `claude` process inherits the parent environment with `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` stripped out (as of v0.35). This protects the Claude Max session path: a stray key in a project's `.env` would otherwise hijack `claude --print` into billable — and possibly invalid — external API-key auth. All other env vars (PATH, HOME, session config) pass through.

## Message flattening

Chat-message lists collapse into a single prompt:

- All `system` messages are joined with blank lines.
- All non-system messages (`user`, `assistant`, …) are joined with blank lines.
- The two blocks are separated by `\n\n---\n\n`.

```python
client.chat(messages=[
    {"role": "system", "content": "be brief"},
    {"role": "user", "content": "summarize this"},
])
# Becomes (prompt piped on stdin, not in argv — argv has a hard ARG_MAX
# limit that large rendered prompts blow past):
#   $ echo "be brief\n\n---\n\nsummarize this" | claude --print
```

Empty / missing content is skipped. An empty messages list (or one with only empty-content messages) raises `ClaudeCodeError`.

## Class

### ClaudeCodeClient

```python
ClaudeCodeClient(
    *,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,  # 600
    binary: str = "claude",
    extra_args: list[str] | None = None,
    model: str | None = None,
    retry: int = 0,
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `timeout` | `int` | `600` | Wall-clock cap (seconds) for one CLI call |
| `binary` | `str` | `"claude"` | Path or name of the executable to run |
| `extra_args` | `list[str]` | `None` | Flags inserted **before** `--model` / `--print` (e.g. `["--allowedTools", "Bash"]`) |
| `model` | `str \| None` | `None` | Default model passed as `--model X` on every call. Falls back to `$PF_CORE_CLAUDE_CODE_MODEL`; `None` omits the flag entirely. Per-call `chat(model=...)` overrides for one call. |
| `retry` | `int` | `0` | Auto-retry count for transient failures. `retry=0` (default) raises on the first failure. `retry=1` makes up to 2 total attempts; `retry=N` makes up to N+1. Both timeout and non-zero exit are retried (transient causes: rate-limit windows, momentary auth refresh, model warm-up). Missing binary and empty messages are NOT retried (deterministic config errors). |

#### chat

```python
client.chat(
    messages: list[dict],
    model: str = "",
    temperature: float = 0.2,
    max_tokens: int = 4096,
    top_p: float = 1.0,
    response_format: dict | None = None,
    timeout: int | None = None,  # per-call override
    **kwargs: Any,
) -> tuple[str, dict]
```

Returns `(content, usage)`. The `usage` dict carries the same keys as [`OpenRouterClient.chat`](openrouter.md):

```python
{
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "cache_read_tokens": 0,
    "cache_write_tokens": 0,
    "reasoning_tokens": 0,
    "cost_usd": 0.0,
    "duration_ms": <int, wall-clock>,
    "system_fingerprint": None,
}
```

## Preflight check

Before launching a long batch of calls, run `client.preflight()` to catch a logged-out session in single-digit seconds — instead of after N failed subprocess invocations.

```python
from pf_core.clients.claude_code import get_client, ClaudeCodeError

client = get_client(model="haiku")
try:
    client.preflight()
except ClaudeCodeError as e:
    # Message names the binary and includes `<binary> /login`
    # remediation. The exception's `context["preflight"]` is True so
    # log filters can distinguish preflight failures from per-call
    # failures.
    print(f"Cannot start batch: {e}")
    sys.exit(1)

# Safe to fan out:
results = run_parallel(items, lambda x: client.chat(...))
```

`preflight()` issues one `claude --print "ok"` against the configured binary and model, with a 30-second default timeout (override via `preflight(timeout=...)`). On any failure — auth, missing binary, timeout, non-zero exit — it raises `ClaudeCodeError` with an actionable message. On success it logs `claude_code_preflight_ok` and returns `None`.

This catches the failure mode that originally prompted the helper: 1180 parallel `claude --print` calls all erroring with "Not logged in · Please run /login" after burning ~10 minutes of wall-clock. Preflight surfaces the same condition in ~2 seconds.

## Singleton

```python
from pf_core.clients.claude_code import get_client, new_client, reset_client
```

| Function | Description |
|---|---|
| `get_client(*, timeout=None, binary=None, extra_args=None, model=None, retry=0)` | Per-model singleton. The cache is keyed on `model`, so `get_client(model="haiku")` and `get_client(model="sonnet")` return distinct instances; `get_client()` is its own slot under the key `None`. For each cache slot the first call's other args (timeout / binary / extra_args / retry) win. |
| `new_client(*, timeout=None, binary=None, extra_args=None, model=None, retry=0)` | Fresh instance with `get_client()`'s defaults but no caching. The escape hatch when different agents need differently-tuned clients (timeout, retry) in one process (also used by the model router's per-backend `client_kwargs`). |
| `reset_client()` | Drop all cached per-model singletons. Useful in tests. |

### Per-task model pinning

A consumer can pin different tasks to different models in the same process — each `get_client(model=X)` call returns its own cached client:

```python
# In your project's clients module:
from pf_core.clients.claude_code import get_client

def get_classifier_client():
    return get_client(model="haiku")    # cheap, fast — fine for short classification

def get_summarizer_client():
    return get_client(model="sonnet")   # smarter — reasoning over longer text
```

Each is cached independently; subsequent calls return the same per-model instance. Pin via constructor (`get_client(model=...)`), env var (`$PF_CORE_CLAUDE_CODE_MODEL` — applies to the no-model slot), or per call (`client.chat(messages, model=...)`).

## Errors

`ClaudeCodeError` (subclass of `pf_core.exceptions.AppError`) is raised when:

- The `claude` binary is not on `PATH` (or at the configured `binary` path).
- The messages list yields no usable user content.
- `claude --print` returns a non-zero exit code.
- `claude --print` exceeds the wall-clock timeout.

Catching `pf_core.exceptions.AppError` will catch all of these.

## Routing

Per-agent backend selection lives in `model_router.yaml` — declare a `claude_code` backend on the agent and let `pf_core.llm.router.resolve_agent` dispatch (see [model-router.md](model-router.md)):

```yaml
agents:
  summarizer:
    default_backend: claude_code
    backends:
      claude_code: {model: sonnet}
      openrouter:  {model: anthropic/claude-sonnet-4.6}
```

```python
from pf_core.llm.router import resolve_agent

client, cfg, backend = resolve_agent("summarizer")
content, usage = client.chat(messages=msgs, **cfg)
```

All backends satisfy the `pf_core.clients.ChatClient` protocol — same `.chat()` signature, same `usage` dict shape — so the service code doesn't change when the YAML flips the backend.

Each backend entry declares its own model string in its own format (claude_code accepts aliases like `sonnet` or bare ids; the client also strips a `provider/` prefix at cmd-build time for legacy single-string configs). The Claude Code module is imported **lazily** — only when an agent actually routes to it — so consumers that never opt in don't need the `claude` CLI installed and don't pay any import cost for it.

`pf_core.clients.routing.get_routed_client(use_claude_code: bool)` — the old boolean dispatch — is **deprecated** (removed in v1.0): it baked in OpenRouter as the silent default. Use `resolve_agent`, or `pf_core.clients.routing.get_client_for_backend("claude_code")` for direct acquisition.

### Practical caveats

- **Throughput**: subprocess invocation is much slower per call than an HTTP request. A `claude --print` call takes 5–15× longer than the same call against OpenRouter. With per-agent parallelism (e.g. via [`pf_core.parallel.run_parallel`](parallel.md)) you can mostly mask this, but expect longer wall times even at `-j 4`.
- **Per-agent model selection works on the Claude Code backend** as of v0.22 — pass `model=` per call (or per-instance via `get_client`) to pin different agents to `haiku` vs `sonnet` vs `opus`. Without it the CLI falls through to the active interactive session model.
- **Concurrency**: each `chat()` call spawns a subprocess. The CLI itself may have an internal session lock; if you see serialized calls despite parallel workers, that's why.

## See also

- [openrouter.md](openrouter.md) — the paid HTTP backend.
- [parallel.md](parallel.md) — parallel batch execution.
- [project-portability.md](project-portability.md) — keeping per-agent routing decisions in your project's config layer.
