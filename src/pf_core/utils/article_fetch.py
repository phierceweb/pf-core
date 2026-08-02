"""Fetch and extract structured article metadata from a URL.

Fetch one URL, extract title + body + publish date into a typed
:class:`FetchedArticle`, falling back to the Wayback Machine
(``used_wayback=True``) when the live fetch is paywalled / blocked /
errored. Never raises — every failure maps to a ``fetch_status``:
``ok`` | ``paywalled`` (401/403 or short body + CTA markers) |
``not_found`` (404/410 — final, no Wayback) | ``blocked`` | ``timeout``
| ``error`` | ``unsupported_content_type`` (a 2xx whose body is binary,
e.g. a PDF — final, no Wayback, since the archive replays the same
bytes) | ``no_content`` (a 2xx HTML page the extractor could not pull a
body from — JS-rendered shells, interstitials). :data:`FETCH_STATUSES`
is the machine-readable set. One URL per call; callers parallelize. The
extraction chain lives in ``_article_extract``.

Requires the ``articles`` extra (``pip install 'pf-core[articles]'``);
without it the import works and :func:`fetch_article` raises a helpful
``ImportError`` at first call. For project-side caching, key on
:data:`FETCHER_VERSION` — it bumps when extraction changes invalidate
cached results.

Usage::

    art = fetch_article("https://example.com/news/some-story")
    if art.fetch_status == "ok":
        print(art.title, art.date_published, art.body[:500])
"""

from __future__ import annotations

import datetime as _dt
import os

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
from pf_core.utils._article_extract import (  # noqa: F401 — helpers re-exported
    FETCH_STATUSES,
    FETCHER_VERSION,
    FetchedArticle,
    _extract_from_html,
    _first_str,
    _parse_iso_date,
    looks_binary,
)
from pf_core.utils._article_extract import _HAS_DEPS as _EXTRACT_HAS_DEPS
from pf_core.utils.urls import (
    canonical_url,
    domain_of,
    fetch_url_content,
    wayback_exists_at,
)

logger = get_logger(__name__)


__all__ = [
    "FETCH_STATUSES",
    "FETCHER_VERSION",
    "FetchedArticle",
    "fetch_article",
    "looks_binary",
]


# Patchable module-level mirror of the extraction module's dep probe —
# `_require_deps` reads THIS name, so tests (and callers) can force it.
_HAS_DEPS = _EXTRACT_HAS_DEPS


def _require_deps() -> None:
    if not _HAS_DEPS:
        raise ImportError(
            "pf_core.utils.article_fetch requires the 'articles' extra. "
            "Install with: pip install 'pf-core[articles]'"
        )


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

# Retry config for transient fetch errors.
_RETRY_ATTEMPTS = 3
_RETRY_INITIAL_WAIT = 2.0
_RETRY_MAX_WAIT = 30.0


class _TransientFetchError(Exception):
    """Internal sentinel — raised by retry inner so tenacity retries."""


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

    # Set when extraction ran but the result wasn't returned outright, so the
    # no-Wayback exit can hand back the title/date it recovered.
    live_article: FetchedArticle | None = None

    if fetch_status == "ok" and body_text:
        article = _extract_from_html(
            url=url,
            final_url=url,
            html=body_text,
            used_wayback=False,
            outlet=outlet,
            canon=canon,
        )
        if article.fetch_status == "no_content":
            fetch_status = "no_content"
            live_article = article
            # Fall through to Wayback attempt.
        elif _looks_paywalled(article.body):
            article.fetch_status = "paywalled"
            fetch_status = "paywalled"
            # Fall through to Wayback attempt.
        else:
            return article

    # ── Wayback fallback for recoverable failures ──
    # 404/410 genuinely don't exist on the live web and Wayback rarely has
    # them; unsupported_content_type is replayed byte-for-byte, so a PDF is
    # still a PDF. Everything else is worth one CDX lookup.
    if fetch_status in ("not_found", "unsupported_content_type"):
        wayback_fallback = False
    if wayback_fallback:
        wb_result = _try_wayback(url, event_date=event_date)
        if wb_result is not None:
            wb_result.url = url
            wb_result.outlet = outlet
            wb_result.canonical_url = canon
            return wb_result

    if live_article is not None:
        return live_article

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
            kind = looks_binary(text)
            if kind:
                logger.debug("article_unsupported_content_type",
                             url=url, kind=kind)
                return "unsupported_content_type", ""
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
    if article.fetch_status == "no_content":
        # Returning it would overwrite a more specific live status
        # (paywalled, blocked) with a vaguer one.
        logger.debug("wayback_extract_empty", url=url, snapshot=snapshot)
        return None
    # Don't re-classify Wayback content as paywalled — if Wayback
    # captured it, we got SOMETHING; the caller decides if it's actionable.
    return article


def _looks_paywalled(body: str) -> bool:
    """Heuristic: short body + CTA marker → treat as paywalled."""
    if not body:
        return False
    if len(body) >= _PAYWALL_MIN_BODY_CHARS:
        return False
    lowered = body.lower()
    return any(marker in lowered for marker in _PAYWALL_CTA_MARKERS)


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
