"""Polite HTTP fetch core over stdlib urllib.

GET-only fetching with status-aware retries, manual redirect walking that
re-runs the SSRF guard on every hop, optional client-side throttling, a
response-size cap, and cache-validator (ETag/Last-Modified) support. Zero
third-party deps, so it ships in the base install; raw urllib exceptions
propagate so callers can branch on ``HTTPError.code``.
"""

from __future__ import annotations

import http.client
import ssl
import time
import urllib.error
import urllib.request
from email.message import Message
from typing import Any, TypedDict
from urllib.parse import urljoin

from pf_core.exceptions import ClientError, InvalidInputError
from pf_core.fetch._decode import decode_body
from pf_core.utils.env import resolve_str
from pf_core.utils.http_tls import verify_tls as _resolve_verify_tls
from pf_core.utils.throttle import Throttle
from pf_core.utils.url_safety import _REDIRECT_CODES, assert_public_url

try:
    from pf_core import __version__ as _PF_VERSION
except ImportError:  # pragma: no cover
    _PF_VERSION = "0.0.0+unknown"

__all__ = [
    "Fetcher",
    "Validators",
    "browser_headers",
    "fetch_bytes",
    "fetch_bytes_meta",
    "fetch_text",
    "not_modified",
]

_RETRY_AFTER_CAP = 30.0  # seconds — don't let a server's Retry-After park a crawl for minutes

_UA_ENV_VAR = "PF_FETCH_UA"
_UA_DEFAULT = f"pf-core-fetch/{_PF_VERSION} (+https://github.com/phierceweb/pf-core)"

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


class Validators(TypedDict):
    """The response's cache validators (either may be absent from a server)."""

    etag: str | None
    last_modified: str | None


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse to follow redirects so every 3xx surfaces as an HTTPError."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _build_opener(verify: bool | None = None) -> urllib.request.OpenerDirector:
    ctx = ssl.create_default_context()
    if not _resolve_verify_tls(verify):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return urllib.request.build_opener(
        _NoRedirectHandler, urllib.request.HTTPSHandler(context=ctx)
    )


def _retry_after(exc: urllib.error.HTTPError, attempt: int) -> float:
    """Seconds to wait on a 429 — the server's Retry-After (capped) when sane, else backoff."""
    try:
        return min(float(exc.headers.get("Retry-After", "")), _RETRY_AFTER_CAP)
    except ValueError:
        return 0.5 * (attempt + 1)


class Fetcher:
    """Configured GET client; build one per source/policy and reuse it.

    Args:
        user_agent: UA override; else ``PF_FETCH_UA`` env, else the pf-core default.
        headers: Extra request headers, merged over the defaults (UA, Accept,
            Accept-Language). No Accept-Encoding by default; gzip/deflate
            response bodies are decoded regardless.
        retries: Re-attempts after the first request (``2`` → up to 3 attempts);
            must be ``>= 0``.
        throttle: Optional :class:`~pf_core.utils.throttle.Throttle`, acquired
            before every request — including retries and redirect hops.
        max_bytes: Response-size cap, applied to the wire read *and* to the
            decoded body so a compression bomb can't slip past it. Overrun —
            and a malformed or truncated body — raises :class:`ClientError`.
            ``None`` = unlimited, in both directions.
        require_public: Run the SSRF guard on the URL and every redirect hop.
        max_redirects: Hops to follow before re-raising the last 3xx.
        verify_tls: TLS certificate verification; ``None`` reads
            ``PF_VERIFY_TLS`` (then legacy ``URL_CHECK_VERIFY_TLS``), default
            on. Resolved once here and frozen into this Fetcher's opener.
    """

    def __init__(
        self,
        *,
        user_agent: str | None = None,
        headers: dict[str, str] | None = None,
        retries: int = 2,
        throttle: Throttle | None = None,
        max_bytes: int | None = None,
        require_public: bool = True,
        max_redirects: int = 5,
        verify_tls: bool | None = None,
    ) -> None:
        if retries < 0:
            raise InvalidInputError(f"retries must be >= 0, got {retries}")
        self._user_agent = user_agent
        self._headers = dict(headers or {})
        self._retries = retries
        self._throttle = throttle
        self._max_bytes = max_bytes
        self._require_public = require_public
        self._max_redirects = max_redirects
        self._opener = _build_opener(verify_tls)

    def get_text(
        self, url: str, *, timeout_s: float = 30.0, encoding: str | None = None
    ) -> tuple[str, str]:
        """Return ``(final_url, text)``; decode ``encoding`` > Content-Type charset > utf-8,
        always with replacement, never raising on bad bytes."""
        final_url, raw, headers = self._fetch(url, timeout_s=timeout_s)
        return final_url, raw.decode(encoding or headers.get_content_charset() or "utf-8", "replace")

    def get_bytes(self, url: str, *, timeout_s: float = 180.0) -> tuple[str, bytes]:
        """Return ``(final_url, raw_bytes)`` — longer default timeout for binary downloads."""
        final_url, raw, _headers = self._fetch(url, timeout_s=timeout_s)
        return final_url, raw

    def get_bytes_meta(
        self, url: str, *, timeout_s: float = 180.0
    ) -> tuple[str, bytes, Validators]:
        """``get_bytes`` plus the response's cache validators, for callers that
        persist them (a later :meth:`not_modified` probe skips the re-download)."""
        final_url, raw, headers = self._fetch(url, timeout_s=timeout_s)
        return final_url, raw, {
            "etag": headers.get("ETag"),
            "last_modified": headers.get("Last-Modified"),
        }

    def not_modified(
        self,
        url: str,
        *,
        etag: str | None,
        last_modified: str | None,
        timeout_s: float = 30.0,
    ) -> bool:
        """One conditional GET: ``True`` ONLY on a definitive 304. ``False`` on
        anything else — changed content, no validators to send, or any error —
        so a caller can always fall back to the full fetch path safely. Never
        raises; makes no request when both validators are absent."""
        if not etag and not last_modified:
            return False
        extra: dict[str, str] = {}
        if etag:
            extra["If-None-Match"] = etag
        if last_modified:
            extra["If-Modified-Since"] = last_modified
        try:
            if self._require_public:
                assert_public_url(url)
            if self._throttle is not None:
                self._throttle.acquire()
            self._open(self._request(url, extra), timeout_s).close()
            return False
        except urllib.error.HTTPError as exc:
            return exc.code == 304
        except Exception:
            return False

    def _open(self, request: urllib.request.Request, timeout_s: float) -> Any:
        """The single request seam — every request (initial, hops, retries,
        conditional GETs) goes through here."""
        return self._opener.open(request, timeout=timeout_s)

    def _request(self, url: str, extra: dict[str, str] | None = None) -> urllib.request.Request:
        ua = resolve_str(self._user_agent, _UA_ENV_VAR, default=_UA_DEFAULT) or _UA_DEFAULT
        return urllib.request.Request(
            url,
            headers={
                "User-Agent": ua,
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                **self._headers,
                **(extra or {}),
            },
        )

    def _attempt(self, url: str, timeout_s: float) -> Any:
        """Retry loop for one hop URL. Permanent 4xx (except 408) fail fast with
        zero sleeps; 429 honors Retry-After (capped); 5xx/408/network errors back
        off 0.5*(attempt+1); exhausted retries re-raise the last exception raw."""
        last: Exception | None = None
        for attempt in range(self._retries + 1):
            if self._throttle is not None:
                self._throttle.acquire()
            try:
                return self._open(self._request(url), timeout_s)
            except urllib.error.HTTPError as exc:
                if exc.code in _REDIRECT_CODES:
                    raise  # a hop for _fetch to walk, not a failure
                last = exc
                if exc.code == 429:
                    if attempt < self._retries:
                        time.sleep(_retry_after(exc, attempt))
                elif 400 <= exc.code < 500 and exc.code != 408:
                    raise  # permanent client error — retrying can't help
                elif attempt < self._retries:  # 5xx / 408
                    time.sleep(0.5 * (attempt + 1))
            except Exception as exc:  # URLError, timeout, connection reset, …
                last = exc
                if attempt < self._retries:
                    time.sleep(0.5 * (attempt + 1))
        raise last  # type: ignore[misc]

    def _fetch(self, url: str, *, timeout_s: float) -> tuple[str, bytes, Message]:
        """GET with manual redirect walking; returns ``(final_url, body, headers)``
        where final_url is the URL of the last hop actually fetched."""
        if self._require_public:
            assert_public_url(url)
        cur = url
        hops = 0
        while True:
            try:
                resp = self._attempt(cur, timeout_s)
            except urllib.error.HTTPError as exc:
                if exc.code not in _REDIRECT_CODES or hops >= self._max_redirects:
                    raise
                location = exc.headers.get("Location")
                if not location:
                    raise
                hops += 1
                cur = urljoin(cur, location)  # 303 included — we only ever GET
                if self._require_public:
                    assert_public_url(cur)
                continue
            try:
                body = self._read_body(resp, cur)
            finally:
                resp.close()
            return cur, body, resp.headers

    def _read_body(self, resp: Any, url: str) -> bytes:
        try:
            if self._max_bytes is None:
                data = resp.read()
            else:
                data = resp.read(self._max_bytes + 1)
        except http.client.IncompleteRead as exc:
            raise ClientError(
                "truncated response body",
                context={"url": url, "read": len(exc.partial)},
                cause=exc,
            ) from exc
        if self._max_bytes is not None and len(data) > self._max_bytes:
            raise ClientError(
                "response exceeded max_bytes",
                context={"url": url, "max_bytes": self._max_bytes},
            )
        return decode_body(data, resp.headers, self._max_bytes, url)


def fetch_text(
    url: str, *, timeout_s: float = 30.0, retries: int = 2, encoding: str | None = None
) -> tuple[str, str]:
    """:meth:`Fetcher.get_text` via a default per-call Fetcher."""
    return Fetcher(retries=retries).get_text(url, timeout_s=timeout_s, encoding=encoding)


def fetch_bytes(url: str, *, timeout_s: float = 180.0, retries: int = 2) -> tuple[str, bytes]:
    """:meth:`Fetcher.get_bytes` via a default per-call Fetcher."""
    return Fetcher(retries=retries).get_bytes(url, timeout_s=timeout_s)


def fetch_bytes_meta(
    url: str, *, timeout_s: float = 180.0, retries: int = 2
) -> tuple[str, bytes, Validators]:
    """:meth:`Fetcher.get_bytes_meta` via a default per-call Fetcher."""
    return Fetcher(retries=retries).get_bytes_meta(url, timeout_s=timeout_s)


def not_modified(url: str, *, etag: str | None, last_modified: str | None) -> bool:
    """:meth:`Fetcher.not_modified` via a default per-call Fetcher."""
    return Fetcher().not_modified(url, etag=etag, last_modified=last_modified)


def browser_headers() -> dict[str, str]:
    """Full Chrome-fingerprint header set (a fresh dict per call) for sites whose
    bot detection 403s the lean defaults — pass as ``Fetcher(headers=...)``.

    Advertises ``gzip, deflate`` only, never ``br`` (stdlib can't decode brotli).
    """
    return {
        "User-Agent": _BROWSER_UA,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }
