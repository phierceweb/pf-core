# URL Utilities

General-purpose URL parsing and inspection helpers.

The pure parsing helpers — `domain_of`, `canonical_url`,
`archive_timestamp_is_round`, `extract_path_date`, `extract_article_metadata` —
need **no extra at all**. Import them from `pf_core.utils`, or directly from
`pf_core.utils.url_parse` / `pf_core.utils.url_html`.

The helpers that make requests — `check_url`, `fetch_url_content`,
`wayback_exists_at`, `check_url_cached` — live in `pf_core.utils.urls` /
`pf_core.utils.url_liveness` and require the `[http]` extra.

## Domain extraction

```python
from pf_core.utils import domain_of

domain_of("https://www.example.com/page")   # "example.com"
domain_of("https://blog.example.com/page")  # "blog.example.com"
domain_of("https://example.com:8080/path")  # "example.com"
```

Strips `www.` prefix and lowercases the hostname. Returns empty string for unparseable input.

## URL canonicalization (for deduplication)

```python
from pf_core.utils import canonical_url

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
from pf_core.utils import archive_timestamp_is_round

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

Every outbound request in this module (`check_url`, `fetch_url_content`, `wayback_exists_at`, and the `url_liveness` GET fallback) **verifies TLS certificates by default**. Set `PF_VERIFY_TLS=0` to disable verification — but only for deliberately probing hosts with known-broken certs. Disabling removes MITM protection, and since `fetch_url_content`'s body flows to downstream LLMs, a MITM could inject content.

The switch is **process-wide, not module-scoped**: it also disables verification for [`pf_core.fetch`](fetch.md), the base-install download path. To turn it off for one client only, use that module's per-`Fetcher` `verify_tls=False`.

`URL_CHECK_VERIFY_TLS` is still honored as a legacy alias; `PF_VERIFY_TLS` wins when both are set. Resolved via `pf_core.utils.http_tls.verify_tls()`, which this module reads per call.

### SSRF protection

`check_url` and `fetch_url_content` accept caller-influenced URLs, so they are guarded against server-side request forgery: the target — and every redirect hop — must use an http/https scheme and resolve to a **public** address. A URL that resolves to loopback, link-local (incl. `169.254.169.254` cloud metadata), private, reserved, multicast, or carrier-grade-NAT shared space (`100.64.0.0/10` — where EKS/GKE pod addresses live) is refused, and the call returns its normal failure tuple (`(0, "error")` / `(0, "error", "")`) with an `ssrf_blocked` warning logged. Set `URL_FETCH_ALLOW_PRIVATE=1` to allow internal targets (service mesh, dev) — the http/https scheme requirement still applies. Implemented in `pf_core.utils.url_safety`.

#### What it does not cover: DNS rebinding

The guard resolves the host to decide, then httpx resolves it again to open the connection. A host whose DNS answer flips between those two lookups — a short-TTL record that answers the check with a public address and the connect with `127.0.0.1` — **passes the check and is then fetched from the private address**. This is a time-of-check/time-of-use gap, and `check_url` / `fetch_url_content` / `check_url_cached` do not close it: they validate the URL and hand it to httpx, which resolves independently. Read the guarantee as "blocks statically-internal targets" (literal private IPs, hostnames with stable private records), not "safe against an attacker who controls DNS for the host they gave you".

`assert_public_url` returns the tuple of addresses it vetted (empty when `URL_FETCH_ALLOW_PRIVATE` skipped the address check) so a caller *can* close the gap — connect to one of the returned IPs directly, carrying the original hostname in the `Host` header and TLS SNI. That pinning is the caller's job; no pf-core fetch helper does it today.

```python
from pf_core.utils.url_safety import assert_public_url

ips = assert_public_url("https://example.com/x")   # ("<ip>", …) in resolution order, may be IPv6 first
```

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
    negative_cache_ttl_seconds=300,   # timeouts/5xx expire fast, not tomorrow
)

# Operator kill switch — caller derives the boolean however it wants
# (env var, config flag, runtime toggle):
check_url_cached(url, disabled=os.environ.get("URL_LIVENESS_DISABLED") == "1")
```

### What this adds over `check_url`

- **403/401 fallback.** Many real sites return 403 to bare HEAD even though their content is real. `check_url_cached` re-issues the request as GET with a browser User-Agent and `follow_redirects=True`, so a 200 via GET correctly downgrades the verdict from "forbidden" to "ok". Distinguishes a real bot-block from a dead link.
- **Two-tier caching.** Result cached at `cache_key_prefix + url`. A stable verdict (`ok`, `not_found`, `gone`, `forbidden`) keeps `cache_ttl_seconds` — `None` reads `URL_LIVENESS_TTL_SECONDS`, default 24h. A **transient** verdict — `timeout`, `error`, or a retryable status (408, 425, 429, 5xx) — gets `negative_cache_ttl_seconds` instead: `None` reads `URL_LIVENESS_NEGATIVE_TTL_SECONDS`, default 300s. Without the split, a 30-second network interruption marks every URL checked in that window dead for a day. Transient verdicts are still cached, not skipped, so a hard-down host stays throttled. Cache failures (corrupt value, backend exception) silently fall through to a fresh network check — never throws.
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
| `cache_ttl_seconds` | `int \| None` | `None` | TTL for stable verdicts. `None` reads `URL_LIVENESS_TTL_SECONDS` (default `86400`). |
| `negative_cache_ttl_seconds` | `int \| None` | `None` | TTL for transient verdicts (`timeout`, `error`, 408/425/429/5xx). `None` reads `URL_LIVENESS_NEGATIVE_TTL_SECONDS` (default `300`). |
| `cache_key_prefix` | `str` | `"url_liveness:"` | Prefix prepended to URL to form the cache key. |
| `disabled` | `bool` | `False` | When `True`, returns `(0, "disabled")` without network or cache activity. |

Returns `(status_code, category)`. Categories include all of `check_url`'s plus `disabled`.

## URL path date extraction

```python
import datetime
from pf_core.utils import extract_path_date

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

Same semantics as `check_url` but always does GET and returns the body on 2xx. Empty body on any non-2xx or error — callers should branch on `category`.

The body is **streamed** and the read stops at 512 KB, so the cap bounds what is pulled into memory rather than being applied after the fact. The request asks for `Accept-Encoding: identity`: at that cap compression saves almost nothing on the wire, and asking for identity keeps a compressed response from inflating inside httpx's decoder before the cap can apply.

A server that sends `Content-Encoding: gzip` regardless is not fully bounded — httpx decodes a whole network chunk before the loop sees it, so peak memory is one chunk's worth of inflated output rather than 512 KB. It is bounded, not unbounded, but the ceiling is set by the chunk size and the payload's compression ratio rather than by the cap. For untrusted hosts, prefer `pf_core.fetch.Fetcher` with `max_bytes`, which bounds the decoded body directly.

Bytes are decoded with the response charset (utf-8 when unset) and **error replacement**, so an undecodable byte becomes U+FFFD rather than disappearing. Callers that sniff magic bytes on the returned text — `article_fetch.looks_binary` does — depend on that: dropping the byte instead would make a PNG read as ordinary markup.

### fetch_url_content

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `url` | `str` | *(required)* | HTTP(S) URL to fetch |
| `timeout` | `int \| None` | `None` | Request timeout in seconds; reads `URL_CHECK_TIMEOUT` env var (default `8`) when `None` |

Returns `(status_code, category, body)` where category matches `check_url` and body is `str`.

## Extracting article metadata

```python
from pf_core.utils import extract_article_metadata

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
from pf_core.utils import archive_timestamp_is_round, domain_of  # noqa: F401
```

No downstream caller changes needed — all callers import from `app.utils.sources`.
