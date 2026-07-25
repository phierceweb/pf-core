# Fetch

Polite, SSRF-guarded HTTP fetching over stdlib `urllib` — no extra required, ships in the base install. Not to be confused with [`urls`](urls.md) / [`article-fetch`](article-fetch.md), the httpx-based URL-*inspection* tier behind `[http]`/`[articles]` (liveness checks, Wayback probes, article extraction): use `pf_core.fetch` when your project **downloads content** and wants pacing, retries, and safety without adding httpx; use the `[http]` tier when you need its inspection helpers and already accept httpx.

---

## Table of Contents

- [Quick usage](#quick-usage)
- [Fetcher](#fetcher)
- [Retry contract](#retry-contract)
- [Safety: SSRF guard and redirects](#safety-ssrf-guard-and-redirects)
- [Conditional GETs](#conditional-gets)
- [browser_headers](#browser_headers)
- [Relationship to other helpers](#relationship-to-other-helpers)

## Quick usage

```python
from pf_core.fetch import fetch_text, fetch_bytes, Fetcher
from pf_core.utils.throttle import Throttle

final_url, text = fetch_text("https://example.com/docs/index.html")
final_url, data = fetch_bytes("https://example.com/files/report.pdf")

# A configured client for a crawl: identifying UA, 1 req/s pacing, 25 MiB cap
fetcher = Fetcher(
    user_agent="mytool/1.0 (+https://example.com/mytool)",
    throttle=Throttle.per_second(1),
    max_bytes=25 * 1024 * 1024,
)
final_url, data = fetcher.get_bytes(url)
```

Module-level `fetch_text` / `fetch_bytes` / `fetch_bytes_meta` / `not_modified` build a default `Fetcher` per call — fine for one-off fetches. Construct a `Fetcher` when a crawl shares configuration across many requests.

The first tuple element is always the **final URL after redirects** — stamp it into provenance comments, derive filenames from it, and `urljoin` follow-up requests against it, never against the URL you requested.

## Fetcher

Constructor knobs (all keyword-only):

| Knob | Default | Meaning |
|---|---|---|
| `user_agent` | kwarg > `PF_FETCH_UA` env > identifying pf-core default | Sent on every request. Give crawls a real contact UA. |
| `headers` | `None` | Merged over the default UA/Accept/Accept-Language set. |
| `retries` | `2` | Re-attempts after the first try. |
| `throttle` | `None` | A [`Throttle`](throttle.md); acquired before **every** request, including retries and redirect hops. |
| `max_bytes` | `None` | Streamed size cap; overrun raises `ClientError`. |
| `require_public` | `True` | SSRF guard — see below. |
| `max_redirects` | `5` | Hop budget for the manual redirect walk. |

`get_text` decodes with `encoding` kwarg > Content-Type charset > utf-8, always with replacement. `get_bytes` defaults to a longer timeout than `get_text` (binary payloads run large on slow CDNs); both take a per-call `timeout_s`. Responses with `Content-Encoding: gzip`/`deflate` are decoded transparently; the default request headers advertise no `Accept-Encoding`, so servers send identity unless you opt in via headers.

TLS verification follows `URL_CHECK_VERIFY_TLS` (default on), the same policy as the `[http]` tier.

## Retry contract

Ported intact from a proven crawl implementation; callers rely on every clause:

- **Permanent client errors (4xx except 408) raise immediately, with zero sleeps.** Callers implement their own 403 cooldowns and 404 fallbacks — an internal retry would double-pace them.
- **429 honors `Retry-After`**, capped at 30 s so a server can't park a crawl; malformed values fall back to the standard backoff.
- **5xx / 408 / network errors** back off `0.5 * (attempt + 1)` between attempts.
- **Exhausted retries re-raise the last exception raw.** `pf_core.fetch` never wraps transport errors: you get real `urllib.error.HTTPError` (branch on `.code`) and `URLError`. The only exceptions it *originates* are `InvalidInputError` (SSRF block) and `ClientError` (size cap).

## Safety: SSRF guard and redirects

Redirects are walked manually: each 3xx hop's `Location` is resolved and — when `require_public` is on — re-validated with [`url_safety.assert_public_url`](urls.md) before it is followed, so a public URL cannot bounce the fetch onto localhost, a private range, or a cloud-metadata endpoint. Blocked targets raise `InvalidInputError`; unresolvable hosts fail closed. Exceeding `max_redirects` raises the last 3xx `HTTPError`.

`URL_FETCH_ALLOW_PRIVATE=1` opts out of the address check (dev, local mirrors); pass `require_public=False` when a specific client legitimately targets private hosts.

## Conditional GETs

`get_bytes_meta` returns the response's cache validators (`Validators`: `etag`, `last_modified`) alongside the body, for callers that persist them. `not_modified(url, etag=..., last_modified=...)` performs one conditional GET and returns `True` **only on a definitive 304** — anything else (changed content, missing validators, any error) returns `False` so the caller can always fall back to a full fetch. It never raises, and sends no request when both validators are `None`.

## browser_headers

`browser_headers()` returns a fresh copy of a full browser-fingerprint header set (real browser UA, `Accept`, `Accept-Language`, `Sec-Fetch-*`, `Accept-Encoding: gzip, deflate` — never brotli). Some sites 403 the lean default header set on their *public* pages; pass this when a legitimate fetch of public content hits bot blocking:

```python
fetcher = Fetcher(headers=browser_headers())
```

Prefer the identifying default UA everywhere it works — the fingerprint set is for sources that reject it, not a first resort.

## Relationship to other helpers

- [`throttle`](throttle.md) — the pacing primitive `Fetcher` composes; use it directly for non-HTTP rate limits.
- [`urls`](urls.md) — httpx URL-inspection tier (`check_url`, `fetch_url_content`) behind `[http]`; shares the SSRF guard.
- [`article-fetch`](article-fetch.md) — extraction-grade article fetching with Wayback fallback, behind `[articles]`.
- [`fetch-images`](fetch-images.md) — the markdown remote-image localizer built on this module.
