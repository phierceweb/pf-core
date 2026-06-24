# Controlled Vocabulary Normalization

Map free-text strings (typically from an LLM) to a project-specific controlled-vocabulary slug. Useful when a downstream column or filter requires a canonical value but the upstream producer emits descriptive free text.

## Quick check

```python
from pf_core.utils.vocab import SlugNormalizer

normalizer = SlugNormalizer(
    canonical_slugs={"article", "memo", "post", "report"},
    synonyms={
        "blog article": "article",
        "social media post": "post",
        "white paper": "report",
    },
    explicit_rejects={"advertisement", "spam", "boilerplate"},
)

normalizer.normalize("Blog Article")        # "article"
normalizer.normalize("report")              # "report" (canonical)
normalizer.normalize("advertisement")       # None  (explicit reject)
normalizer.normalize("kerfuffle")           # None  (unknown free-text)
```

## Three lookup paths

A `SlugNormalizer` decides what to return by checking three sets in order:

1. **Pass-through** — the input is already in the canonical slug set (case-insensitive, whitespace-tolerant). Returned unchanged. Also tolerates space-for-underscore swaps: `"press release"` → `"press_release"` if the latter is canonical.
2. **Explicit reject** — the input is a category that should drop the row entirely. Returns `None`. Use `is_explicit_reject(raw)` to distinguish these from unknown inputs (which also return `None`).
3. **Synonym lookup** — the input is a known free-text variant. Returns the canonical slug it maps to.

Anything else returns `None`. The caller decides what `None` means in their domain (skip the row, raise a validation error, fall back to `"other"`, etc.).

## Class

### SlugNormalizer

```python
SlugNormalizer(
    *,
    canonical_slugs: set[str],
    synonyms: dict[str, str] | None = None,
    explicit_rejects: set[str] | None = None,
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `canonical_slugs` | `set[str]` | (required) | Valid slug set. Inputs already in this set pass through. Case-folded internally. |
| `synonyms` | `dict[str, str]` | `None` | Free-text variant → canonical slug. Keys are case-folded; values are returned as-given. |
| `explicit_rejects` | `set[str]` | `None` | Free-text values that mean "drop this row". Returned as `None` from `normalize`; flagged by `is_explicit_reject`. |

#### normalize

```python
normalizer.normalize(raw: str | None) -> str | None
```

Map `raw` to a canonical slug, or `None` if empty / unknown / explicit reject. Does not raise. Whitespace-normalized and case-insensitive.

#### is_explicit_reject

```python
normalizer.is_explicit_reject(raw: str | None) -> bool
```

`True` iff `raw` is in the explicit-reject set. Distinct from `normalize(raw) is None`: the latter is also true for unknown free-text. Use this when you need to distinguish "the producer named a category we deliberately drop" from "the producer named something we don't recognize".

## Precedence rules

When values appear in more than one set:

- **Canonical beats reject** — a slug listed in both `canonical_slugs` and `explicit_rejects` (a project-config bug) returns the canonical value. `is_explicit_reject` still returns `True` so the bug is detectable in audit code.
- **Reject beats synonym** — a value listed in both `synonyms` and `explicit_rejects` returns `None`. The "drop this" intent wins.

These are tested explicitly so consumers can rely on the behavior.

## Worked example — a content_type vocabulary

A consumer classifies ingested documents by *content type* (article, memo, post, report, etc.). The classifier LLM emits free-text labels like `"blog article"`, `"social media post"`, `"advertisement"`, but the entries table needs canonical slugs. `SlugNormalizer` centralizes the mapping:

```python
# myapp/content_types.py
from pf_core.utils.vocab import SlugNormalizer

_CONTENT_TYPES = {"article", "post", "report", "release", "newsletter", "guide"}

_NORMALIZER = SlugNormalizer(
    canonical_slugs=_CONTENT_TYPES,
    synonyms={
        "blog article": "article",
        "social media post": "post",
        "white paper": "report",
        "press release": "release",
        # ... ~150 more domain-specific mappings ...
    },
    explicit_rejects={
        "advertisement",  # ads aren't catalogued content
        "spam",            # junk, not content
        "boilerplate",     # template text, not content either
        # ... ~30 more non-content categories ...
    },
)

normalize_content_type = _NORMALIZER.normalize
is_explicit_non_content = _NORMALIZER.is_explicit_reject
```

A content-check rule then uses these:

```python
# myapp/content_rules.py
def check_content_type(data: dict) -> tuple[str, str]:
    raw = data.get("content_type")
    canonical = normalize_content_type(raw)
    if canonical is None:
        if is_explicit_non_content(raw):
            return "reject", f"content_type {raw!r} is an explicit non-content"
        return "reject", f"content_type {raw!r} is not in the controlled vocabulary"
    data["content_type"] = canonical  # normalize in place
    return "", ""
```

## When to use

Reach for `SlugNormalizer` when:

- An LLM (or any free-text producer) emits values that should map to a small fixed vocabulary.
- The mapping is mostly mechanical (synonyms, casing, whitespace) but some free-text values represent categories you want to *drop* rather than coerce.
- Your project has a controlled vocabulary in config and you want a single bottleneck for normalization rather than scattered `if/elif` chains across services.

Don't reach for it when:

- The mapping is so domain-specific that synonyms have to encode rules beyond a flat lookup (use a small dispatch function instead).
- You can fix the producer side cleanly (tighten the LLM prompt, change the upstream contract).

## See also

- `pf_core.utils.json` — same "make ad-hoc fallbacks tidy" spirit.
- `pf_core.utils.urls` — domain canonicalization for URL normalization (a different kind of vocabulary work).
