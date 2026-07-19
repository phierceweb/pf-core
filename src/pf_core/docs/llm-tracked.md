# Tracked LLM Call

One function — `tracked_call` — that composes the pf-core LLM primitives consumers kept re-implementing by hand: render a prompt spec, resolve a versioned `system_prompt_id`, invoke an injected chat client, record exactly one `llm_runs` row, and (when JSON is expected) parse it with a single **tracked** retry.

This is the orchestration layer on top of [LLM tracking](llm-tracking.md). It does not replace `LlmRunRepo.record()` or the `@track_run` decorator — use it when a call follows the common "spec → invoke → record → maybe-parse-with-retry" shape and you want the retry row linked automatically.

---

## Table of Contents

- [When to use it](#when-to-use-it)
- [The client contract](#the-client-contract)
- [What one call does](#what-one-call-does)
- [JSON expectation and the tracked retry](#json-expectation-and-the-tracked-retry)
- [Failure handling](#failure-handling)
- [The messages variant — `tracked_messages_call`](#the-messages-variant--tracked_messages_call)
- [Why not `@track_run`](#why-not-track_run)
- [Adding a new call site](#adding-a-new-call-site)

---

## When to use it

**Do:** use `tracked_call` when a stage renders one prompt spec part, sends it as a single user message, and records one run — optionally parsing JSON with one retry.

**Do not:** use it for multi-message conversations, streaming, or flows that need to attach configs/validations/metrics to the run. Those compose `LlmRunRepo.record()` directly.

The client is injected, never coded in. Bake per-stage options (model pin, `--allowedTools`, timeout) into the client before passing it — the orchestrator stays backend-agnostic.

---

## The client contract

`tracked_call` accepts any object satisfying the `ChatClient` protocol:

```python
def chat(self, messages: list[dict], model: str = ..., **kwargs) -> tuple[str, dict]: ...
```

Both `pf_core.clients.claude_code.ClaudeCodeClient` and `pf_core.clients.openrouter.OpenRouterClient` satisfy it. The returned `usage` dict is recorded as-is except for two keys: `system_fingerprint` is mapped to `llm_runs.model_fingerprint`, and `duration_ms` is read for logging.

---

## What one call does

```python
from pf_core.clients.claude_code import get_client
from pf_core.llm import tracked_call
from pf_core.llm.prompts import load_prompt_spec

spec = load_prompt_spec("config/prompts/classifier.yaml", expected_agent="classifier")
parsed, run_id = tracked_call(
    client=get_client(model="haiku"),
    agent_type="classifier",
    spec=spec,
    model="haiku",
    render_kwargs={"text": text, "category": category},
    expect_json=True,
)
```

In order: render `spec[part]` with the chosen placeholder `style`; resolve `agent_type` (auto-registered) and a `system_prompt_id` for `(agent_type, part, version)` from the spec's `version`; send the rendered text as the **user** message; record one `llm_runs` row with the rendered prompt and raw response. The rendered text lands in the payload's `rendered_user` slot — the slot matches the wire role, so eval replays rebuild the same message role the call actually used.

`render_kwargs` keys are upper-cased internally for the default `style="@@"` (token placeholders are `@@UPPER@@`); for `style="brace"` they pass through unchanged.

---

## JSON expectation and the tracked retry

With `expect_json=False` (default) the raw string and its `run_id` are returned.

With `expect_json=True` the response is parsed via `parse_llm_json(recover=True, strict=True)`:

- Parse succeeds → return `(parsed, run_id)`.
- Parse fails and `json_retry=True` (default) → invoke once more. The retry writes a **second** `llm_runs` row linked to the first via `llm_run_links` with `relation="retry"`. The returned `run_id` is the retry's.
- Both attempts unparseable, or `json_retry=False` and the first parse failed → raise `LlmJsonError`.

`LlmJsonError` carries the last raw response on `.raw` so callers can persist it for debugging (e.g. write `<label>.json.error` next to the output).

---

## Failure handling

A client exception (timeout, non-zero exit, transport error) is recorded as a `status="failed"` row — capturing `error` (truncated to 10 000 chars) and `error_class` — and then **re-raised**. `tracked_call` never swallows client failures; the caller decides whether to retry or abort. Only JSON *parse* failures trigger the built-in retry, not client failures.

---

## The messages variant — `tracked_messages_call`

For call sites that build a real message list (system + user roles, multi-turn) instead of rendering a spec into one user message:

```python
from pf_core.llm.tracked import tracked_messages_call

content, usage, run_id = tracked_messages_call(
    client=client,
    agent_type="summarizer",
    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    model=cfg["model"],
    sampling=sampling,                                   # forwarded to chat AND recorded
    chat_kwargs={"response_format": {"type": "json_object"}, "timeout": 120},  # forwarded only
    spec=spec,                                           # registers system (+ user) prompt ids
    provider="openrouter",
    input_hash=input_hash,                               # pair with your cache lookup
    configs={"report_config": config_id},
)
```

Differences from `tracked_call`: messages pass through verbatim (rendered system/user are extracted by role for `llm_run_payloads`); `spec` is optional and may be minimal (`{"version": int, "system": str}` for canonical-template registration; a full `load_prompt_spec` dict also registers the `user` part, with `spec_on_change` forwarded to `resolve_prompt_id`); there is no JSON retry (validate downstream with `parse_and_validate`); it returns `(content, usage, run_id)`. Client exceptions record a `status="failed"` row and re-raise, same as `tracked_call`. `on_record_error="warn"` makes the tracking sink best-effort — a failed `record()` logs a warning and returns `run_id=None` instead of masking the call result (for pipelines where tracking must never break the work).

Two attribution kwargs: `metadata=` takes a flat dict, split into tags/metrics via [`split_metadata`](llm-recording.md#split_metadata) and merged beneath any explicit `tags=`/`metrics=` (tags concatenated + deduped; explicit metrics win); `job_id=` attributes the run explicitly, with `None` keeping the ambient-Job fallback. When a [recording window](llm-recording.md) is open, its session metadata merges beneath `metadata=` (call wins) and a per-call summary is appended to the window on success and failure.

---

## Why not `@track_run`

`tracked_call` composes `LlmRunRepo` directly rather than reusing the generic `@track_run` decorator because `track_run` cannot carry a spec-resolved `system_prompt_id` nor emit the retry-linked second row — the two things this layer exists to provide.

---

## Adding a new call site

1. Load the spec with `load_prompt_spec(path, expected_agent=...)` so `agent`, `version`, and the part keys are present.
2. Construct the client with all per-stage options baked in.
3. Call `tracked_call` with `expect_json=True` only if the prompt actually demands JSON — a raw text stage should not pay the parse/retry cost.
4. To share a transaction or route writes in tests, pass `repo=LlmRunRepo(...)`; otherwise a fresh instance is used.
