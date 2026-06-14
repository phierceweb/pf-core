# Model Router

Per-agent model, sampling, **and backend** config loaded from a YAML file, with a TTL cache so operators can swap an agent's model — or which client serves it — by editing the file. No restart required.

The router also owns **client routing**: the nested agent form declares a model per backend (`openrouter` / `claude_code` / `anthropic` / any backend registered via [`pf_core.clients.routing.register_client`](#client-registry)), and `resolve_agent(slug)` returns the right client plus chat kwargs in one call. There is deliberately **no framework-hardcoded default backend** — the consumer's YAML declares the default, and when nothing resolves the router fails hard with `ConfigurationError`.

> **Install:** the config-resolution API (`get_agent_config`, `resolve_backend`, `get_agent_block`, `list_agents`, `assert_agents_registered`) is pyyaml-only, and pyyaml is one of pf-core's hard base dependencies — so a bare `pip install pf-core` is enough to read the YAML and hand kwargs to a client you construct yourself. The client-acquisition API (`resolve_agent`, `call_with_fallback`, `resolve_agent_candidates`) additionally imports the relevant client module on demand, so it needs whatever extra that backend requires (`[llm]` for openrouter, `[anthropic]` for the Anthropic SDK, the `claude` CLI for claude_code).

This is not the LLM tracking backbone. Tracking (`pf_core.llm.tracking`) records the **actuals** of every call into `llm_runs`. The router declares **intent**: which model and sampling kwargs a given agent slug should use next time it runs. The two are complementary — services typically read the router, pass the result into the client, and rely on tracking to persist what actually happened.

---

## Table of Contents

- [Concepts](#concepts)
- [Quick start](#quick-start)
- [YAML format](#yaml-format)
- [Environment variables](#environment-variables)
- [API](#api)
- [Client registry](#client-registry)
- [Per-agent Anthropic prompt caching](#per-agent-anthropic-prompt-caching)
- [Errors](#errors)
- [TTL cache behavior](#ttl-cache-behavior)
- [Integration with tracking](#integration-with-tracking)
- [Operational rollout](#operational-rollout)
- [Adding a new agent](#adding-a-new-agent)

---

## Concepts

- **Agent slug** — the unit of configuration. `summarizer` gets one model, `classifier` gets another. A single global default is an anti-pattern — different agents have different cost, latency, and quality tradeoffs.
- **YAML is intent.** Structured, diff-reviewable, one entry per agent. Sampling kwargs live alongside the model choice so the whole call shape is captured in one place.
- **`llm_runs` is actuals.** Every invocation's real model, fingerprint, tokens, cost, and sampling params land in the tracking tables. The router never writes to the DB.
- **Per-backend models, never translated.** The same model is `anthropic/claude-sonnet-4.6` on OpenRouter, `sonnet` as a claude_code alias, and `claude-sonnet-4-6` to the Anthropic SDK — these are not mechanically convertible, so the nested form declares a model string per backend and the router never rewrites ids.
- **Capability constraint by omission.** An agent only routes to backends it declares. An agent that only lists an `openrouter` backend can never be rotated onto a backend that lacks a capability it needs.
- **Fallback is opt-in and comes in two shapes, both gated by `fallback: true`.** *Availability* fallback (`resolve_agent`): a declared backend whose client can't be constructed or whose `preflight()` fails is skipped at acquisition time. *Call-failure* fallback (`call_with_fallback` / `resolve_agent_candidates`): a `chat()` that raises moves to the next declared backend with **its own** model — the call itself is the probe, no preflight. An env-selected backend participates in both (its choice goes first in the chain); only an explicit per-call `backend=` is deterministic. Clients still own same-backend retry, and per-call quality ladders ("try X, judge, then Y") stay caller-owned business logic.
- **Reload without restart.** The loader caches the parsed YAML for a TTL. Edit the file, wait one interval, the next call sees the new config.

**Do:** name agent slugs the same across YAML, call sites, and `assert_agents_registered(...)`.

**Do not:** pass arbitrary config values as function arguments — keep them in the YAML so every call site stays consistent.

---

## Quick start

```yaml
# config/model_router.yaml
agents:
  summarizer:
    model: anthropic/claude-opus-4.1   # OpenRouter id — sent to the provider verbatim
    temperature: 0.3
    max_tokens: 4000
```

```python
from pf_core.llm.router import get_agent_config
from pf_core.clients.openrouter import get_client

cfg = get_agent_config("summarizer")
# {"model": "anthropic/claude-opus-4.1", "temperature": 0.3, "max_tokens": 4000}

content, usage = get_client().chat(messages=msgs, **cfg)
```

The flat `model` is sent to the provider exactly as written, so it must match the client you pair it with — an OpenRouter id (`anthropic/…`) for the OpenRouter client, a bare id for the Anthropic SDK. (The nested form below declares a model *per backend* for this reason.) The returned dict is a plain `dict` — unpack it straight into `client.chat(**cfg, messages=...)`. No wrapper class, no accessor methods.

---

## YAML format

One file per consumer project, committed to the repo at `config/model_router.yaml` by convention.

```yaml
# config/model_router.yaml
agents:
  summarizer:
    model: claude-opus-4-7
    temperature: 0.3
    max_tokens: 4000

  classifier:
    model: claude-sonnet-4-6
    temperature: 0.0

  extractor:
    model: perplexity/sonar-pro
    temperature: 0.2
    top_p: 1.0

  critic:
    model: claude-sonnet-4-6
    temperature: 0.1
    reasoning_effort: medium
```

### Nested form — per-backend models + client routing

When an agent can run on more than one backend, nest a `backends:` mapping instead of a flat `model`:

```yaml
default_client: openrouter            # top-level declared default (optional)
env_prefix: MYPROJ                    # enables MYPROJ_<SLUG>_BACKEND env overrides (optional)
non_chat_keys: [max_input_tokens]     # consumer-extension keys stripped from chat kwargs (optional)

agents:
  summarizer:
    default_backend: claude_code      # this agent's default (optional)
    fallback: true                    # opt-in availability fallback (optional)
    temperature: 0.3                  # agent-wide sampling; backend entries may override
    max_input_tokens: 800000          # consumer extension key (declared in non_chat_keys)
    backends:
      openrouter:  {model: anthropic/claude-sonnet-4.6}
      claude_code: {model: sonnet, max_tokens: 8000, client_kwargs: {retry: 1}}
      anthropic:   {model: claude-sonnet-4-6}

  extractor:                          # only declares openrouter -> can never route elsewhere
    backends:
      openrouter: {model: perplexity/sonar-pro}
```

**Backend selection precedence:** per-call `backend=` kwarg > env `<ENV_PREFIX>_<SLUG>_BACKEND` > agent `default_backend` (or its single declared backend) > top-level `default_client` > `ConfigurationError`. An env value naming an undeclared backend falls through to the next tier (ops resilience).

### Keys

| Key | Required | Description |
|-----|----------|-------------|
| `default_client` (top level) | no | Backend used when an agent doesn't pick one — the consumer-declared LLM default |
| `env_prefix` (top level) | no | Enables `<ENV_PREFIX>_<SLUG>_BACKEND` env overrides; without it, env vars are not consulted |
| `non_chat_keys` (top level) | no | Consumer-extension keys stripped from chat kwargs (read them back via `get_agent_block`) |
| `agents.<slug>.model` | flat form | Canonical model slug — sent to the provider exactly as written |
| `agents.<slug>.backends.<backend>.model` | nested form | Backend-specific model string — each backend's own id format, never translated |
| `agents.<slug>.default_backend` | no | This agent's default backend (must be declared under its `backends`) |
| `agents.<slug>.fallback` | no | `true` enables the fallback chain: skip unavailable backends at acquisition (`resolve_agent`) and move past failed calls (`call_with_fallback`). Env-selected backends participate; per-call `backend=` does not |
| `agents.<slug>.backends.<backend>.client_kwargs` | no | Passed to the client factory, never to `chat()` (e.g. `{retry: 1}`). Built-ins construct one instance per distinct kwargs signature (module singleton when empty) — differently-tuned agents don't collide |
| `agents.<slug>.temperature` / `max_tokens` / `top_p` / `reasoning_effort` / `<other>` | no | Sampling — agent-wide in the nested form, overridable per backend entry |

A flat `model` and a `backends:` mapping are mutually exclusive on one agent. The router does not validate sampling keys beyond `model` — unknown keys flow through to the client, which is responsible for rejecting anything the provider does not accept.

**Do:** match agent slugs to `llm_agent_types.slug` used by tracking. Misspellings surface at `assert_agents_registered(...)` or at first run (tracking would insert a new `llm_agent_types` row with the wrong slug — caught in review).

**Do not:** store secrets or per-environment values here. Use `.env` for those. The YAML is checked into the repo.

---

## Environment variables

| Var | Default | Purpose |
|-----|---------|---------|
| `MODEL_ROUTER_CONFIG` | `config/model_router.yaml` | Path to the YAML file |
| `MODEL_ROUTER_RELOAD_SECONDS` | `60` | TTL for the in-memory cache. `0` disables caching — every call re-reads the file |

Both are read inside `pf_core.llm.router` directly — consumers do not need to add them to their `AppConfig` subclass.

---

## API

All functions live in `pf_core.llm.router`.

### `resolve_agent(slug, *, backend=None, model_override=None) -> ResolvedAgent`

The single entry point for YAML-routed LLM sites — picks the backend, acquires the client (through the [client registry](#client-registry)), and returns chat-unpackable kwargs. `ResolvedAgent` is a NamedTuple unpacking as `(client, chat_kwargs, backend)`.

```python
from pf_core.llm.router import resolve_agent

client, cfg, backend = resolve_agent("summarizer")
content, usage = client.chat(messages=msgs, **cfg)
```

- `backend=` forces a specific backend (per-call override — beats everything, disables fallback).
- `model_override=` swaps the model only (a `--model` CLI flag, A/B run) — beats the YAML.
- `backend` in the result is the resolved name — pass it to tracking as the `provider` label.
- With `fallback: true` (selection from YAML *or* env — the env choice goes first in the chain), unavailable backends (factory error or failed `preflight()`) are skipped in declaration order; all-unavailable raises `ConfigurationError` naming each failure. Without fallback there is no preflight call and acquisition errors propagate unchanged.

### `call_with_fallback(slug, messages, *, model_override=None, retry_on=(Exception,)) -> FallbackCall`

Call-failure fallback in one line — walks the agent's chain and calls `chat()` on each candidate (with that candidate's own model and kwargs) until one succeeds. `FallbackCall` unpacks as `(content, usage, resolved)`.

```python
from pf_core.llm.router import call_with_fallback

content, usage, resolved = call_with_fallback("summarizer", msgs)
# resolved.backend answered — use it as the tracking provider label
```

- An exception matching `retry_on` logs and moves to the next backend; anything else propagates immediately. Narrow `retry_on` to transport-shaped errors (`(ClientError,)`, timeouts) so a deterministic bug doesn't burn a second backend.
- No `preflight()` on this path — the call itself is the probe.
- Exhaustion re-raises the **last call exception unchanged** (existing except clauses keep working); if no backend was even constructable, raises `ConfigurationError` listing the failures.
- Without `fallback: true` this is a plain single-backend call.

### `resolve_agent_candidates(slug, *, model_override=None) -> Iterator[ResolvedAgent]`

The primitive under `call_with_fallback`, for callers that own the try/except — e.g. to record a tracking row per attempt. Yields the chain lazily (a later backend's client is only constructed if iteration reaches it), each candidate carrying its own backend's model and chat kwargs.

```python
for client, cfg, backend in resolve_agent_candidates("summarizer"):
    try:
        content, usage = client.chat(messages=msgs, **cfg)
        break
    except ClientError:
        continue
```

### `get_agent_config(slug, *, backend=None, model_override=None) -> dict`

Config-only resolution — use when you manage clients yourself.

```python
from pf_core.llm.router import get_agent_config

cfg = get_agent_config("summarizer")
# flat:   {"model": "claude-opus-4-7", "temperature": 0.3, "max_tokens": 4000}
# nested: the active backend's model + merged sampling, structural keys stripped

content, usage = client.chat(messages=msgs, **cfg)
```

Returns a fresh `dict` per call — mutating it does not affect the cache. Always contains a `model` key plus sampling kwargs; never contains `backends`, `default_backend`, `fallback`, `client_kwargs`, or declared `non_chat_keys`.

### `resolve_backend(slug, *, backend=None) -> str`

The selection step alone — which backend would serve this agent right now. Useful for logging/labels without acquiring a client.

### `get_agent_block(slug) -> dict`

The raw validated YAML block (a copy) — the escape hatch for reading consumer-extension keys that `get_agent_config` strips (e.g. an input-token gate declared in `non_chat_keys`).

### `list_agents() -> list[str]`

Use this for admin UIs, debug endpoints, or CLI `--help` listings.

```python
from pf_core.llm.router import list_agents

list_agents()
# ["classifier", "critic", "extractor", "summarizer"]
```

Returns all slugs defined in the YAML, sorted.

### `assert_agents_registered(expected)`

Use this once at app startup to fail fast if the codebase references a slug that is not in the YAML.

```python
from pf_core.llm.router import assert_agents_registered

assert_agents_registered(["summarizer", "classifier", "extractor", "critic"])
```

Raises `ConfigurationError` if any slug is missing. Catches typos and un-staged YAML edits before the first LLM call.

### `clear_cache()`

Use this in tests and in admin "force reload" endpoints.

```python
from pf_core.llm.router import clear_cache

clear_cache()
```

Drops the in-memory cache. The next call reloads the YAML from disk.

---

## Client registry

`backends:` keys are resolved through `pf_core.clients.routing` — three factories are built in (`openrouter`, `claude_code`, `anthropic`), and consumers register their own. Any object satisfying the `pf_core.clients.ChatClient` protocol works; no inheritance needed.

```python
# At app startup:
from pf_core.clients.routing import register_client
from app.clients.ollama import OllamaClient

register_client("ollama", lambda **kw: OllamaClient(**kw))
```

```yaml
# model_router.yaml may now route agents to it:
agents:
  classifier:
    default_backend: ollama
    backends:
      ollama:     {model: "qwen2.5:14b"}
      openrouter: {model: anthropic/claude-haiku-4-5}
```

- Custom factories are called on every acquisition with the backend block's `client_kwargs` (if any) and own their lifecycle (fresh per call, cached, pooled — their choice).
- Built-ins are lifecycle-managed by the registry: no kwargs returns the module singleton (`get_client()`); with kwargs, a fresh instance is constructed via the module's `new_client()` and cached per `(backend, kwargs)` signature — so two agents tuning the same backend differently get distinct clients, re-resolution reuses them, and the kwargs-less singleton is never poisoned. (`new_client()` on `openrouter` / `claude_code` / `anthropic` is also directly callable — same env resolution as `get_client()`, no caching.)
- Registering a built-in name overrides it for the process — useful in tests.
- `get_client_for_backend(name, **kwargs)` is the direct acquisition path for callers that don't use the YAML router; `registered_backends()` lists what's routable; `clear_client_cache()` drops the per-signature instances (tests).
- `get_routed_client(use_claude_code: bool)` is **deprecated** (removed in v1.0): the boolean shape baked in OpenRouter as the silent default. Use `resolve_agent` or `get_client_for_backend`.

---

## Per-agent Anthropic prompt caching

Backend-block kwargs forward to the client, so caching is one line per agent:

```yaml
agents:
  summarizer:
    default_backend: anthropic
    backends:
      anthropic: { model: claude-sonnet-4-6, cache_system: true }
```

See `docs/anthropic.md` (System messages and prompt caching) for semantics and caveats.

---

## Errors

All failures raise `pf_core.exceptions.ConfigurationError` — operator-fixable at deploy time, not bugs.

| Condition | Message |
|-----------|---------|
| YAML file missing | `model_router.yaml not found at <path>` |
| YAML malformed | `failed to parse <path>: <reason>` |
| Top level not a mapping | `<path> must be a mapping at the top level` |
| Missing `agents` section | `<path> is missing required 'agents' section` |
| `agents` not a mapping | `<path>: 'agents' must be a mapping` |
| Agent entry not a mapping | `agent '<slug>' entry must be a mapping` |
| Agent declares neither form | `agent '<slug>' must declare a 'model' (flat form) or a 'backends' mapping (nested form)` |
| Agent declares both forms | `agent '<slug>' declares both a top-level 'model' and 'backends' — …` |
| Empty or non-string `model` value | `… 'model' must be a non-empty string` |
| Backend entry missing `model` | `agent '<slug>' backend '<backend>' missing required key 'model'` |
| Empty `backends:` mapping | `agent '<slug>': 'backends' must be a non-empty mapping` |
| `client_kwargs` not a mapping | `agent '<slug>' backend '<backend>': 'client_kwargs' must be a mapping` |
| `fallback` not a boolean | `agent '<slug>': 'fallback' must be a boolean` |
| `default_client` / `env_prefix` empty or non-string | `<path>: '<key>' must be a non-empty string` |
| `non_chat_keys` not a list of strings | `<path>: 'non_chat_keys' must be a list of strings` |
| `default_backend` not declared | `agent '<slug>': default_backend '<backend>' is not one of its declared backends (…)` |
| Slug not in YAML | `agent '<slug>' not defined in <path>` |
| Requested backend not declared | `agent '<slug>' does not declare backend '<backend>' — declared: …` |
| No backend resolvable | `no backend resolvable for agent '<slug>' in <path> — set 'default_backend' …` |
| Unknown backend name | `unknown LLM backend '<name>' — routable backends: …` (from the client registry) |
| All fallback candidates unavailable | `agent '<slug>': no available backend — tried <backend>: <error>; …` |
| Empty slug passed to `get_agent_config` | `agent slug is required` |
| `pyyaml` not installed | `pyyaml is required to load model_router.yaml` |

`FlowException` is the wrong base class here — these are not normal domain failures, they are deployment-time misconfigurations. The app should refuse to serve traffic until they are fixed.

---

## TTL cache behavior

The loader keeps a process-local cache of the parsed YAML.

- **First call** reads the file, parses it, stores the result plus a monotonic timestamp.
- **Subsequent calls within `MODEL_ROUTER_RELOAD_SECONDS`** return the cached dict without touching disk.
- **After the TTL expires**, the next call re-reads the file under a lock. Concurrent callers wait for the single reload — no thundering herd.
- **`MODEL_ROUTER_RELOAD_SECONDS=0`** disables the TTL and re-reads every call. Useful in dev loops; avoid in production.
- **If the reload fails to parse**, the loader logs a warning (`model_router_reload_failed_keeping_cache`) and keeps serving the last-known-good cache. The system does not fail closed — a bad edit should not take down running services. The cache timestamp advances so the next attempt waits another TTL.
- **If the reload fails on the very first load** (no prior cache), the `ConfigurationError` propagates. Without a known-good state to fall back to, there is nothing to serve.
- **Changing `MODEL_ROUTER_CONFIG` at runtime** forces a reload on the next call — the cache is keyed on the resolved path.

`clear_cache()` resets everything. Use it in test teardown so env-var changes take effect.

---

## Integration with tracking

Router and tracking are separate concerns. The router picks the intent; tracking records the actual.

```python
from pf_core.clients.openrouter import get_client
from pf_core.llm.router import get_agent_config
from pf_core.llm.tracking import LlmRunRepo

def summarize(item_id: int, messages: list[dict]) -> str:
    cfg = get_agent_config("summarizer")
    content, usage = get_client().chat(messages=messages, **cfg)

    LlmRunRepo().record(
        agent_type="summarizer",
        model=cfg["model"],
        sampling={k: v for k, v in cfg.items() if k != "model"},
        usage=usage,
        rendered_prompts=(None, messages[-1]["content"]),
        raw_response=content,
    )
    return content
```

Or, when using the `@track_run` decorator, pass `**cfg` through the wrapped call:

```python
from pf_core.llm.tracking import track_run

@track_run(agent_type="summarizer")
def tracked_chat(*, model, messages, **sampling):
    return get_client().chat(model=model, messages=messages, **sampling)

cfg = get_agent_config("summarizer")
content, usage = tracked_chat(messages=msgs, **cfg)
```

The tracking decorator captures `model` and sampling kwargs from the call signature, so the YAML intent and the recorded actuals stay aligned.

**Do:** keep the YAML slug and the `agent_type=` argument identical. Both point at the same `llm_agent_types.slug`.

**Do not:** write a per-project shim that re-interprets YAML keys. If the client does not accept a kwarg the YAML provides, change the YAML — do not filter in code.

See [`llm-tracking.md`](llm-tracking.md) for the full tracking API and [`openrouter.md`](openrouter.md) for the client's accepted kwargs.

---

## Operational rollout

**Swapping a model.** Edit `config/model_router.yaml`, point the target agent's `model` at the new slug, commit, deploy. The running process picks up the change within one `MODEL_ROUTER_RELOAD_SECONDS` interval. Watch `llm_runs` to confirm the new model is hitting production:

```sql
SELECT m.name, COUNT(*), AVG(cost_usd)
FROM llm_runs r
JOIN llm_models m ON m.id = r.model_id
JOIN llm_agent_types a ON a.id = r.agent_type_id
WHERE a.slug = 'summarizer'
  AND r.created_at >= CURRENT_TIMESTAMP - INTERVAL '1' HOUR
GROUP BY m.name;
```

**Rolling back.** `git revert` the YAML edit and redeploy — or edit the file directly and wait one TTL. The previous slug is always recoverable from git history.

**Reviewing.** Every routing change is a PR. Reviewers see exactly which agent moved to which model, side by side with the commit message explaining why.

---

## Adding a new agent

1. **Add an entry to `config/model_router.yaml`:**

   ```yaml
   agents:
     reviewer:
       model: claude-sonnet-4-6
       temperature: 0.1
   ```

2. **Use it at the call site:**

   ```python
   from pf_core.llm.router import get_agent_config

   cfg = get_agent_config("reviewer")
   content, usage = client.chat(messages=msgs, **cfg)
   ```

3. **Register it in the startup check** so a typo or un-staged YAML edit fails loud at boot:

   ```python
   from pf_core.llm.router import assert_agents_registered

   assert_agents_registered([
       "summarizer", "classifier", "extractor", "reviewer",
   ])
   ```

4. **No DB work needed.** Tracking auto-inserts the `llm_agent_types` row on the first call via `resolve_agent_type_id()`. If you want a human-facing description, pre-seed the row in your project's data migration.
