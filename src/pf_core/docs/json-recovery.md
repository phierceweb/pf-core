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

### extract_json_array

Extract the first complete JSON array, ignoring surrounding text:

```python
from pf_core.utils.json_recovery import extract_json_array

raw = 'Here are the results:\n[{"id": 1}, {"id": 2}]\nHope this helps!'
result = extract_json_array(raw)  # [{"id": 1}, {"id": 2}]
```

Returns `None` if no valid array is found.

### extract_json_object

Extract the first complete JSON object:

```python
from pf_core.utils.json_recovery import extract_json_object

raw = 'The classification is: {"category": "sports", "confidence": 0.95}'
result = extract_json_object(raw)  # {"category": "sports", "confidence": 0.95}
```

Returns `None` if no valid object is found.

### extract_json

Extract the first valid JSON value (object or array):

```python
from pf_core.utils.json_recovery import extract_json

result = extract_json(raw)  # dict or list, or None
```

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
