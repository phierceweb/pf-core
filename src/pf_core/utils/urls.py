"""URL reachability and content fetching (httpx), plus the public facade
for the URL utility family.

General-purpose URL helpers used across the framework — not specific to
LLM content validation. Pure parsing lives in ``url_parse``; HTML
metadata extraction in ``url_html``; both re-exported here so the
public import path stays one module::

    from pf_core.utils.urls import domain_of, archive_timestamp_is_round, check_url

    domain_of("https://www.example.com/page")  # "example.com"
    status, category = check_url("https://example.com")  # (200, "ok")
"""

from __future__ import annotations

import datetime
import json as _json
import os

try:
    import httpx
except ImportError as e:  # pragma: no cover - exercised by bare-install CI
    from pf_core._extras import extra_import_error

    raise extra_import_error("http", "httpx", feature="pf_core.utils.urls") from e

from pf_core.utils.http_tls import verify_tls
from pf_core.utils.url_html import extract_article_metadata  # noqa: F401 — re-export
from pf_core.utils.url_parse import (
    archive_timestamp_is_round,
    canonical_url,
    domain_of,
    extract_path_date,
)
from pf_core.utils.url_safety import guarded_get, guarded_head

__all__ = [
    "archive_timestamp_is_round",
    "canonical_url",
    "check_url",
    "domain_of",
    "extract_article_metadata",
    "extract_path_date",
    "fetch_url_content",
    "wayback_exists_at",
]

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

    Always GETs (we need the body), browser-like User-Agent, truncates at
    ``_CONTENT_BODY_MAX_BYTES`` to protect downstream token budgets.
    ``timeout=None`` reads ``URL_CHECK_TIMEOUT`` (default 8).

    Returns:
        ``(status_code, category, body)`` — ``category`` mirrors
        :func:`check_url`; ``body`` is the (possibly truncated) text on
        success, ``""`` on any failure.
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


_WAYBACK_DEFAULT_TIMEOUT = 30


def wayback_exists_at(
    url: str,
    at: datetime.date | None = None,
    *,
    tolerance_days: int = 14,
    timeout: int | None = None,
) -> tuple[bool, str | None]:
    """Check whether the Wayback Machine has a snapshot of ``url`` near ``at``.

    Public CDX API, no auth. Verifies a cited URL existed when it was
    allegedly published — a strong signal against hallucinated URLs that
    return 200 today but were never captured. ``at=None`` accepts any
    snapshot; otherwise a capture within ``± tolerance_days`` qualifies.
    ``timeout=None`` reads ``WAYBACK_TIMEOUT`` (default 30 — CDX is slow).

    Returns:
        ``(exists, snapshot_url)``; ``(False, None)`` on miss, API error,
        or timeout — callers treat "unknown" as "not verified."
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
