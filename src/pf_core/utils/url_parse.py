"""Pure URL parsing and inspection — no HTTP, no httpx dependency.

Public import path is ``pf_core.utils.urls`` (this module is its
parsing half); importing from here directly also works and needs no
``[http]`` extra.
"""

from __future__ import annotations

import datetime
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

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

    The same resource reached via different link shapes (newsletter
    ``utm_*`` links, bare URLs, social shares) canonicalizes to one string.
    Rules: scheme lowercased and http→https; non-HTTP schemes return "";
    host lowercased, ``www.`` and credentials dropped; default ports
    stripped; fragment dropped; tracking params removed (see
    ``_TRACKING_PARAMS`` / ``_TRACKING_PARAM_PREFIXES``); remaining query
    params sorted by key; path case preserved (RFC 3986), bare/empty path
    → ``/``, trailing slash stripped on non-root paths. Idempotent.

    Returns "" for empty, non-string, or unparseable input — treat an
    empty return as "not a URL."
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
