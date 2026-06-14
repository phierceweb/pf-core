# JSON Utilities

Safe JSON parsing helpers that replace the `try: json.loads(x) except: fallback` pattern.

## Quick usage

```python
from pf_core.utils.json import safe_json_loads, safe_json_col, canonical_json

# Parse a JSON string with fallback
data = safe_json_loads(raw_string, fallback={})

# Parse with warning logged on failure
data = safe_json_loads(raw_string, fallback=[], label="config")

# Handle DB columns that may be string or already-parsed
data = safe_json_col(row["metadata"], fallback=[])

# Canonical form for equality comparison or hashing
key = canonical_json({"b": 1, "a": 2})   # '{"a":2,"b":1}'
```

## Functions

### safe_json_loads

Parse a JSON string, returning `fallback` on failure.

```python
safe_json_loads('{"a": 1}')                          # {"a": 1}
safe_json_loads('bad data')                           # None
safe_json_loads('bad data', fallback=[])              # []
safe_json_loads('bad data', fallback=[], label="cfg") # [] + WARNING logged
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `val` | `str \| None` | *(required)* | Raw JSON string, `None`, or empty string |
| `fallback` | `Any` | `None` | Value returned when `val` is missing or unparseable |
| `label` | `str \| None` | `None` | If provided, a WARNING is logged on parse failure |

Returns the parsed Python object, or `fallback`.

Catches `json.JSONDecodeError`, `TypeError`, and `ValueError` — never bare `Exception`.

### safe_json_col

Normalize a value that may be a JSON string or already-parsed object.

SQLite JSON columns return parsed `dict`/`list` objects directly, while other backends return raw strings. This function handles both.

```python
safe_json_col('{"a": 1}')   # {"a": 1} (parsed from string)
safe_json_col({"a": 1})     # {"a": 1} (returned as-is)
safe_json_col([1, 2])       # [1, 2]   (returned as-is)
safe_json_col(None)          # None
safe_json_col(42)            # None     (unrecognized type)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `val` | `Any` | *(required)* | Column value — `None`, `str`, `dict`, `list`, or other |
| `fallback` | `Any` | `None` | Value returned when `val` is `None`, unrecognized, or unparseable |
| `label` | `str \| None` | `None` | Forwarded to `safe_json_loads` when `val` is a string |

Returns the parsed Python object, or `fallback`.

### canonical_json

Serialize a value to canonical JSON — sorted keys, compact `,`/`:` separators — so two semantically-equal objects produce byte-identical output. Use it for equality comparison ("did this config change?") or as the input to a hash.

```python
canonical_json({"b": 1, "a": 2})              # '{"a":2,"b":1}'
canonical_json({"a": 1, "b": 2}) == canonical_json({"b": 2, "a": 1})  # True
canonical_json([1, 2, 3])                      # '[1,2,3]'
canonical_json({"when": date(2026, 4, 14)})    # '{"when":"2026-04-14"}'  (str fallback)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `obj` | `Any` | *(required)* | Any JSON-serializable value; non-native values (`datetime`, `Decimal`, …) fall back to `str` |

Returns a deterministic JSON string. Pairs with [`content_hash`](hashing.md) — `content_hash(obj)` hashes `canonical_json(obj)`.

## Logging

When `label` is provided and parsing fails, a structured WARNING is emitted:

```
json_parse_failed  label=config  preview=bad data here...
```

When `label` is omitted, failures are silent (equivalent to the old `except: pass` pattern, but without catching overly broad exceptions).

## Migrating from consumer projects

Replace this pattern:

```python
try:
    blob["field"] = json.loads(row[7])
except Exception:
    blob["field"] = row[7]
```

With:

```python
from pf_core.utils.json import safe_json_col

blob["field"] = safe_json_col(row[7], fallback=row[7], label="field")
```
