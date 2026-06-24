# `pf_core.utils.article_fetch`

Fetch and extract structured article metadata from a URL — title, body, publish date, and outlet — with automatic Wayback Machine fallback. Built for tools that read what a URL actually says (link verification, content matching, source archiving), not for general-purpose scraping.

## Why this exists

Most "fetch a URL" use cases in news/research tooling want the same shape:

1. Try the live URL.
2. If it's paywalled / blocked / errored, try the Wayback Machine.
3. Extract title, body, and publish date with reasonable fallbacks.
4. Return a structured result — never raise.

This module bundles that. It's the deterministic counterpart to LLM content-matching: a real article with real metadata that downstream tools (link matchers, content verifiers) can judge without making calls to the LLM.

## Installation

Requires the `articles` optional extra (heavy deps not in base pf-core):

```bash
pip install 'pf-core[articles]'
```

This pulls in `trafilatura` (extraction) and `htmldate` (date fallback). Without them, the module imports successfully but `fetch_article()` raises `ImportError` with a helpful message on first call.

## Usage

```python
from pf_core.utils.article_fetch import fetch_article

art = fetch_article("https://example.com/news/fusion-milestone")

if art.fetch_status == "ok":
    print(art.title)              # "Scientists report fusion milestone..."
    print(art.date_published)     # date(2026, 4, 15)
    print(art.body[:500])         # extracted prose
    print(art.outlet)             # "example.com"
elif art.fetch_status == "paywalled":
    # Article exists but content is behind a paywall.
    # Wayback may have been tried already (check art.used_wayback).
    pass
elif art.fetch_status == "not_found":
    # 404/410 — URL is dead. Often a sign of fabrication.
    pass
```

### Wayback fallback

By default, `fetch_article` falls back to the Wayback Machine for recoverable failures (paywalled, blocked, timeout, error — but NOT 404/410, since those rarely have captures and aren't worth the latency). Disable globally via env:

```bash
PF_ARTICLE_WAYBACK_FALLBACK=0
```

Or per-call:

```python
art = fetch_article(url, wayback_fallback=False)
```

When the Wayback fetch succeeds, `art.used_wayback=True` and `art.final_url` is the `web.archive.org` URL. The `art.url` and `art.canonical_url` always reflect what the caller passed.

### Date hint for Wayback snapshot selection

When you have an approximate event date and want a contemporaneous snapshot rather than the latest capture:

```python
from datetime import date

art = fetch_article(
    url,
    event_date=date(2025, 3, 15),
)
# Wayback CDX lookup prefers a snapshot within ±14 days of event_date.
```

## `FetchedArticle`

```python
@dataclass
class FetchedArticle:
    url: str                              # original URL caller passed
    final_url: str                        # what we actually fetched
    fetch_status: str                     # ok | paywalled | not_found | blocked | timeout | error
    used_wayback: bool                    # True if content came from web.archive.org
    title: str                            # extracted title or ""
    date_published: date | None           # extracted publication date
    body: str                             # extracted body, trimmed to 8000 chars
    outlet: str                           # domain of url (e.g. "example.com")
    canonical_url: str                    # dedup-key form via canonical_url()
    raw_meta: dict                        # full extractor output for debugging
```

## Status values

| Status | Meaning |
|---|---|
| `ok` | 2xx response, body extracted, not paywalled |
| `paywalled` | 401/403, OR short body + CTA markers ("subscribe to continue", etc.) |
| `not_found` | 404 or 410 (final — Wayback NOT attempted) |
| `blocked` | Other non-2xx (429, 500, etc.) |
| `timeout` | Request timed out after 3 retries |
| `error` | Transport error after 3 retries |

## Extractor chain

The body/title/date extraction runs a fallback chain:

1. **trafilatura precision mode** (default) — clean bodies for well-structured pages.
2. **trafilatura recall mode fallback** — when precision returns empty (some non-standard layouts like PBS NewsHour).
3. **htmldate** — aggressive publish-date detection when trafilatura missed.
4. **URL path date** (`/YYYY/MM/DD/` pattern) — last resort for sites that bury the date in metadata.

Body is trimmed to 8000 characters by default (enough for downstream content matching; fewer tokens for LLM consumers).

## Paywall detection

A 2xx response with extracted body shorter than 500 characters AND containing one of these markers is reclassified as `paywalled`:

```
subscribe to continue, subscribe to read, sign in to continue,
sign up to continue, continue reading, to read the rest of,
already a subscriber, unlock this article, this article is for,
for subscribers only, become a member, support our work
```

Wayback fallback fires for these too — many paywalled live URLs have free Wayback captures.

## Retry behavior

Transient failures (timeout, transport error, empty 2xx body) retry up to 3 times with exponential backoff and jitter (initial 2s, max 30s). Deterministic failures (404, 403, 500) return immediately — retrying them is pure waste.

## Caching

This module does NOT cache. Project-side cache is the right pattern because cache invalidation is project-specific (which DB, what schema, how to handle multi-tenancy).

The :data:`FETCHER_VERSION` constant bumps when extraction logic changes in a way that invalidates previously-cached results — your project-side cache should treat rows stamped with a different version as misses.

Reference cache implementation pattern:

```python
def cached_fetch(url: str, *, event_date=None, use_cache=True):
    canon = canonical_url(url) or url
    if use_cache:
        row = my_db.get_cached_article(canon, fetcher_version=FETCHER_VERSION)
        if row:
            return rebuild_article(row, url=url)

    art = fetch_article(url, event_date=event_date)

    # Cache terminal states (ok, paywalled, not_found, blocked).
    # Don't cache transient errors (timeout, error) — they may recover.
    if use_cache and art.fetch_status not in ("timeout", "error"):
        my_db.upsert_cached_article(art, fetcher_version=FETCHER_VERSION)
    return art
```

## Never raises

The "never raises" contract is load-bearing. Callers branch on `fetch_status`, not exception handling. Internal exceptions (trafilatura parse failures, network errors, invalid HTML) all map to the appropriate status value. The only exception path is `ImportError` at first call when the `articles` extra isn't installed — and that's at the import boundary, not the call boundary, in spirit.

## See also

- `pf_core.utils.urls` — `fetch_url_content`, `wayback_exists_at`, `canonical_url`, `domain_of`, `extract_path_date` — the lower-level primitives this module composes
- `pf_core.utils.relative_dates` — for resolving the date phrases that LLMs emit when describing events from articles
