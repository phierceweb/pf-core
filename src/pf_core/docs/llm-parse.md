# LLM Response Parsing

High-level parser that composes the individual extraction and recovery helpers from `pf_core.utils.json_recovery` into a single call.

> **Install:** `parse_llm_json` requires the `[validate]` extra (`pip install 'pf-core[validate]'`) — it uses `json-repair` to recover near-valid JSON, with no httpx/client stack. The low-level helpers in `pf_core.utils.json_recovery` are stdlib-only and import on the base install. Importing `pf_core.llm.parse` without `[validate]` raises a friendly `ImportError` naming the extra.

## Quick usage

```python
from pf_core.llm.parse import parse_llm_json

result = parse_llm_json(llm_response_text, expect="array")
if result is None:
    print("Could not parse response")
```

## Function

### parse_llm_json

Walks a multi-step fallback pipeline to extract valid JSON from LLM output:

1. Strip markdown fences (` ```json ... ``` `)
2. Try `json.loads()` on cleaned text (strict)
3. Try targeted extraction — `extract_json_array` / `extract_json_object`, or `extract_json` for `expect="any"`. Every balanced span is tried and ranked by content, not just the first ([which span wins](json-recovery.md#which-span-wins))
4. Try truncated array recovery (when `recover=True`), anchored on the first *unclosed* bracket
5. Try `json_repair.loads()` — permissive last-resort repair (when `recover=True`). Handles unescaped inner double quotes in string values (e.g. embedded quoted dialogue), backslash-escaped single quotes, trailing commas, unquoted keys, single-quoted strings.
6. Type-check against `expect` parameter

Strict parsing runs first so well-formed responses stay on the fast path — `json_repair` is only called when the stricter steps have all failed, which keeps its permissive tolerance from masking genuine structural defects.

> **Truncation is lossy and unsignaled in the return.** Step 4 salvages the complete-object prefix of an array cut off at `max_tokens` and **drops the incomplete tail** — `[{"a":1},{"b":2},{"c":3` returns `[{"a":1},{"b":2}]`. The return type carries no truncation flag, so a caller can't tell a salvaged-partial result from a complete one by value: 40 records in, 15 rows out, exit 0.
>
> Pass **`on_truncation="raise"`** to get an `InvalidInputError` instead of the prefix — that is the only way to fail a run that must not shed records, and it is what [`tracked_call`](llm-tracked.md) defaults to. Under the default `on_truncation="warn"` the parser returns the prefix and logs a **WARNING** (`parse_llm_json_recovered_truncated`, with the recovered item count) — watch that event, or raise `max_tokens`, if completeness matters. `recover=False` disables truncation recovery (and `json_repair`) entirely and returns `None` on any truncated input.

### Detecting truncation: `truncated=`, not the text

Step 4 only fires once at least one element has closed. A response cut before *anything* closed — the ordinary single-object answer, or an array cut inside its first element — reaches step 5, where `json_repair` seals it and returns a plausible partial record with no warning and no exception:

```python
parse_llm_json('{"total_usd": 12345', expect="object", on_truncation="raise")
# → {"total_usd": 12345}   — truncated from 123456, and it does not raise
```

The response text cannot close that gap. Quote-parity and "must end on a closing bracket" heuristics both fire on *complete* payloads — an unescaped inner quote (`{"size": "5" long", "ok": true}`) or trailing commentary is exactly the malformed-but-complete shape `json_repair` exists to rescue.

So the signal must come from the provider. Pass **`truncated=`**, read off the client's `usage` dict:

```python
from pf_core.llm.parse import parse_llm_json, truncated_from_usage

content, usage = client.chat(messages=messages, model=model)
parse_llm_json(content, on_truncation="raise", truncated=truncated_from_usage(usage))
```

| `truncated` | Meaning | Effect |
|---|---|---|
| `True` | Provider reported the token limit | Gates the whole pipeline: `on_truncation="raise"` raises even when step 2 parsed the payload cleanly (the cap can land on the closing brace); `"warn"` returns the value and logs `parse_llm_json_recovered_truncated`. |
| `False` | Provider reported a different stop reason | Nothing — step 4 still behaves as it always has. |
| `None` (default) | Unknown | Identical to omitting the argument. |

`truncated_from_usage` reads `usage["finish_reason"]` against two vocabularies, case-insensitively:

| Reported reason | Result |
|---|---|
| `length`, `max_tokens`, `model_context_window_exceeded` | `True` |
| `stop`, `end_turn`, `stop_sequence`, `eos`, `tool_use`, `tool_calls`, `function_call` | `False` |
| anything else, or absent / `None` | `None` |

An unrecognised reason is **unknown, not complete** — answering `False` would assert completeness on a token no backend here has ever emitted. All three clients report a reason: [OpenRouter](openrouter.md) passes the route's through, [Anthropic](anthropic.md) and [Claude Code](claude-code.md) normalise their `stop_reason` onto `"length"`. [`tracked_call`](llm-tracked.md) and [`llm_step`](llm-step.md) wire this through for you.

```python
# Parse any JSON
parse_llm_json('{"key": "val"}')  # {"key": "val"}

# Expect a specific type
parse_llm_json("[1, 2, 3]", expect="array")  # [1, 2, 3]
parse_llm_json('{"a": 1}', expect="array")  # None (wrong type)

# Handle markdown fences
parse_llm_json("```json\n[1, 2]\n```", expect="array")  # [1, 2]

# Handle trailing prose
parse_llm_json('[{"a":1}]\nHere is my explanation...')  # [{"a": 1}]

# Recover truncated arrays — prefix salvaged, tail dropped, WARNING logged
parse_llm_json('[{"a":1},{"b":2},{"c":3', expect="array")  # [{"a":1},{"b":2}]

# …or refuse the partial result
parse_llm_json('[{"a":1},{"b":2},{"c":3', expect="array", on_truncation="raise")
# raises InvalidInputError

# Repair malformed LLM output — unescaped inner quotes, trailing commas, etc.
parse_llm_json('{"quote": "She said, "Hello.""}', expect="object")
# → {"quote": "She said, \"Hello.\""}

# Strict mode — raises instead of returning None
parse_llm_json("garbage", strict=True)  # raises InvalidInputError

# recover=False disables BOTH truncation recovery AND json_repair
parse_llm_json('{"q": "she said, "hi""}', recover=False)  # → None
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `raw` | `str` | *(required)* | Raw LLM response text |
| `expect` | `str` | `"any"` | Expected type: `"any"`, `"array"`, or `"object"` |
| `recover` | `bool` | `True` | Enable both truncated-array recovery and `json_repair` permissive repair. Set `False` for strict parse semantics. |
| `strict` | `bool` | `False` | Raise `InvalidInputError` instead of returning `None` |
| `on_truncation` | `str` | `"warn"` | What happens on a known-truncated response: `"warn"` returns what was salvaged and logs a WARNING, `"raise"` raises `InvalidInputError`. Independent of `strict`. |
| `truncated` | `bool \| None` | `None` | Authoritative provider flag (see above). `None` means unknown and changes nothing. |

Returns `dict | list | None`.

## Migrating from consumer projects

Replace this pattern:

```python
from pf_core.utils.json_recovery import (
    extract_json_array,
    recover_truncated_json,
    strip_markdown_fences,
)

raw = strip_markdown_fences(content)
try:
    result = json.loads(raw)
except json.JSONDecodeError:
    result = extract_json_array(raw)
    if result is None:
        result = recover_truncated_json(raw)
```

With:

```python
from pf_core.llm.parse import parse_llm_json

result = parse_llm_json(content, expect="array")
```

## Related

- [JSON Recovery](json-recovery.md) — the lower-level extraction functions this module composes (`extract_json_array`, `recover_truncated_json`, `strip_markdown_fences`)
- [JSON Utilities](json-utils.md) — safe parsing for non-LLM JSON (DB columns, config)
