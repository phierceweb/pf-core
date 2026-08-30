# Claude Code Client

Thin wrapper around the local `claude --print` CLI. Implements the same `.chat(messages, model, ...) -> (content, usage)` interface as [`OpenRouterClient`](openrouter.md), so the two are drop-in interchangeable. Uses the machine's active Claude Max session and consumes no API credits.

By default each call is a **plain completion**: no tools, and your `system` messages as the system prompt in place of Claude Code's own agent prompt. That matches what the other backends send and keeps the call to your prompt's tokens (see [Tools and the system prompt](#tools-and-the-system-prompt)). `tools` and `agent_prompt` turn a call back into an agent turn.

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

- **Model** — passed as `--model X` to the CLI. An OpenRouter-style `provider/model` prefix is stripped automatically (e.g. `anthropic/claude-3.7-sonnet` → `claude-3.7-sonnet`) so a legacy single model string in your config still works on this backend. Resolution order, highest wins:
  1. Per-call `chat(model="opus")`
  2. Constructor arg `ClaudeCodeClient(model="haiku")` / `get_client(model="haiku")`
  3. Env var `$PF_CORE_CLAUDE_CODE_MODEL`
  4. No `--model` flag → CLI uses the active interactive session model. For Claude Max users this can silently route batch work onto Sonnet/Opus and chew through quota — pin a model whenever you batch.
- **Temperature / max_tokens / top_p / response_format** — accepted as kwargs for API parity with `OpenRouterClient` but **ignored**. The active Claude Code session controls sampling. Passing them won't error; they just don't reach the CLI.
- **Token counts** — read from the `--output-format json` envelope's `usage` block: `prompt_tokens` ← `input_tokens`, `completion_tokens` ← `output_tokens`, `cache_read_tokens` ← `cache_read_input_tokens`, `cache_write_tokens` ← `cache_creation_input_tokens`. All `0` when the envelope omits them (older CLIs) or in text mode (consumer-supplied `--output-format` / `--verbose`). `reasoning_tokens` is always `0` — the envelope doesn't report it. Real token counts are what make a `tokens` budget limit enforceable on this backend (see [cost-budget.md](cost-budget.md)).
- **Cost** — always `0.0`. Claude Max sessions don't bill per call.
- **Duration** — wall-clock from invocation, in milliseconds.
- **`system_fingerprint`** — always `None`.
- **`finish_reason`** — the model's stop reason, read off the `--output-format json` envelope. Present whenever that envelope reports one (see [Truncation reporting](#truncation-reporting)).
- **Authentication** — the child `claude` process inherits the parent environment with `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` stripped out. This protects the Claude Max session path: a stray key in a project's `.env` would otherwise hijack `claude --print` into billable — and possibly invalid — external API-key auth. All other env vars (PATH, HOME, session config) pass through.
- **Tools** — none by default: the call passes `--tools ""` and `--strict-mcp-config`, so the model has no built-in or MCP tool to call. A prompt that asks the model to use one (read this file, run this command) gets an answer written without it — not an error — so name every tool a prompt relies on: `tools=["Read", "Grep"]`, or `["default"]` for the CLI's full set. See [Tools and the system prompt](#tools-and-the-system-prompt).
- **System prompt** — your `system` messages, in a private temp file passed with `--system-prompt-file`, replace Claude Code's agent prompt; the other messages go on stdin. `agent_prompt=True` keeps Claude Code's prompt and appends yours instead. See [Messages and the system prompt](#messages-and-the-system-prompt).
- **Isolation** — by default (`isolate=True`) the call runs with `--safe-mode`, so `claude --print` starts with all customizations **off**: the surrounding project's `CLAUDE.md`, skills, hooks, plugins, and MCP servers — including ones passed with `--mcp-config` — are not loaded (auth, model, and explicit flags like `--tools` still apply). This is the secure default for a programmatic call, which runs in whatever directory the consumer happens to be in and almost never wants that directory's ambient instructions steering it — without it, a weaker model can be hijacked into obeying an ambient "invoke the relevant skill" instruction and emit skill text where the caller expected a completion. Pass `isolate=False` only when you deliberately run claude inside a project to use that project's customizations (the user's `CLAUDE.md` is then injected even when your system prompt replaces Claude Code's). With `agent_prompt=True`, `--safe-mode` does not remove the CLI's own workspace context: Claude Code's prompt carries the cwd and the repo's git status, and a weak model may answer about them instead of the prompt — run such consumers from a neutral cwd. Requires a `claude` CLI new enough to support `--safe-mode` (Claude Code 2.x); on an older binary the call exits non-zero with `unknown option '--safe-mode'` — set `isolate=False` to fall back.
- **Session persistence** — off by default (`persist_session=False`): the call passes `--no-session-persistence`, so the CLI saves no transcript under `~/.claude/projects/`. Without it a batch consumer leaves one session file per call, crowding real conversations out of transcript tooling. Set `persist_session=True` to keep transcripts while debugging a specific call; a `--resume`, `--continue` or `--session-id` in `extra_args` keeps them too, since a resumable session is what that flag asks for.

## Messages and the system prompt

A chat-message list is split in two:

- All `system` messages, joined with blank lines, become the **system prompt**. They are written to a private temp file (mode `0600`, removed when the call returns, kept across retries) and passed as `--system-prompt-file`, which **replaces** Claude Code's agent prompt. A file rather than `--system-prompt <text>`: argv has an ARG_MAX ceiling and is visible to other users through `ps`. With no `system` message the file is empty — the call still runs without Claude Code's prompt.
- All other messages (`user`, `assistant`, …), joined with blank lines, go on **stdin**.

```python
client.chat(
    messages=[
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "summarize this"},
    ]
)
# Runs, with "be brief" in the temp file:
#   $ echo "summarize this" | claude --safe-mode --no-session-persistence \
#       --tools "" --strict-mcp-config --system-prompt-file /tmp/pf-core-claude-system-….md \
#       --output-format json --print
```

The CLI prepends a one-line agent identity to a replacement prompt ("You are a Claude agent, built on Anthropic's Claude Agent SDK."); your text follows it.

`agent_prompt=True` keeps Claude Code's own prompt and appends the `system` messages to it with `--append-system-prompt-file` (no file at all when there are none).

System-prompt flags in `extra_args`: the CLI accepts one replacement flag and one append flag.

- `--append-system-prompt` / `--append-system-prompt-file` composes with the client's `--system-prompt-file`: your system messages replace Claude Code's prompt and the flag's text is appended.
- `--system-prompt` / `--system-prompt-file` — or an append flag while `agent_prompt=True` holds the one append slot — leaves the client no flag to use. It adds none and flattens the messages onto stdin instead: `system` block, `\n\n---\n\n`, then the rest.

Empty / missing content is skipped. An empty messages list (or one with only `system` content) raises `ClaudeCodeError`.

## Tools and the system prompt

`claude --print` is a full Claude Code agent turn unless told otherwise: it carries Claude Code's own system prompt and tool definitions (about 30k tokens with the default tool set), and a model that has tools uses them — a batch summarizer asked for a word count will run `wc` through Bash, write drafts to files, and re-read its context each turn. The defaults turn that off:

| Default | Flags | Input tokens beyond your prompt, measured on Haiku |
|---|---|---|
| Claude Code's prompt and tools (before v0.23.0) | none | ~30k (26k cache read + 4.7k cache write) |
| no tools | `--tools "" --strict-mcp-config` | ~4k |
| no tools, your system prompt (**default**) | + `--system-prompt-file` | ~250, plus your system prompt |

On a real 77-entry summarization prompt with a 2k-character system prompt (Sonnet), the default took 18k input tokens in one turn where the pre-v0.23.0 call took 57k over several tool turns.

`--tools ""` removes only the built-in tools. Without `--safe-mode` (`isolate=False`) the user's MCP servers would still load theirs; `--strict-mcp-config` (with no `--mcp-config`) stops that. Under the default `isolate=True` no MCP server loads either way.

For an agent call, name the tools:

```python
client = new_client(
    model="sonnet",
    tools=["Read", "Grep", "Glob"],
    extra_args=["--allowedTools", "Read,Grep,Glob"],
)
```

Tool turns are prompt-cached either way: once your system prompt, the tool definitions and the conversation so far pass the model's minimum cacheable length, each turn re-reads the earlier context from cache. Add `agent_prompt=True` when you want Claude Code's own tool-use guidance and workspace context in the prompt — it costs about 5k tokens with one tool enabled and about 30k with the default set, mostly read from cache after the first turn.

- `tools` accepts built-in tool names only; an MCP name (`mcp__…`) raises `ConfigurationError`. MCP tools come from the user's MCP config, which `--safe-mode` switches off entirely, so they need `isolate=False` — then run from a neutral cwd, since the cwd's `CLAUDE.md`, skills and hooks load too. Grant them in `extra_args`: `new_client(isolate=False, tools=["Read"], extra_args=["--allowedTools", "Read,mcp__server__tool"])`. With named `tools` and `isolate=False`, every configured MCP server loads, and each server's tool definitions count as input tokens; `--allowedTools` pre-approves tools, it does not limit which load. For MCP tools and no built-ins, pass `--tools` yourself — `extra_args=["--tools", "", "--allowedTools", "mcp__server__tool"]` with `isolate=False`; a `--tools` in `extra_args` replaces the client's tool flags, `--strict-mcp-config` included.
- Granting tools only through `--allowedTools` / `--allowed-tools` in `extra_args` raises `ConfigurationError` at construction: with `tools=()` those tools would not exist. So do `tools` given as a string, and `tools` together with a `--tools` in `extra_args`.
- **Reasoning effort** is the CLI session's default, and on Sonnet it can dominate a call's output: the 77-entry prompt above produced ~29.5k output tokens for a ~2.5k-token answer in 6 minutes at the default, and ~3k tokens in under a minute with `extra_args=["--effort", "low"]` or `"medium"` — which also covered a few fewer entries. Set it per agent where output quality allows.
- The flags need a Claude Code CLI that supports them (`--no-session-persistence`, `--tools`, `--strict-mcp-config`, `--system-prompt-file`, `--append-system-prompt-file`; verified on 2.1.250). An older binary exits non-zero with `unknown option`.

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
    isolate: bool = True,
    persist_session: bool = False,
    tools: Sequence[str] | None = (),
    agent_prompt: bool = False,
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `timeout` | `int` | `600` | Wall-clock cap (seconds) for one CLI call |
| `binary` | `str` | `"claude"` | Path or name of the executable to run |
| `extra_args` | `list[str]` | `None` | Flags inserted after the client's own (`--safe-mode`, `--no-session-persistence`, tool and system-prompt flags), before `--model` / `--output-format` / `--print` (e.g. `["--allowedTools", "Read"]` with `tools=["Read"]`). A `--tools` here replaces the client's tool flags; a system-prompt flag composes with or replaces the client's — see [Messages and the system prompt](#messages-and-the-system-prompt); `--output-format` or `--verbose` suppresses the client's envelope — see [Truncation reporting](#truncation-reporting) |
| `model` | `str \| None` | `None` | Default model passed as `--model X` on every call. Falls back to `$PF_CORE_CLAUDE_CODE_MODEL`; `None` omits the flag entirely. Per-call `chat(model=...)` overrides for one call. |
| `retry` | `int` | `0` | Auto-retry count for transient failures, sleeping `0.5 * attempt` seconds between attempts. `retry=0` (default) raises on the first failure. `retry=1` makes up to 2 total attempts; `retry=N` makes up to N+1. Both timeout and non-zero exit are retried (transient causes: rate-limit windows, momentary auth refresh, model warm-up). Missing binary and empty messages are NOT retried (deterministic config errors). |
| `isolate` | `bool` | `True` | Run with `--safe-mode` so the call ignores the ambient project's `CLAUDE.md` / skills / hooks / plugins / MCP servers (auth, model, and explicit flags still apply). `True` is the secure default for a programmatic call; set `False` only to deliberately use the surrounding project's customizations. See **Isolation** above. |
| `persist_session` | `bool` | `False` | Save each call as a Claude Code session transcript. `False` passes `--no-session-persistence`, unless `extra_args` resume a session. See **Session persistence** above. |
| `tools` | `Sequence[str] \| None` | `()` | Built-in tools the model may call, passed as `--tools` (`["default"]` for the full set). Empty passes `--tools ""` and `--strict-mcp-config`: no built-in or MCP tools. See [Tools and the system prompt](#tools-and-the-system-prompt). |
| `agent_prompt` | `bool` | `False` | Keep Claude Code's agent system prompt and append the `system` messages to it (`--append-system-prompt-file`). `False` replaces it with them (`--system-prompt-file`). See [Messages and the system prompt](#messages-and-the-system-prompt). |

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

Returns `(content, usage)`. The `usage` dict carries the same token/cost keys as [`OpenRouterClient.chat`](openrouter.md):

```python
{
    "prompt_tokens": <int, from the envelope's usage.input_tokens; 0 without an envelope>,
    "completion_tokens": <int, usage.output_tokens; 0 without an envelope>,
    "cache_read_tokens": <int, usage.cache_read_input_tokens; 0 without an envelope>,
    "cache_write_tokens": <int, usage.cache_creation_input_tokens; 0 without an envelope>,
    "reasoning_tokens": 0,
    "cost_usd": 0.0,
    "duration_ms": <int, wall-clock>,
    "system_fingerprint": None,
    "finish_reason": <str, when the JSON envelope is readable>,
}
```

## Truncation reporting

Text-mode stdout carries the answer and nothing else, so a truncated response is indistinguishable from a complete one. The client therefore adds **`--output-format json`** to the command and reads the CLI's envelope: `result` holds the same text stdout would have carried, and the top-level `stop_reason` is the model's own.

`stop_reason` maps onto the shared `usage["finish_reason"]` vocabulary the other clients use, so [`truncated_from_usage`](llm-parse.md#detecting-truncation-truncated-not-the-text) needs no special case:

| Envelope | `usage["finish_reason"]` |
|---|---|
| `stop_reason` is `max_tokens` or `model_context_window_exceeded` | `"length"` (also logs a `claude_code_truncated` warning) |
| any other `stop_reason` string | that string, verbatim — the CLI types it as a free string, not an enum |
| `stop_reason` null | key omitted — unknown, never guessed |
| `is_error` true | `ClaudeCodeError` raised, quoting the envelope's `result` — an error report is never returned as the answer |

A `--verbose` transcript array unwraps via its final `result` entry.

Passing `--output-format` or `--verbose` in `extra_args` suppresses envelope mode entirely: your flag wins, the client adds none of its own, and stdout is read as plain text with no `finish_reason`. Other `extra_args` (`--allowedTools`, `--tools`) leave envelope mode on.

When the client *did* request the envelope and stdout is still unreadable — not valid JSON, or no string `result` (an older CLI, a wrapper script, a changed schema) — `chat()` raises `ClaudeCodeError` rather than returning machine output as the answer. The error names the remediation: upgrade the CLI, or pass `extra_args=["--output-format", "text"]` to restore text-mode output (truncation then unreportable).

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

This catches the failure mode where a whole batch of parallel `claude --print` calls all error with "Not logged in · Please run /login" — without preflight, only after burning minutes of wall-clock. Preflight surfaces the same condition in ~2 seconds.

## Singleton

```python
from pf_core.clients.claude_code import get_client, new_client, reset_client
```

| Function | Description |
|---|---|
| `get_client(*, timeout=None, binary=None, extra_args=None, model=None, retry=0, isolate=True, persist_session=False, tools=(), agent_prompt=False)` | Shared client per distinct set of arguments: calls with the same args get the same instance, and a call with any other arg — another `model`, `tools`, `extra_args`, `isolate`, `timeout` — gets its own. One caller's tool grant, flags or `isolate=False` never reaches a client another caller asked for, and `get_client()` is always the isolated, tool-less default. |
| `new_client(*, timeout=None, binary=None, extra_args=None, model=None, retry=0, isolate=True, persist_session=False, tools=(), agent_prompt=False)` | Fresh instance with `get_client()`'s defaults but no caching (the model router caches its per-backend `client_kwargs` clients itself, e.g. `client_kwargs: {tools: [Read, Grep], agent_prompt: true}`). |
| `reset_client()` | Drop all cached clients. Useful in tests. |

### Per-task model pinning

A consumer can pin different tasks to different models in the same process — each `get_client(model=X)` call returns its own cached client:

```python
# In your project's clients module:
from pf_core.clients.claude_code import get_client


def get_classifier_client():
    return get_client(model="haiku")  # cheap, fast — fine for short classification


def get_summarizer_client():
    return get_client(model="sonnet")  # smarter — reasoning over longer text
```

Each is cached independently; subsequent calls with the same args return the same instance. Pin via constructor (`get_client(model=...)`), env var (`$PF_CORE_CLAUDE_CODE_MODEL` — applies when no `model` is passed), or per call (`client.chat(messages, model=...)`).

## Errors

`ConfigurationError` is raised at construction for a `tools` / `extra_args` combination that cannot work (see [Tools and the system prompt](#tools-and-the-system-prompt)).

`ClaudeCodeError` (subclass of `pf_core.exceptions.ClientError`) is raised when:

- The `claude` binary is not on `PATH` (or at the configured `binary` path).
- The messages list yields no usable user content.
- `claude --print` returns a non-zero exit code.
- `claude --print` exceeds the wall-clock timeout.

Catching `pf_core.exceptions.ClientError` (or `AppError`) catches every `ClaudeCodeError`; the construction-time `ConfigurationError` is a `FlowException` and is not among them.

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
- **Per-agent model selection works on the Claude Code backend** — pass `model=` per call (or per-instance via `get_client`) to pin different agents to `haiku` vs `sonnet` vs `opus`. Without it the CLI falls through to the active interactive session model.
- **Concurrency**: each `chat()` call spawns a subprocess. The CLI itself may have an internal session lock; if you see serialized calls despite parallel workers, that's why.

## See also

- [openrouter.md](openrouter.md) — the paid HTTP backend.
- [parallel.md](parallel.md) — parallel batch execution.
- [project-portability.md](project-portability.md) — keeping per-agent routing decisions in your project's config layer.
