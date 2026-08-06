# JSON Recovery

Extract and recover JSON from messy text — markdown fences, trailing commentary, or truncation mid-object. Generic and stdlib-only: the functions operate on plain strings with no LLM coupling, so they live in the foundation `utils` package (`pf_core.utils.json_recovery`) and import on the base install. A common source of messy JSON is LLM output, but nothing here is LLM-specific.

## Functions

### strip_markdown_fences

Remove ` ```json ``` ` wrappers from text:

```python
from pf_core.utils.json_recovery import strip_markdown_fences

raw = '```json\n{"key": "value"}\n```'
clean = strip_markdown_fences(raw)  # '{"key": "value"}'
```

### Which span wins

`extract_json_array`, `extract_json_object` and `extract_json` all try **every**
balanced span in the text, not just the first one, and rank the spans that parse:

1. **A non-empty span starting at offset 0 wins outright.** Nothing precedes it,
   so it cannot be prose. `[]` and `{}` are excluded — an empty container is
   valid JSON and would otherwise beat the payload from the front of the text.
2. **Otherwise a span carrying a non-empty object beats a scalar-only one.**
   This is the load-bearing rule: a citation marker like `[1]` or `[1,2]` *is
   itself valid JSON*, so "the first span that parses" cannot tell it apart from
   the payload. Content can — real payloads are objects or lists of objects.
3. **Otherwise the earliest span wins**, so first-wins is preserved among equals.

Two things bound where the scan looks:

- **It stops at the first unclosed opener.** Truncation leaves the container
  unclosed, so a span after that point is usually a fragment of something
  incomplete — an inner field's array, not the payload.
- **A span that fails to parse is skipped whole**; the scan never descends into
  it.

Both exist so that a well-formed fragment lifted out of a broken container
cannot satisfy step 3 of [`parse_llm_json`](llm-parse.md) and thereby pre-empt
steps 4 and 5 (`recover_truncated_json` and `json_repair`), which own that
input — silently dropping the wrapper and the truncation warning with it. The
scan also gives up after a bounded number of dead ends (`_MAX_SCAN_FAILURES` in
`utils/json_recovery.py`).

Two limits are worth knowing:

- The unclosed-opener bound cannot tell truncation from a stray brace in prose,
  so a payload that *follows* an unclosed `{` or `[` is not extracted —
  `extract_json_object('A [ stray bracket. Then {"a": 1}')` is `None`. Control
  falls through to `recover_truncated_json` / `json_repair`, which may recover
  it, and may emit the truncation warning on input that was never truncated.
- `extract_json` scans braces and brackets independently, so a failed `{` span
  does not bound the `[` scan: on `'{"items": [{"a": 1}], "b": nope}'` it
  returns the inner `[{"a": 1}]`. Use `extract_json_object` when the payload is
  known to be an object — it declines and lets `json_repair` rebuild the
  wrapper.

**Residual, by design:** when the genuine payload is itself scalar-only, it ranks
the same as a citation decoy and the earliest wins — `'As shown [3], [1, 2, 3]'`
returns `[3]`. Preferring the longest span would fix that case, but the
ambiguity is genuine — nothing in the text distinguishes a leading marker from a
leading payload — so the rule stays positional rather than guessing.

### extract_json_array

```python
from pf_core.utils.json_recovery import extract_json_array

raw = 'Here are the results:\n[{"id": 1}, {"id": 2}]\nHope this helps!'
result = extract_json_array(raw)  # [{"id": 1}, {"id": 2}]

# A prose bracket does not win, and does not block extraction:
extract_json_array('Based on the criteria [1]:\n[{"a": 1}]')  # [{"a": 1}]
extract_json_array('See [Table 1]. Result:\n[{"x": 1}]')    # [{"x": 1}]
```

Returns `None` if no valid array is found.

### extract_json_object

```python
from pf_core.utils.json_recovery import extract_json_object

raw = 'The classification is: {"category": "sports", "confidence": 0.95}'
result = extract_json_object(raw)  # {"category": "sports", "confidence": 0.95}

# `{}` is the only prose token that is also valid JSON — it loses too:
extract_json_object('Template {} then real: {"a": 1}')       # {"a": 1}
extract_json_object('Use the {placeholder} form:\n{"a": 1}')  # {"a": 1}
```

Returns `None` if no valid object is found.

### extract_json

Extract the best JSON value, object or array:

```python
from pf_core.utils.json_recovery import extract_json

result = extract_json(raw)  # dict or list, or None
```

An array inside an object loses to the object containing it — a container always
starts at or before what it contains, so rule 3 settles it.

### recover_truncated_json

Salvage complete objects from a truncated array. When a response is cut off mid-stream, the array ends incomplete:

```python
from pf_core.utils.json_recovery import recover_truncated_json

raw = '[{"id": 1, "text": "complete"}, {"id": 2, "text": "also complete"}, {"id": 3, "te'
result = recover_truncated_json(raw)
# [{"id": 1, "text": "complete"}, {"id": 2, "text": "also complete"}]
# The incomplete third object is dropped
```

Returns `None` if recovery fails entirely.

## Recommended pattern

For LLM output, prefer the high-level [LLM Response Parser](llm-parse.md) (needs `[validate]`) over calling these directly:

```python
from pf_core.llm.parse import parse_llm_json

entries = parse_llm_json(llm_response_text, expect="array") or []
```

If you need fine-grained control over each fallback step (or you're parsing non-LLM text), use the individual functions:

```python
from pf_core.utils.json_recovery import extract_json_array, recover_truncated_json

entries = extract_json_array(raw)
if entries is None:
    entries = recover_truncated_json(raw)
if entries is None:
    entries = []
```

## Related

- [LLM Response Parser](llm-parse.md) — high-level pipeline composing these functions (LLM tier, `[validate]`)
- [JSON Utilities](json-utils.md) — safe parsing for structured JSON (DB columns, config)
