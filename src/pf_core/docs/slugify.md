# Slugify

Fold free text to a stable lowercase ASCII slug for filenames, ids, and URL fragments. Not to be confused with [`vocab`](vocab.md)'s `SlugNormalizer`, which maps free text onto a *known* controlled vocabulary (with synonym and explicit-reject handling) — use `slugify` when the slug is derived from arbitrary text, use `vocab` when the output must be one of a fixed set.

---

## Table of Contents

- [Quick usage](#quick-usage)
- [Function](#function)
- [Stability contract](#stability-contract)
- [Relationship to other helpers](#relationship-to-other-helpers)

## Quick usage

```python
from pf_core.utils.slugify import slugify   # also re-exported from pf_core.utils

slugify("São Paulo")            # "sao-paulo"
slugify("rock 'n' roll")        # "rock-n-roll"
slugify("Straße 9", sep="_")    # "strasse_9"
slugify("★☆★")                  # "" — caller owns the empty-slug fallback
```

## Function

### slugify

Fold `text` to a slug: strip + lowercase, map special letters NFKD can't decompose (ø→o, å→a, æ→ae, œ→oe, ð→d, þ→th, ł→l, ß→ss), NFKD-decompose and drop combining marks, drop remaining non-ASCII, collapse every non-alphanumeric run to one `sep`, trim leading/trailing separators.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `text` | `str` | *(required)* | Free text. No ASCII alphanumerics (including `""`) → returns `""`. |
| `sep` | `str` | `"-"` | Separator between word runs (keyword-only). |

Pure stdlib; deterministic; idempotent (`slugify(slugify(x)) == slugify(x)`).

## Stability contract

Consumers persist slugify output as durable keys (dedupe ledgers, entity primary keys, generated filenames). Do not change the fold steps or extend the special-letter map casually — any output change for any input is a **behavior change**: existing stored slugs stop matching newly computed ones. Migrating a project from a hand-rolled slugifier onto this one: diff the two functions' outputs over the project's real key population first.

## Relationship to other helpers

- [`vocab`](vocab.md) — the other slug helper: normalizes into a fixed vocabulary instead of generating from text.
- [`ids`](ids.md) — when you need a *random* URL-safe identifier rather than one derived from a name.
- [`hashing`](hashing.md) — when the key must reflect full content, not a human-readable name.
