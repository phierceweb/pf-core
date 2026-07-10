# URL Utilities

General-purpose URL parsing and inspection helpers. Import everything from
`pf_core.utils.urls` (requires the `[http]` extra). The pure halves also
import directly with **no extra at all**: `pf_core.utils.url_parse`
(`domain_of`, `canonical_url`, `archive_timestamp_is_round`,
`extract_path_date`) and `pf_core.utils.url_html`
(`extract_article_metadata`).

## Domain extraction

```python
from pf_core.utils.urls import domain_of

domain_of("https://www.example.com/page")   # "example.com"
domain_of("https://blog.example.com/page")  # "blog.example.com"
domain_of("https://example.com:8080/path")  # "example.com"
```

Strips `www.` prefix and lowercases the hostname. Returns empty string for unparseable input.

## URL canonicalization (for deduplication)

```python
from pf_core.utils.urls import canonical_url

canonical_url("https://www.example.com/story?utm_source=newsletter&utm_medium=email")
# "https://example.com/story"

canonical_url("http://example.com/x?fbclid=abc#section-2")
# "https://example.com/x"

# Same article via three different share paths — all canonicalize the same:
canonical_url("https://example.com/article/foo?utm_source=newsletter")
canonical_url("https://www.example.com/article/foo/")
# both → "https://example.com/article/foo"
```

Produces a canonical form so URLs referencing the same resource via different link shapes compare equal — a newsletter link, a search result, and a Twitter share of the same article all produce the same canonical string.

Normalization applied (in order):

- Scheme lowercased; `http` upgraded to `https` (same resource for dedup)
- Non-HTTP schemes (`mailto:`, `file:`, etc.) → `""`
- Hostname lowercased; `www.` prefix stripped; user credentials dropped
- Default ports (80 for http, 443 for https) stripped
- Fragment (`#…`) dropped
- Tracking query params dropped — `utm_*`, `fbclid`, `gclid`, `mc_cid`, `_ga`, `__hs*`, `pk_*`, `vero_*`, and a handful more
- Remaining query params sorted alphabetically (so `?a=1&b=2` and `?b=2&a=1` match)
- Path case preserved (RFC 3986); empty/root path → `/`; trailing slash stripped on deeper paths
- Idempotent: `canonical_url(canonical_url(x)) == canonical_url(x)`

Use `canonical_url` as the dedup key when persisting source URLs — store `url` for display and `canonical_url` for matching. MySQL JSON multi-valued indexes over `$[*].canonical_url` paths accelerate cross-corpus lookups (`MEMBER OF`).

### canonical_url

| Parameter | Type | Description |
|-----------|------|-------------|
| `url` | `str` | Any URL string |

Returns `str` — canonical URL, or `""` if the input is empty, non-string, non-HTTP, or unparseable.

**Non-goals.** Does not percent-decode paths (case-sensitive, server-defined) and does not perform IDN/punycode conversion on hostnames. For source-URL dedup this is adequate; cross-locale URL equivalence is out of scope.

## Archive timestamp detection

```python
from pf_core.utils.urls import archive_timestamp_is_round

archive_timestamp_is_round(
    "https://web.archive.org/web/20250101000000/https://example.com"
)  # True — midnight timestamps are almost always fabricated
```

Returns `True` if a `web.archive.org` URL has a suspiciously round midnight timestamp (14-digit timestamp ending in `000000`). Non-archive URLs return `False`.

## Functions

### domain_of

| Parameter | Type | Description |
|-----------|------|-------------|
| `url` | `str` | Any URL string |

Returns `str` — lowercase domain with `www.` stripped.

### archive_timestamp_is_round

| Parameter | Type | Description |
|-----------|------|-------------|
| `url` | `str` | Any URL (non-archive returns `False`) |

Returns `bool`.

## URL health checking

```python
from pf_core.utils.urls import check_url

status, category = check_url("https://example.com")           # (200, "ok")
status, category = check_url("https://example.com/missing")   # (404, "not_found")
status, category = check_url("https://down.invalid")          # (0, "error")
```

HEAD request with GET fallback on 405 or transport error. Browser-like User-Agent. Follows redirects.

### TLS verification

Every outbound request in this module (`check_url`, `fetch_url_content`, `wayback_exists_at`, and the `url_liveness` GET fallback) **verifies TLS certificates by default**. Set `URL_CHECK_VERIFY_TLS=0` to disable verification — but only for deliberately probing hosts with known-broken certs. Disabling removes MITM protection, and since `fetch_url_content`'s body flows to downstream LLMs, a MITM could inject content. Resolved via `pf_core.utils.http_tls.verify_tls()` (reads the env var per call, default `True`).

### SSRF protection

`check_url` and `fetch_url_content` accept caller-influenced URLs, so they are guarded against server-side request forgery: the target — and every redirect hop — must use an http/https scheme and resolve to a **public** address. A URL that resolves to loopback, link-local (incl. `169.254.169.254` cloud metadata), private, reserved, or multicast space is refused, and the call returns its normal failure tuple (`(0, "error")` / `(0, "error", "")`) with an `ssrf_blocked` warning logged. Set `URL_FETCH_ALLOW_PRIVATE=1` to allow internal targets (service mesh, dev) — the http/https scheme requirement still applies. Implemented in `pf_core.utils.url_safety`.

### check_url

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `url` | `str` | *(required)* | HTTP(S) URL to check |
| `timeout` | `int \| None` | `None` | Timeout in seconds. `None` reads `URL_CHECK_TIMEOUT` env var (default `8`) |

Returns `(status_code, category)`. Categories: `ok`, `not_found`, `forbidden`, `gone`, `timeout`, `error`, `http_{NNN}`.

## Cached liveness check

`pf_core.utils.url_liveness.check_url_cached` wraps `check_url` with a TTL'd cache, browser-UA GET fallback for 403/401, and a kill-switch boolean. Use it any time the same URL might be checked again — periodic audits, batch dedup, revalidation loops.

```python
from pf_core.utils.url_liveness import check_url_cached

# No cache — same shape as check_url, plus the GET fallback:
check_url_cached("https://example.com/article/x")  # (200, "ok") via GET-fallback even if HEAD 403s

# With redis-py (or anything matching CacheBackend):
import redis
r = redis.from_url("redis://localhost:6379/0")
check_url_cached(
    "https://example.com/x",
    cache=r,
    cache_key_prefix="myapp:url_liveness:",
    cache_ttl_seconds=86400,
)

# Operator kill switch — caller derives the boolean however it wants
# (env var, config flag, runtime toggle):
check_url_cached(url, disabled=os.environ.get("URL_LIVENESS_DISABLED") == "1")
```

### What this adds over `check_url`

- **403/401 fallback.** Many real sites return 403 to bare HEAD even though their content is real. `check_url_cached` re-issues the request as GET with a browser User-Agent and `follow_redirects=True`, so a 200 via GET correctly downgrades the verdict from "forbidden" to "ok". Distinguishes a real bot-block from a dead link.
- **Caching.** Result cached at `cache_key_prefix + url` for `cache_ttl_seconds` (default 24h). Cache failures (corrupt value, backend exception) silently fall through to a fresh network check — never throws.
- **Kill switch.** `disabled=True` returns `(0, "disabled")` with no network or cache activity. Useful during incidents.

### CacheBackend protocol

Tiny Protocol — anything with `get(key) -> bytes | str | None` and `setex(key, ttl, value)` works. `redis-py`'s `Redis` client matches without an adapter.

```python
from pf_core.utils.url_liveness import CacheBackend

class MyCache:
    def get(self, key: str) -> bytes | None: ...
    def setex(self, key: str, time: int, value: str) -> None: ...

# Pass any CacheBackend-shaped object:
check_url_cached(url, cache=MyCache())
```

`cache=None` (the default) disables caching entirely without any code-path branching at the call site — the in-process state machine is the same; only the read/write operations are skipped.

### What's deliberately NOT here

A "trusted domain" short-circuit (skipping liveness for known-good trusted sites). That list is project policy, not framework infrastructure. Wrap `check_url_cached` with a 5-line consumer-side wrapper when you need it.

### check_url_cached

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `url` | `str` | *(required)* | HTTP(S) URL to check. Empty string returns `(0, "error")`. |
| `cache` | `CacheBackend \| None` | `None` | Optional cache backend. `None` disables caching. |
| `cache_ttl_seconds` | `int` | `86400` | TTL for cached entries. |
| `cache_key_prefix` | `str` | `"url_liveness:"` | Prefix prepended to URL to form the cache key. |
| `disabled` | `bool` | `False` | When `True`, returns `(0, "disabled")` without network or cache activity. |

Returns `(status_code, category)`. Categories include all of `check_url`'s plus `disabled`.

## URL path date extraction

```python
import datetime
from pf_core.utils.urls import extract_path_date

extract_path_date("https://www.example.com/2025/03/15/section/story.html")
# datetime.date(2025, 3, 15)

extract_path_date("https://example.com/article/abc")
# None

extract_path_date("https://example.com/2025/02/30/story")
# None — invalid calendar date
```

Returns the first valid `/YYYY/MM/DD/` segment found in a URL path. Useful for cross-checking a source URL's self-reported date against the date an event is claimed to have happened: a mismatch is a hallucination signal.

### extract_path_date

| Parameter | Type | Description |
|-----------|------|-------------|
| `url` | `str` | Any URL |

Returns `datetime.date \| None`. Accepts `/YYYY/MM/DD/` with 19xx or 20xx years and 1- or 2-digit month/day. Rejects impossible dates (Feb 30 → `None`) and hyphen forms (`/2025-03-15/` → `None`).

## Wayback snapshot verification

```python
import datetime
from pf_core.utils.urls import wayback_exists_at

# Did web.archive.org capture this URL within 14 days of 2025-03-15?
exists, snapshot = wayback_exists_at(
    "https://www.example.com/story",
    at=datetime.date(2025, 3, 15),
)
# (True, "https://web.archive.org/web/20250315123045/https://www.example.com/story")

# Without a date, any captured snapshot qualifies.
exists, snapshot = wayback_exists_at("https://www.example.com/story")
```

Queries the public Wayback CDX API (no auth). Returns `(False, None)` on any error or missing snapshot — callers treat "unknown" as "not verified" rather than raising. A URL that a consumer claims existed on a date but has zero Wayback captures near that date is a strong fabrication signal.

### wayback_exists_at

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `url` | `str` | *(required)* | The target URL to check |
| `at` | `datetime.date \| None` | `None` | Reference date. `None` accepts any captured snapshot |
| `tolerance_days` | `int` | `14` | Accept snapshots within `±tolerance_days` of `at` |
| `timeout` | `int \| None` | `None` | Request timeout in seconds (reads `WAYBACK_TIMEOUT` when `None`, default `30`) |

Returns `(exists, snapshot_url)`. `snapshot_url` is a full `web.archive.org/web/...` URL when a snapshot is found; `None` otherwise.

## Fetching page content

```python
from pf_core.utils.urls import fetch_url_content

code, category, body = fetch_url_content("https://example.com/article")
# (200, "ok", "<html>…</html>")

code, category, body = fetch_url_content("https://paywalled.example/article")
# (403, "forbidden", "")
```

Same semantics as `check_url` but always does GET and returns the body on 2xx. Body is truncated at 512 KB to protect downstream token budgets. Empty body on any non-2xx or error — callers should branch on `category`.

### fetch_url_content

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `url` | `str` | *(required)* | HTTP(S) URL to fetch |
| `timeout` | `int \| None` | `None` | Request timeout in seconds; reads `URL_CHECK_TIMEOUT` env var (default `8`) when `None` |

Returns `(status_code, category, body)` where category matches `check_url` and body is `str`.

## Extracting article metadata

```python
from pf_core.utils.urls import extract_article_metadata

html = "<html><head><title>Quarterly Results Announced</title>..."
metadata = extract_article_metadata(html)
# {
#   "title": "Quarterly Results Announced",
#   "description": "",
#   "og_title": "Quarterly Results Announced",
#   "og_description": "The company reported record revenue …",
#   "twitter_title": "",
#   "twitter_description": "",
#   "first_paragraph": "The company on Monday announced…"
# }
```

Uses only stdlib `html.parser` — no beautifulsoup / readability dependency. The goal is a lightweight topic-sniff for LLM content-match checks, not full article extraction. Malformed HTML degrades gracefully.

### extract_article_metadata

| Parameter | Type | Description |
|-----------|------|-------------|
| `html` | `str` | Raw HTML as returned by `fetch_url_content` |

Returns `dict[str, str]` with keys `title`, `description`, `og_title`, `og_description`, `twitter_title`, `twitter_description`, `first_paragraph`. Missing fields are empty strings (never absent keys).

## Related

- [LLM URL Check](llm-validation.md) — uses these utilities to detect hallucinated URLs
- [Article Fetch](article-fetch.md) — composes `fetch_url_content`, `wayback_exists_at`, `canonical_url`, `domain_of`, and `extract_path_date` into a fetch-and-extract pipeline with Wayback fallback

## Migrating from consumer projects

**Example consumer** — `app/utils/sources.py` re-exports from pf-core:

```python
# Before
def domain_of(url): ...
def archive_timestamp_is_round(url): ...

# After
from pf_core.utils.urls import archive_timestamp_is_round, domain_of  # noqa: F401
```

No downstream caller changes needed — all callers import from `app.utils.sources`.
