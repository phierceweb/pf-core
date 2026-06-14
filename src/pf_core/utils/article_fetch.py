"""Fetch and extract structured article metadata from a URL.

Given a candidate article URL, fetch the page, extract title + body +
publish date, and return a typed :class:`FetchedArticle`. Falls back to
the Wayback Machine when the live fetch is paywalled / blocked / errored.
Never raises — every failure mode maps to a ``fetch_status`` value.

Designed for tools that want to read what a URL actually says (link
verification, content matching, source archiving) — not for general-
purpose scraping. Only one URL at a time; callers parallelize.

Dependencies: requires ``trafilatura`` and ``htmldate``. Install via the
``articles`` extra::

    pip install 'pf-core[articles]'

Without those, importing the module works but :func:`fetch_article` raises
``ImportError`` with a helpful message on first call.

Usage::

    from pf_core.utils.article_fetch import fetch_article

    art = fetch_article("https://apnews.com/article/abc123")
    if art.fetch_status == "ok":
        print(art.title, art.date_published)
        print(art.body[:500])

Status values:

- ``ok`` — 2xx, body extracted, not paywalled
- ``paywalled`` — 401/403, or short body + CTA markers
- ``not_found`` — 404/410 (final; Wayback not attempted)
- ``blocked`` — other non-2xx (429, 500, etc.)
- ``timeout`` — request timed out after retries
- ``error`` — transport error after retries

Wayback fallback (default on) re-tries paywalled / blocked / errored
URLs against ``web.archive.org`` and returns the captured copy when
present. ``used_wayback=True`` flags the case.

For project-side caching, wrap this with your own DB cache layer — the
:data:`FETCHER_VERSION` constant bumps when extraction logic changes
in a way that invalidates cached results.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
from dataclasses import dataclass, field
from typing import Any

try:
    from tenacity import (
        Retrying,
        retry_if_exception_type,
        stop_after_attempt,
        wait_exponential_jitter,
    )
except ImportError as e:  # pragma: no cover - exercised by bare-install CI
    from pf_core._extras import extra_import_error

    raise extra_import_error("articles", "tenacity", feature="pf_core.utils.article_fetch") from e

from pf_core.log import get_logger
from pf_core.utils.urls import (
    canonical_url,
    domain_of,
    extract_path_date,
    fetch_url_content,
    wayback_exists_at,
)


logger = get_logger(__name__)


__all__ = [
    "FETCHER_VERSION",
    "FetchedArticle",
    "fetch_article",
]


# Lazy import of optional heavy deps. We want `from pf_core.utils.article_fetch
# import fetch_article` to succeed at import time even when the extras aren't
# installed — the error fires at call time with a helpful message.
try:
    import trafilatura  # type: ignore
    import htmldate  # type: ignore
    _HAS_DEPS = True
except ImportError:
    trafilatura = None  # type: ignore
    htmldate = None  # type: ignore
    _HAS_DEPS = False


def _require_deps() -> None:
    if not _HAS_DEPS:
        raise ImportError(
            "pf_core.utils.article_fetch requires the 'articles' extra. "
            "Install with: pip install 'pf-core[articles]'"
        )


# ──────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────

# Bumped when the fetch/extract pipeline changes in ways that invalidate
# existing cached results. Consumers reading project-side cache rows
# stamped with a different version should treat them as misses — a code
# bump triggers refetch without a data migration.
#
# Version history:
#   1 — pre-2026-04-22 (baseline; never used because we start at 2)
#   2 — post precision/recall fallback (2026-04-22)
#   3 — extracted to pf-core (2026-05-03)
FETCHER_VERSION = 3

# Minimum extracted body length (chars) that we trust as "full article."
# Below this, if we also see paywall CTAs, we mark paywalled.
_PAYWALL_MIN_BODY_CHARS = 500

# CTA markers that, combined with short body, suggest a paywall wall-of-text
# tease. Lowercased substring match against extracted body.
_PAYWALL_CTA_MARKERS = (
    "subscribe to continue",
    "subscribe to read",
    "sign in to continue",
    "sign up to continue",
    "continue reading",
    "to read the rest of",
    "already a subscriber",
    "unlock this article",
    "this article is for",
    "for subscribers only",
    "become a member",
    "support our work",
)

# Upper bound on the extracted body we keep. Trim saves tokens for
# downstream LLM consumers; raise it project-side if you need full text.
_BODY_MAX_CHARS = 8000

# Retry config for transient fetch errors.
_RETRY_ATTEMPTS = 3
_RETRY_INITIAL_WAIT = 2.0
_RETRY_MAX_WAIT = 30.0


class _TransientFetchError(Exception):
    """Internal sentinel — raised by retry inner so tenacity retries."""


# ──────────────────────────────────────────────────────────────────────────
# Dataclass
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class FetchedArticle:
    """Result of a fetch + extract attempt for one URL.

    Attributes:
        url: the URL the caller passed (preserved verbatim).
        final_url: what was actually fetched (may differ from ``url``
            when the Wayback fallback fired — then this is the
            ``web.archive.org`` URL).
        fetch_status: one of ``ok`` / ``paywalled`` / ``not_found`` /
            ``blocked`` / ``timeout`` / ``error``.
        used_wayback: True when content came from the Wayback Machine.
        title: extracted article title (empty string if none found).
        date_published: extracted publication date or None.
        body: extracted article body, trimmed to ``_BODY_MAX_CHARS``.
        outlet: domain extracted from the original URL (e.g. ``apnews.com``).
        canonical_url: dedup-key form of the URL (tracking params stripped,
            scheme normalized) via :func:`pf_core.utils.urls.canonical_url`.
        raw_meta: the raw extractor output dict for debugging — author,
            description, language, etc.
    """

    url: str
    final_url: str
    fetch_status: str
    used_wayback: bool
    title: str
    date_published: _dt.date | None
    body: str
    outlet: str
    canonical_url: str
    raw_meta: dict[str, Any] = field(default_factory=dict)


# ──────────────────────────────────────────────────────────────────────────
# Public entry point
# ──────────────────────────────────────────────────────────────────────────


def fetch_article(
    url: str,
    *,
    event_date: _dt.date | None = None,
    wayback_fallback: bool | None = None,
) -> FetchedArticle:
    """Fetch + extract one URL. Never raises.

    Args:
        url: the URL to fetch. Empty/non-string returns an ``error`` stub.
        event_date: when provided, the Wayback lookup will prefer a
            snapshot within ±14 days of this date. Useful when a date
            hint is available and you want a contemporaneous capture.
        wayback_fallback: override the
            ``PF_ARTICLE_WAYBACK_FALLBACK`` env flag. Default
            (``None``) reads the env var; absence defaults to on.

    Returns:
        A :class:`FetchedArticle`. Check ``fetch_status`` to branch.

    Raises:
        ImportError: when the ``articles`` extra is not installed.
            Raised at first call, not at import time.
    """
    _require_deps()

    if not isinstance(url, str) or not url.strip():
        return _empty_result(url or "", fetch_status="error")

    url = url.strip()
    canon = canonical_url(url) or url
    outlet = domain_of(url)

    if wayback_fallback is None:
        wayback_fallback = os.environ.get(
            "PF_ARTICLE_WAYBACK_FALLBACK", "1"
        ).strip() != "0"

    # ── live fetch with retry ──
    fetch_status, body_text = _live_fetch_with_retry(url)

    if fetch_status == "ok" and body_text:
        article = _extract_from_html(
            url=url,
            final_url=url,
            html=body_text,
            used_wayback=False,
            outlet=outlet,
            canon=canon,
        )
        if _looks_paywalled(article.body):
            article.fetch_status = "paywalled"
            fetch_status = "paywalled"
            # Fall through to Wayback attempt.
        else:
            return article

    # ── Wayback fallback for recoverable failures ──
    # Don't waste time on 404/410 — those URLs genuinely don't exist on the
    # live web and Wayback rarely has them either. paywalled / forbidden /
    # timeout / error / blocked are all worth one CDX lookup.
    if wayback_fallback and fetch_status not in ("not_found",):
        wb_result = _try_wayback(url, event_date=event_date)
        if wb_result is not None:
            wb_result.url = url
            wb_result.outlet = outlet
            wb_result.canonical_url = canon
            return wb_result

    return FetchedArticle(
        url=url,
        final_url=url,
        fetch_status=fetch_status,
        used_wayback=False,
        title="",
        date_published=None,
        body="",
        outlet=outlet,
        canonical_url=canon,
        raw_meta={},
    )


# ──────────────────────────────────────────────────────────────────────────
# Live fetch + retry
# ──────────────────────────────────────────────────────────────────────────


def _live_fetch_with_retry(url: str) -> tuple[str, str]:
    """Fetch with tenacity retry on transient shapes.

    Returns ``(fetch_status, body)``. ``body`` is HTML text when status
    is ``ok``; empty string otherwise.
    """

    def _attempt() -> tuple[str, str]:
        code, category, text = fetch_url_content(url)
        if category == "timeout":
            raise _TransientFetchError("timeout")
        if category == "error":
            raise _TransientFetchError("error")
        if 200 <= code < 300:
            if not text:
                raise _TransientFetchError("empty body on 2xx")
            return "ok", text
        if code == 401 or category == "forbidden":
            return "paywalled", ""
        if category in ("not_found", "gone"):
            return "not_found", ""
        return "blocked", ""

    retryer = Retrying(
        stop=stop_after_attempt(_RETRY_ATTEMPTS),
        wait=wait_exponential_jitter(
            initial=_RETRY_INITIAL_WAIT, max=_RETRY_MAX_WAIT,
        ),
        retry=retry_if_exception_type(_TransientFetchError),
        reraise=True,
    )
    try:
        for attempt in retryer:
            with attempt:
                return _attempt()
    except _TransientFetchError as e:
        msg = str(e)
        if msg == "timeout":
            return "timeout", ""
        return "error", ""

    return "error", ""


# ──────────────────────────────────────────────────────────────────────────
# Extract — trafilatura + htmldate + path-date fallback chain
# ──────────────────────────────────────────────────────────────────────────


def _trafi_extract(html: str, url: str, *, favor_recall: bool) -> dict | None:
    """Run trafilatura with precision or recall preference; None on failure."""
    try:
        doc = trafilatura.bare_extraction(
            html, url=url, with_metadata=True,
            include_comments=False, include_tables=False,
            favor_precision=not favor_recall, favor_recall=favor_recall,
            as_dict=True,
        )
    except Exception as e:
        logger.debug("trafilatura_extract_failed", url=url,
                     favor_recall=favor_recall, error=str(e)[:200])
        return None
    return doc if isinstance(doc, dict) else None


def _extract_from_html(
    *,
    url: str,
    final_url: str,
    html: str,
    used_wayback: bool,
    outlet: str,
    canon: str,
) -> FetchedArticle:
    """Run the extractor chain. Body/title/date with graceful fallbacks.

    Chain:
      1. trafilatura precision mode (default — clean bodies, may miss
         non-standard layouts)
      2. trafilatura recall mode fallback (catches pages where
         precision mode returns empty)
      3. htmldate.find_date for publish date (more aggressive than
         trafilatura)
      4. URL path date pattern (``/YYYY/MM/DD/``) as last resort
    """
    meta: dict[str, Any] = {}
    title = ""
    body = ""
    date_published: _dt.date | None = None

    doc = _trafi_extract(html, url, favor_recall=False)
    if not (isinstance(doc, dict) and _first_str(doc.get("text"))):
        recall_doc = _trafi_extract(html, url, favor_recall=True)
        if isinstance(recall_doc, dict) and _first_str(recall_doc.get("text")):
            doc = recall_doc

    if isinstance(doc, dict):
        meta = doc
        title = _first_str(doc.get("title"))
        body = _first_str(doc.get("text"))
        date_raw = _first_str(doc.get("date"))
        date_published = _parse_iso_date(date_raw)

    if date_published is None:
        try:
            date_raw = htmldate.find_date(
                html, url=url, outputformat="%Y-%m-%d",
                extensive_search=True, original_date=True,
            )
        except Exception as e:
            logger.debug("htmldate_failed", url=url, error=str(e)[:200])
            date_raw = None
        date_published = _parse_iso_date(date_raw)

    if date_published is None:
        date_published = extract_path_date(url)

    if body and len(body) > _BODY_MAX_CHARS:
        body = body[:_BODY_MAX_CHARS]

    return FetchedArticle(
        url=url,
        final_url=final_url,
        fetch_status="ok",
        used_wayback=used_wayback,
        title=title.strip(),
        date_published=date_published,
        body=body.strip(),
        outlet=outlet,
        canonical_url=canon,
        raw_meta=meta,
    )


# ──────────────────────────────────────────────────────────────────────────
# Wayback fallback
# ──────────────────────────────────────────────────────────────────────────


def _try_wayback(
    url: str, *, event_date: _dt.date | None,
) -> FetchedArticle | None:
    """Look up the Wayback Machine; if a snapshot exists, fetch + extract it.

    Returns None if no snapshot found or the Wayback fetch itself fails.
    """
    exists, snapshot = wayback_exists_at(url, at=event_date)
    if not exists or not snapshot:
        return None

    status, body = _live_fetch_with_retry(snapshot)
    if status != "ok" or not body:
        logger.debug(
            "wayback_fetch_failed",
            url=url, snapshot=snapshot, status=status,
        )
        return None

    article = _extract_from_html(
        url=url,
        final_url=snapshot,
        html=body,
        used_wayback=True,
        outlet=domain_of(url),
        canon=canonical_url(url) or url,
    )
    # Don't re-classify Wayback content as paywalled — if Wayback
    # captured it, we got SOMETHING; the caller decides if it's actionable.
    return article


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


def _looks_paywalled(body: str) -> bool:
    """Heuristic: short body + CTA marker → treat as paywalled."""
    if not body:
        return False
    if len(body) >= _PAYWALL_MIN_BODY_CHARS:
        return False
    lowered = body.lower()
    return any(marker in lowered for marker in _PAYWALL_CTA_MARKERS)


_ISO_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _parse_iso_date(val: Any) -> _dt.date | None:
    """Parse a trafilatura/htmldate date string into ``date``.

    Both libraries return ``YYYY-MM-DD`` when they find anything, but
    occasionally emit longer ISO strings (``2025-03-15T12:00:00``) or
    timezoned variants. Strip to the first 10 chars' date segment.
    """
    if not val:
        return None
    if isinstance(val, _dt.date):
        return val
    if not isinstance(val, str):
        return None
    m = _ISO_DATE_RE.search(val)
    if not m:
        return None
    try:
        return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _first_str(val: Any) -> str:
    """Coerce a possibly-None / possibly-list value to a string."""
    if val is None:
        return ""
    if isinstance(val, str):
        return val
    if isinstance(val, (list, tuple)):
        for item in val:
            if isinstance(item, str) and item.strip():
                return item
        return ""
    return str(val)


def _empty_result(url: str, *, fetch_status: str) -> FetchedArticle:
    return FetchedArticle(
        url=url,
        final_url=url,
        fetch_status=fetch_status,
        used_wayback=False,
        title="",
        date_published=None,
        body="",
        outlet=domain_of(url),
        canonical_url=canonical_url(url) or url,
        raw_meta={},
    )
