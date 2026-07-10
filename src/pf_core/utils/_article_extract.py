"""Extraction chain for ``article_fetch`` — trafilatura + htmldate + fallbacks.

Private implementation half of :mod:`pf_core.utils.article_fetch`: the
``FetchedArticle`` model and the HTML→(title, body, date) extractors.
The fetch/retry/Wayback flow and the ``articles``-extra gate live in the
public module.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from typing import Any

from pf_core.log import get_logger
from pf_core.utils.url_parse import extract_path_date

logger = get_logger(__name__)

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


# Bumped when the fetch/extract pipeline changes in ways that invalidate
# existing cached results. Consumers reading project-side cache rows
# stamped with a different version should treat them as misses — a code
# bump triggers refetch without a data migration.
FETCHER_VERSION = 3

# Upper bound on the extracted body we keep. Trim saves tokens for
# downstream LLM consumers; raise it project-side if you need full text.
_BODY_MAX_CHARS = 8000


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
        outlet: domain extracted from the original URL (e.g. ``example.com``).
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
