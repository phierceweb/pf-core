"""SSRF guard for outbound HTTP fetches.

The URL-fetch helpers accept caller-influenced URLs; without a guard they will
happily fetch internal targets — `http://169.254.169.254/…` (cloud metadata),
`http://127.0.0.1/…`, private-range hosts — which is a server-side request
forgery vector. This module blocks any URL that resolves to a non-public
address, on the initial request and on every redirect hop.

Verification is on by default. `URL_FETCH_ALLOW_PRIVATE=1` opts out for
consumers that deliberately fetch internal hosts (dev, service mesh); it still
requires an http/https scheme.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

from pf_core.exceptions import InvalidInputError
from pf_core.log import get_logger
from pf_core.utils.env import resolve_bool

_logger = get_logger(__name__)

_ALLOW_PRIVATE_ENV = "URL_FETCH_ALLOW_PRIVATE"
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})


def _allow_private() -> bool:
    return resolve_bool(None, _ALLOW_PRIVATE_ENV, default=False)


def _ip_is_blocked(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def assert_public_url(url: str) -> None:
    """Raise ``InvalidInputError`` if *url* is not a fetchable public http(s) URL.

    Requires an http/https scheme and a host that resolves entirely to public
    addresses. Honors ``URL_FETCH_ALLOW_PRIVATE`` (skips the address check, still
    enforces scheme).
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        _logger.warning("ssrf_blocked", url=url, reason="scheme")
        raise InvalidInputError(f"URL scheme not allowed for fetch: {scheme!r}")
    host = parts.hostname
    if not host:
        _logger.warning("ssrf_blocked", url=url, reason="no_host")
        raise InvalidInputError("URL has no host")
    if _allow_private():
        return
    port = parts.port or (443 if scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as e:
        _logger.warning("ssrf_blocked", url=url, host=host, reason="unresolved")
        raise InvalidInputError(f"could not resolve host: {host}") from e
    for info in infos:
        ip = info[4][0]
        if _ip_is_blocked(ip):
            _logger.warning("ssrf_blocked", url=url, host=host, ip=ip)
            raise InvalidInputError(f"URL resolves to non-public address: {ip}")


def _location(resp: object) -> str | None:
    headers = getattr(resp, "headers", None)
    if headers is None or not hasattr(headers, "get"):
        return None
    return headers.get("location") or headers.get("Location")


def guarded_get(client: object, url: str, *, max_redirects: int = 5) -> object:
    """GET *url* through *client*, validating the target and every redirect hop.

    The client must be built with ``follow_redirects=False`` so this loop sees
    each 3xx and re-validates the ``Location`` before following it.
    """
    return _guarded(client.get, url, max_redirects=max_redirects)  # type: ignore[attr-defined]


def guarded_head(client: object, url: str, *, max_redirects: int = 5) -> object:
    """HEAD *url* through *client*, validating the target and every redirect hop."""
    return _guarded(client.head, url, max_redirects=max_redirects)  # type: ignore[attr-defined]


def _guarded(fetch, url, *, max_redirects):
    assert_public_url(url)
    cur = url
    resp = fetch(cur)
    for _ in range(max_redirects):
        if resp.status_code not in _REDIRECT_CODES:
            return resp
        loc = _location(resp)
        if not loc:
            return resp
        cur = urljoin(cur, loc)
        assert_public_url(cur)
        resp = fetch(cur)
    return resp
