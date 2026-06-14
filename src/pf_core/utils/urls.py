"""URL parsing and inspection utilities.

General-purpose URL helpers used across the framework — not specific to
LLM content validation.

Usage::

    from pf_core.utils.urls import domain_of, archive_timestamp_is_round, check_url

    domain_of("https://www.example.com/page")  # "example.com"
    status, category = check_url("https://example.com")  # (200, "ok")
"""

from __future__ import annotations

import datetime
import json as _json
import os
import re
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

try:
    import httpx
except ImportError as e:  # pragma: no cover - exercised by bare-install CI
    from pf_core._extras import extra_import_error

    raise extra_import_error("http", "httpx", feature="pf_core.utils.urls") from e

from pf_core.utils.http_tls import verify_tls
from pf_core.utils.url_safety import guarded_get, guarded_head

# Matches /YYYY/MM/DD/ segments embedded in a URL path. Requires slash
# boundaries so "/2025-03-15" or "part-2025-03-15" do not match.
_PATH_DATE_RE = re.compile(r"/(?P<year>(?:19|20)\d{2})/(?P<month>\d{1,2})/(?P<day>\d{1,2})(?:/|$)")


def domain_of(url: str) -> str:
    """Extract the registrable domain from a URL, stripping ``www.`` prefix.

    Args:
        url: Any URL string (with or without scheme).

    Returns:
        Lowercase domain string, or empty string if unparseable.
    """
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


# Pure tracking / attribution query params — dropped when deduplicating URLs so
# the same article reached via different shares collapses to one canonical URL.
_TRACKING_PARAM_PREFIXES: tuple[str, ...] = ("utm_", "__hs", "pk_", "vero_")
_TRACKING_PARAMS: frozenset[str] = frozenset({
    "fbclid",                 # Facebook click ID
    "gclid",                  # Google Ads click ID
    "dclid",                  # Google Campaign Manager
    "gbraid",                 # Google ad network
    "wbraid",                 # Google ad network (web)
    "msclkid",                # Microsoft Ads
    "yclid",                  # Yandex click ID
    "_ga",                    # Google Analytics cross-domain
    "_gl",                    # Google Analytics cross-domain
    "mc_cid",                 # Mailchimp campaign ID
    "mc_eid",                 # Mailchimp email ID
    "ref_src",                # generic referrer source (Twitter share)
    "ref_url",
    "referrer",
    "__twitter_impression",
    "hsctatracking",          # HubSpot CTA
    "igshid",                 # Instagram share
    "si",                     # YouTube share identifier
})

# Ports that are always redundant in the canonical (https) form — 80 is
# http's default, 443 is https's default, and we upgrade http→https so both
# should be elided when they appear.
_CANONICAL_DEFAULT_PORTS: frozenset[int] = frozenset({80, 443})


def canonical_url(url: str) -> str:
    """Normalize a URL for deduplication comparison.

    Produces a canonical form so URLs that reference the same resource via
    different link shapes compare equal — one article accessed via a newsletter's
    ``?utm_source=newsletter`` link, a bare URL from a search result, and a
    Twitter share with ``?s=21`` all produce the same canonical string.

    Normalization rules:

    - Scheme lowercased; ``http`` upgraded to ``https`` (for dedup
      purposes the two are treated as the same resource)
    - Non-HTTP schemes (``mailto:``, ``file:``, etc.) return ``""``
    - Hostname lowercased; ``www.`` prefix stripped; credentials
      (``user:pass@``) dropped
    - Default ports (80 for http, 443 for https) stripped
    - Fragment (``#…``) dropped entirely
    - Tracking query params removed — ``utm_*``, ``fbclid``, ``gclid``,
      ``mc_cid``, ``_ga``, etc. See ``_TRACKING_PARAMS`` /
      ``_TRACKING_PARAM_PREFIXES`` for the full list
    - Remaining query params sorted alphabetically by key so
      ``?a=1&b=2`` and ``?b=2&a=1`` match
    - Path preserved case-sensitively (per RFC 3986); empty or bare ``/``
      path normalized to ``/``; trailing slash stripped on non-root paths
    - Idempotent: ``canonical_url(canonical_url(x)) == canonical_url(x)``

    Args:
        url: Any URL string. Empty, non-string, or unparseable input
            returns ``""`` — callers should treat an empty return as
            "not a URL" rather than "canonical form of empty input."

    Returns:
        Canonical URL string, or ``""`` if the input cannot be canonicalized.
    """
    if not isinstance(url, str):
        return ""
    url = url.strip()
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except Exception:
        return ""

    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        # Non-HTTP URLs have no meaningful canonical form for our dedup use.
        return ""
    # Upgrade http→https; any caller that genuinely needs the http variant
    # should not be using the canonical form.
    scheme = "https"

    host = (parsed.hostname or "").lower()
    if not host:
        return ""
    if host.startswith("www."):
        host = host[4:]

    port = parsed.port
    if port is not None and port not in _CANONICAL_DEFAULT_PORTS:
        netloc = f"{host}:{port}"
    else:
        netloc = host

    # Path: empty or bare "/" → "/"; strip trailing slash on deeper paths.
    path = parsed.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    # Query: drop tracking params, sort the rest for stable ordering.
    kept: list[tuple[str, str]] = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lower_key = key.lower()
        if lower_key in _TRACKING_PARAMS:
            continue
        if any(lower_key.startswith(p) for p in _TRACKING_PARAM_PREFIXES):
            continue
        kept.append((key, value))
    kept.sort(key=lambda kv: (kv[0], kv[1]))
    query = urlencode(kept, doseq=False) if kept else ""

    # Fragment is intentionally dropped (empty string in position 5).
    return urlunparse((scheme, netloc, path, "", query, ""))


def archive_timestamp_is_round(url: str) -> bool:
    """Return ``True`` if an ``archive.org`` URL has a round midnight timestamp.

    Fabricated archive URLs typically use exact midnight
    (e.g. ``20250101000000``); real Wayback Machine captures almost never
    land on exact midnight.

    Args:
        url: Any URL (non-archive URLs return ``False``).
    """
    if "web.archive.org" not in url or "/web/2" not in url:
        return False
    ts_part = url.split("/web/")[1].split("/")[0]
    return ts_part.endswith("000000") and len(ts_part) == 14


_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_STATUS_CATEGORIES: dict[int, str] = {
    200: "ok",
    403: "forbidden",
    404: "not_found",
    410: "gone",
}


def check_url(url: str, *, timeout: int | None = None) -> tuple[int, str]:
    """Check whether an HTTP URL is reachable and return its status.

    Performs a HEAD request first; falls back to GET on 405 or transport
    error.  Uses a browser-like User-Agent to avoid bot-detection blocks.

    Args:
        url: Fully-qualified HTTP(S) URL to check.
        timeout: Request timeout in seconds.  When ``None``, reads
            ``URL_CHECK_TIMEOUT`` from the environment (default ``8``).

    Returns:
        ``(status_code, category)`` tuple.  *category* is one of
        ``"ok"``, ``"not_found"``, ``"forbidden"``, ``"gone"``,
        ``"timeout"``, ``"error"``, or ``"http_{status_code}"``.
    """
    if timeout is None:
        timeout = int(os.environ.get("URL_CHECK_TIMEOUT", "8"))

    headers = {"User-Agent": _USER_AGENT}

    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            verify=verify_tls(),
            headers=headers,
        ) as client:
            try:
                resp = guarded_head(client, url)
                if resp.status_code == 405:
                    resp = guarded_get(client, url)
            except httpx.HTTPError:
                resp = guarded_get(client, url)

            code = resp.status_code
            category = _STATUS_CATEGORIES.get(code, f"http_{code}")
            return code, category

    except httpx.TimeoutException:
        return 0, "timeout"
    except Exception:
        return 0, "error"


# Cap the retained body — pages can run to megabytes; downstream only needs the head.
_CONTENT_BODY_MAX_BYTES = 512 * 1024  # 512 KB


def fetch_url_content(
    url: str,
    *,
    timeout: int | None = None,
) -> tuple[int, str, str]:
    """Fetch an HTTP URL's body for downstream content analysis.

    HEAD-less by design — always does a GET because we need the body.
    Follows redirects, uses a browser-like User-Agent. Truncates at
    ``_CONTENT_BODY_MAX_BYTES`` to protect downstream token budgets.

    Args:
        url: Fully-qualified HTTP(S) URL to fetch.
        timeout: Request timeout in seconds. When ``None``, reads
            ``URL_CHECK_TIMEOUT`` from the environment (default ``8``).

    Returns:
        ``(status_code, category, body)``. ``category`` mirrors
        :func:`check_url` (``ok``, ``not_found``, ``forbidden``,
        ``gone``, ``timeout``, ``error``, ``http_{code}``). ``body`` is
        the response text (possibly truncated) on success, empty string
        on any failure.
    """
    if not url:
        return 0, "error", ""
    if timeout is None:
        timeout = int(os.environ.get("URL_CHECK_TIMEOUT", "8"))

    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            verify=verify_tls(),
            headers=headers,
        ) as client:
            resp = guarded_get(client, url)
            code = resp.status_code
            category = _STATUS_CATEGORIES.get(code, f"http_{code}")
            if 200 <= code < 300:
                text = resp.text or ""
                if len(text.encode("utf-8", errors="ignore")) > _CONTENT_BODY_MAX_BYTES:
                    text = text.encode("utf-8", errors="ignore")[:_CONTENT_BODY_MAX_BYTES].decode(
                        "utf-8", errors="ignore"
                    )
                return code, category, text
            return code, category, ""
    except httpx.TimeoutException:
        return 0, "timeout", ""
    except Exception:
        return 0, "error", ""


class _MetadataExtractor(HTMLParser):
    """Minimal HTML parser that collects <title>, selected <meta> tags, and
    the first paragraph of <article>/<main>/<body> for quick content sniffing.

    Deliberately simple — no dependency on beautifulsoup or readability. It
    handles well-formed HTML well enough for LLM content-match checks; edge
    cases (malformed markup, JS-rendered pages) degrade gracefully to empty.
    """

    _WANTED_META_NAMES = {
        "description": "description",
        "og:title": "og_title",
        "og:description": "og_description",
        "twitter:title": "twitter_title",
        "twitter:description": "twitter_description",
    }

    def __init__(self) -> None:
        super().__init__()
        self.title_parts: list[str] = []
        self.meta: dict[str, str] = {}
        self.first_paragraph_parts: list[str] = []
        self._in_title = False
        self._capturing_p = False
        self._inside_main_region = False
        self._done_first_paragraph = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        d = {k: v or "" for k, v in attrs}
        if tag == "title":
            self._in_title = True
            return
        if tag == "meta":
            name = (d.get("name") or d.get("property") or "").lower()
            key = self._WANTED_META_NAMES.get(name)
            if key and "content" in d and key not in self.meta:
                self.meta[key] = d["content"]
            return
        if tag in ("article", "main"):
            self._inside_main_region = True
            return
        if tag == "p" and self._inside_main_region and not self._done_first_paragraph:
            self._capturing_p = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag == "p" and self._capturing_p:
            self._capturing_p = False
            # Consider this paragraph done only if it has non-trivial content;
            # otherwise keep looking (e.g., skip caption-only paragraphs).
            text = "".join(self.first_paragraph_parts).strip()
            if len(text) >= 40:
                self._done_first_paragraph = True
            else:
                self.first_paragraph_parts = []
        elif tag in ("article", "main"):
            self._inside_main_region = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        if self._capturing_p:
            self.first_paragraph_parts.append(data)


def extract_article_metadata(html: str) -> dict[str, str]:
    """Pull title, common meta tags, and the first paragraph from HTML.

    Used to get a lightweight snapshot of a page's topic for LLM
    content-match checks — the goal is to answer "is this article about X?"
    not to reconstruct the article's full prose.

    Args:
        html: Raw HTML as returned by :func:`fetch_url_content`. May be
            truncated; the parser handles partial input gracefully.

    Returns:
        Dict with keys ``title``, ``description``, ``og_title``,
        ``og_description``, ``twitter_title``, ``twitter_description``,
        ``first_paragraph``. Missing fields are empty strings rather than
        absent — callers can safely do ``metadata["title"]`` without
        checking membership.
    """
    out: dict[str, str] = {
        "title": "", "description": "", "og_title": "",
        "og_description": "", "twitter_title": "",
        "twitter_description": "", "first_paragraph": "",
    }
    if not html:
        return out
    parser = _MetadataExtractor()
    try:
        parser.feed(html)
    except Exception:
        return out
    out["title"] = re.sub(r"\s+", " ", "".join(parser.title_parts)).strip()
    for k, v in parser.meta.items():
        out[k] = re.sub(r"\s+", " ", v).strip()
    if parser.first_paragraph_parts:
        out["first_paragraph"] = re.sub(
            r"\s+", " ", "".join(parser.first_paragraph_parts)
        ).strip()
    return out


def extract_path_date(url: str) -> datetime.date | None:
    """Return the first ``/YYYY/MM/DD/`` date segment found in a URL path.

    Many news-site URLs encode the publication date in the path
    (``/2025/03/15/section/slug``). This helper extracts and validates
    that date so callers can cross-check it against claimed event dates.

    Args:
        url: Any URL.

    Returns:
        ``datetime.date`` if a valid date segment is found, otherwise ``None``.
        Invalid calendar dates (e.g. ``/2025/13/45/``) return ``None``.
    """
    if not url:
        return None
    path = urlparse(url).path or ""
    m = _PATH_DATE_RE.search(path)
    if not m:
        return None
    try:
        return datetime.date(int(m["year"]), int(m["month"]), int(m["day"]))
    except (ValueError, TypeError):
        return None


_WAYBACK_DEFAULT_TIMEOUT = 30


def wayback_exists_at(
    url: str,
    at: datetime.date | None = None,
    *,
    tolerance_days: int = 14,
    timeout: int | None = None,
) -> tuple[bool, str | None]:
    """Check whether the Wayback Machine has a snapshot of ``url`` near ``at``.

    Queries the public CDX API (no auth required). Used to verify that a
    cited URL existed at the time a consumer claims it was published — a
    strong signal against hallucinated URLs that return 200 today but were
    never captured when the event allegedly occurred.

    Args:
        url: The target URL to check.
        at: Reference date (usually an event or publication date). When
            ``None``, any captured snapshot qualifies.
        tolerance_days: Accept a snapshot within ``± tolerance_days`` of
            ``at``. Ignored when ``at`` is ``None``.
        timeout: Request timeout in seconds. When ``None``, reads
            ``WAYBACK_TIMEOUT`` from the environment (default ``30``). CDX
            queries are routinely slow even for a single-result lookup, so
            the default is larger than ``check_url``'s ``8``.

    Returns:
        ``(exists, snapshot_url)``. ``snapshot_url`` is a fully-qualified
        ``web.archive.org`` URL when a snapshot is found; ``None`` otherwise.
        On API error or timeout returns ``(False, None)`` — callers treat
        "unknown" as "not verified" rather than raising.
    """
    if not url:
        return False, None
    if timeout is None:
        timeout = int(os.environ.get("WAYBACK_TIMEOUT", str(_WAYBACK_DEFAULT_TIMEOUT)))

    params: dict[str, str] = {
        "url": url,
        "output": "json",
        "limit": "1",
        "fastLatest": "true",
    }
    if at is not None:
        start = at - datetime.timedelta(days=tolerance_days)
        end = at + datetime.timedelta(days=tolerance_days)
        params["from"] = start.strftime("%Y%m%d")
        params["to"] = end.strftime("%Y%m%d")

    headers = {"User-Agent": _USER_AGENT}
    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            verify=verify_tls(),
            headers=headers,
        ) as client:
            resp = client.get(
                "https://web.archive.org/cdx/search/cdx", params=params
            )
            if resp.status_code != 200:
                return False, None
            data = _json.loads(resp.text or "[]")
    except httpx.TimeoutException:
        return False, None
    except Exception:
        return False, None

    # CDX JSON: first row is column headers, remaining rows are hits.
    if not isinstance(data, list) or len(data) < 2:
        return False, None
    header = data[0]
    row = data[1]
    if not isinstance(header, list) or not isinstance(row, list):
        return False, None
    try:
        ts_idx = header.index("timestamp")
        orig_idx = header.index("original")
    except ValueError:
        return False, None
    if ts_idx >= len(row) or orig_idx >= len(row):
        return False, None
    snapshot = f"https://web.archive.org/web/{row[ts_idx]}/{row[orig_idx]}"
    return True, snapshot
