# Content Hashing

Stable hex digests for change-detection and cache keys. `content_hash` hashes raw text/bytes directly, or any structured value via its [`canonical_json`](json-utils.md) form — so dicts and lists with reordered keys produce the same digest.

Not a security primitive: the digest is plain and unsalted. Use it to answer "did this content change?" or to key a cache, not to store passwords.

## Quick usage

```python
from pf_core.utils.hashing import content_hash

content_hash("some text")          # sha256 hex of the UTF-8 bytes
content_hash({"a": 2, "b": 1})     # sha256 of canonical_json(obj) — order-independent
content_hash(corpus, algo="md5")   # any hashlib algorithm
```

## Functions

### content_hash

Return a stable hex digest of `content`.

```python
content_hash("hello")                       # 2cf24dba...938b9824 (sha256)
content_hash(b"hello") == content_hash("hello")                   # True (str encodes UTF-8)
content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})  # True (canonical form)
content_hash("hello", algo="md5")           # 5d41402abc4b2a76b9719d911017c592
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `content` | `Any` | *(required)* | Text, bytes, or any JSON-serializable value (hashed via `canonical_json`) |
| `algo` | `str` | `"sha256"` | Any algorithm name accepted by `hashlib.new` (`"md5"`, `"sha1"`, …) |

Returns the hex digest as a string.

## Relationship to other helpers

- Objects are hashed through [`canonical_json`](json-utils.md), so key order and insignificant whitespace don't affect the digest.
- For LLM-call cache keys specifically, `pf_core.llm.tracking.compute_input_hash` builds a richer key (model + prompts + sampling); `content_hash` is the general-purpose primitive.
