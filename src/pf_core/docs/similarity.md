# Text Similarity

Character-level shingling and Jaccard similarity for fast, dependency-free near-duplicate detection.

## Quick check

```python
from pf_core.utils.similarity import is_near_duplicate

if is_near_duplicate(draft_summary, existing_summary):
    print("Too similar — likely a duplicate")
```

## Using the primitives

```python
from pf_core.utils.similarity import shingle, jaccard

sim = jaccard(shingle(text_a), shingle(text_b))
print(f"Similarity: {sim:.0%}")
```

## Functions

### shingle

Create character k-grams from text for set-based comparison.

```python
from pf_core.utils.similarity import shingle

shingle("abcdef")        # {"abcd", "bcde", "cdef"}  (k=4 default)
shingle("abcdef", k=2)   # {"ab", "bc", "cd", "de", "ef"}
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `text` | `str` | (required) | Input string |
| `k` | `int` | `4` | Shingle size (keyword-only) |

Returns `set[str]`. Strings shorter than `k` return a set containing the full string.

### jaccard

Jaccard similarity coefficient between two sets.

```python
from pf_core.utils.similarity import jaccard

jaccard({"a", "b", "c"}, {"b", "c", "d"})  # 0.5
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `a` | `set` | First set (typically shingle output) |
| `b` | `set` | Second set |

Returns `float` in `[0.0, 1.0]`. Returns `0.0` when both sets are empty.

### is_near_duplicate

Convenience wrapper that shingles both texts and checks Jaccard similarity against a threshold.

```python
from pf_core.utils.similarity import is_near_duplicate

is_near_duplicate("hello world", "hello world")  # True
is_near_duplicate(text_a, text_b, threshold=0.9)  # stricter
is_near_duplicate(text_a, text_b, k=2)            # more forgiving
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `text_a` | `str` | (required) | First text |
| `text_b` | `str` | (required) | Second text |
| `k` | `int` | `4` | Shingle size (keyword-only) |
| `threshold` | `float` | `0.75` | Minimum Jaccard similarity (keyword-only) |

Returns `bool`.

## Algorithm notes

**Jaccard similarity** measures the overlap between two sets: `|A ∩ B| / |A ∪ B|`. Combined with character shingling, it provides a fast O(n) similarity measure that is tolerant of word reordering and minor edits.

**Choosing k:** Smaller values (2–3) are more forgiving; larger values (5–8) are more specific. The default `k=4` works well for paragraph-length text.

**Choosing threshold:** The default `0.75` catches near-duplicates while allowing paraphrased content through. Use `0.9+` for stricter matching, `0.5` for loose similarity.

## Migration

Replace a hand-rolled `similarity` helper in your project:

```python
# Before
def shingle(text, k=4): ...
def jaccard(a, b): ...

# After — re-export from pf-core
from pf_core.utils.similarity import shingle, jaccard
```

No downstream caller changes needed if you keep re-exporting the same names — the service modules that import from your local `similarity` helper stay unchanged.
