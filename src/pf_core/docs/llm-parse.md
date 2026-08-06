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
>
> `on_truncation` governs step 4 only. A truncated *object* is completed by `json_repair` in step 5 and is not covered.

```python
# Parse any JSON
parse_llm_json('{"key": "val"}')                     # {"key": "val"}

# Expect a specific type
parse_llm_json('[1, 2, 3]', expect="array")          # [1, 2, 3]
parse_llm_json('{"a": 1}', expect="array")           # None (wrong type)

# Handle markdown fences
parse_llm_json('```json\n[1, 2]\n```', expect="array")  # [1, 2]

# Handle trailing prose
parse_llm_json('[{"a":1}]\nHere is my explanation...') # [{"a": 1}]

# Recover truncated arrays — prefix salvaged, tail dropped, WARNING logged
parse_llm_json('[{"a":1},{"b":2},{"c":3', expect="array")  # [{"a":1},{"b":2}]

# …or refuse the partial result
parse_llm_json('[{"a":1},{"b":2},{"c":3', expect="array", on_truncation="raise")
# raises InvalidInputError

# Repair malformed LLM output — unescaped inner quotes, trailing commas, etc.
parse_llm_json('{"quote": "She said, "Hello.""}', expect="object")
# → {"quote": "She said, \"Hello.\""}

# Strict mode — raises instead of returning None
parse_llm_json('garbage', strict=True)  # raises InvalidInputError

# recover=False disables BOTH truncation recovery AND json_repair
parse_llm_json('{"q": "she said, "hi""}', recover=False)  # → None
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `raw` | `str` | *(required)* | Raw LLM response text |
| `expect` | `str` | `"any"` | Expected type: `"any"`, `"array"`, or `"object"` |
| `recover` | `bool` | `True` | Enable both truncated-array recovery and `json_repair` permissive repair. Set `False` for strict parse semantics. |
| `strict` | `bool` | `False` | Raise `InvalidInputError` instead of returning `None` |
| `on_truncation` | `str` | `"warn"` | What step 4 does with a salvaged prefix: `"warn"` returns it and logs a WARNING, `"raise"` raises `InvalidInputError`. Independent of `strict`. |

Returns `dict | list | None`.

## Migrating from consumer projects

Replace this pattern:

```python
from pf_core.utils.json_recovery import extract_json_array, recover_truncated_json, strip_markdown_fences

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
