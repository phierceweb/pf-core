"""Tests for pf_core.fetch — polite stdlib fetch core.

Hermetic: every request goes through the patched ``Fetcher._open`` seam and
DNS resolution is faked at ``socket.getaddrinfo``, so the SSRF guard runs
for real without live lookups.
"""

from __future__ import annotations

import gzip
import socket
import time
import urllib.error
import urllib.request
import zlib
from email.message import Message

import pytest

from pf_core.exceptions import ClientError, InvalidInputError
from pf_core.fetch import (
    Fetcher,
    browser_headers,
    fetch_bytes,
    fetch_bytes_meta,
    fetch_text,
    not_modified,
)

URL = "https://example.com/docs/page"

_PRIVATE_HOSTS = {"internal.example"}


@pytest.fixture(autouse=True)
def _fake_dns(monkeypatch):
    """Resolve every hostname locally: public IP by default, private for _PRIVATE_HOSTS."""

    def fake_getaddrinfo(host, port, *args, **kwargs):
        ip = "10.0.0.5" if host in _PRIVATE_HOSTS else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


@pytest.fixture
def sleeps(monkeypatch):
    """Record retry sleeps instead of sleeping (fetch looks up time.sleep at call time)."""
    recorded: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: recorded.append(seconds))
    return recorded


def _headers(items: dict[str, str] | None = None) -> Message:
    msg = Message()
    for key, value in (items or {}).items():
        msg[key] = value
    return msg


class _Resp:
    """Minimal stand-in for the response objects ``_open`` returns."""

    def __init__(self, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.headers = _headers(headers)
        self._body = body
        self.closed = False

    def read(self, amt: int | None = None) -> bytes:
        if amt is None:
            chunk, self._body = self._body, b""
        else:
            chunk, self._body = self._body[:amt], self._body[amt:]
        return chunk

    def close(self) -> None:
        self.closed = True


def _http_error(code: int, headers: dict[str, str] | None = None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(URL, code, f"status {code}", _headers(headers), None)


def _script_open(monkeypatch, script: list):
    """Patch Fetcher._open to serve ``script`` items in order (exceptions are raised).

    Returns the recorded calls as ``(request, timeout_s)`` tuples.
    """
    calls: list[tuple[urllib.request.Request, float]] = []

    def fake_open(self, request, timeout_s):
        calls.append((request, timeout_s))
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(Fetcher, "_open", fake_open)
    return calls


def _sent_headers(request: urllib.request.Request) -> dict[str, str]:
    """Request headers with lowercased keys (urllib stores them capitalized)."""
    return {key.lower(): value for key, value in request.header_items()}


class TestRetryLoop:
    def test_permanent_4xx_raises_immediately_no_sleeps(self, monkeypatch, sleeps):
        err = _http_error(404)
        calls = _script_open(monkeypatch, [err])
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            Fetcher().get_text(URL)
        assert exc_info.value is err
        assert sleeps == []
        assert len(calls) == 1

    @pytest.mark.parametrize("code", [400, 401, 403, 404, 410, 422])
    def test_4xx_family_fails_fast(self, monkeypatch, sleeps, code):
        calls = _script_open(monkeypatch, [_http_error(code)])
        with pytest.raises(urllib.error.HTTPError):
            Fetcher().get_bytes(URL)
        assert sleeps == []
        assert len(calls) == 1

    def test_429_honors_retry_after(self, monkeypatch, sleeps):
        calls = _script_open(
            monkeypatch, [_http_error(429, {"Retry-After": "3"}), _Resp(b"ok")]
        )
        _, body = Fetcher().get_bytes(URL)
        assert body == b"ok"
        assert sleeps == [3.0]
        assert len(calls) == 2

    def test_429_retry_after_capped_at_30(self, monkeypatch, sleeps):
        _script_open(monkeypatch, [_http_error(429, {"Retry-After": "600"}), _Resp(b"ok")])
        Fetcher().get_bytes(URL)
        assert sleeps == [30.0]

    def test_429_malformed_retry_after_uses_backoff(self, monkeypatch, sleeps):
        _script_open(monkeypatch, [_http_error(429, {"Retry-After": "soon"}), _Resp(b"ok")])
        Fetcher().get_bytes(URL)
        assert sleeps == [0.5]

    def test_429_without_retry_after_uses_backoff(self, monkeypatch, sleeps):
        _script_open(monkeypatch, [_http_error(429), _Resp(b"ok")])
        Fetcher().get_bytes(URL)
        assert sleeps == [0.5]

    def test_429_exhausted_reraises_last_raw(self, monkeypatch, sleeps):
        errors = [_http_error(429), _http_error(429)]
        _script_open(monkeypatch, list(errors))
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            Fetcher(retries=1).get_bytes(URL)
        assert exc_info.value is errors[-1]
        assert sleeps == [0.5]

    def test_5xx_backoff_schedule(self, monkeypatch, sleeps):
        calls = _script_open(monkeypatch, [_http_error(500), _http_error(503), _Resp(b"ok")])
        _, body = Fetcher(retries=2).get_bytes(URL)
        assert body == b"ok"
        assert sleeps == [0.5, 1.0]
        assert len(calls) == 3

    def test_408_is_retried(self, monkeypatch, sleeps):
        _script_open(monkeypatch, [_http_error(408), _Resp(b"ok")])
        Fetcher().get_bytes(URL)
        assert sleeps == [0.5]

    def test_exhausted_5xx_reraises_last_raw_no_final_sleep(self, monkeypatch, sleeps):
        errors = [_http_error(500), _http_error(502), _http_error(503)]
        calls = _script_open(monkeypatch, list(errors))
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            Fetcher(retries=2).get_bytes(URL)
        assert exc_info.value is errors[-1]
        assert sleeps == [0.5, 1.0]
        assert len(calls) == 3

    def test_network_error_retried_then_reraised_raw(self, monkeypatch, sleeps):
        last = urllib.error.URLError("connection reset")
        _script_open(monkeypatch, [urllib.error.URLError("timeout"), last])
        with pytest.raises(urllib.error.URLError) as exc_info:
            Fetcher(retries=1).get_bytes(URL)
        assert exc_info.value is last
        assert sleeps == [0.5]

    def test_retries_zero_single_attempt(self, monkeypatch, sleeps):
        calls = _script_open(monkeypatch, [_http_error(500)])
        with pytest.raises(urllib.error.HTTPError):
            Fetcher(retries=0).get_bytes(URL)
        assert sleeps == []
        assert len(calls) == 1


class TestRedirects:
    def test_final_url_after_chain(self, monkeypatch):
        script = [
            _http_error(301, {"Location": "https://example.com/moved"}),
            _http_error(302, {"location": "assets-chart.png"}),  # relative + lowercase key
            _Resp(b"data"),
        ]
        calls = _script_open(monkeypatch, script)
        final_url, body = Fetcher().get_bytes(URL)
        assert final_url == "https://example.com/assets-chart.png"
        assert body == b"data"
        assert [call[0].full_url for call in calls] == [
            URL,
            "https://example.com/moved",
            "https://example.com/assets-chart.png",
        ]

    def test_303_continues_as_get(self, monkeypatch):
        script = [_http_error(303, {"Location": "https://example.com/result"}), _Resp(b"ok")]
        calls = _script_open(monkeypatch, script)
        final_url, _ = Fetcher().get_bytes(URL)
        assert final_url == "https://example.com/result"
        assert calls[1][0].get_method() == "GET"

    def test_max_redirects_exceeded_raises_last_3xx(self, monkeypatch):
        errors = [
            _http_error(301, {"Location": f"https://example.com/hop{i}"}) for i in range(3)
        ]
        calls = _script_open(monkeypatch, list(errors))
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            Fetcher(max_redirects=2).get_bytes(URL)
        assert exc_info.value is errors[-1]
        assert len(calls) == 3  # initial request + 2 followed hops

    def test_redirect_without_location_raises(self, monkeypatch):
        err = _http_error(301)
        _script_open(monkeypatch, [err])
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            Fetcher().get_bytes(URL)
        assert exc_info.value is err


class TestSsrfGuard:
    def test_initial_private_url_blocked_before_any_request(self, monkeypatch):
        calls = _script_open(monkeypatch, [])
        with pytest.raises(InvalidInputError):
            Fetcher().get_text("https://internal.example/status")
        assert calls == []

    def test_redirect_to_private_host_blocked(self, monkeypatch):
        script = [_http_error(302, {"Location": "https://internal.example/latest"})]
        calls = _script_open(monkeypatch, script)
        with pytest.raises(InvalidInputError):
            Fetcher().get_bytes(URL)
        assert len(calls) == 1  # the private hop is never fetched

    def test_require_public_false_skips_guard(self, monkeypatch):
        calls = _script_open(monkeypatch, [_Resp(b"ok")])
        _, body = Fetcher(require_public=False).get_bytes("https://internal.example/status")
        assert body == b"ok"
        assert len(calls) == 1


class TestTextDecoding:
    def test_encoding_kwarg_wins_over_charset_header(self, monkeypatch):
        body = "café".encode("latin-1")
        _script_open(monkeypatch, [_Resp(body, {"Content-Type": "text/html; charset=utf-8"})])
        _, text = Fetcher().get_text(URL, encoding="latin-1")
        assert text == "café"

    def test_charset_header_used_when_no_kwarg(self, monkeypatch):
        body = "café".encode("latin-1")
        _script_open(monkeypatch, [_Resp(body, {"Content-Type": "text/html; charset=latin-1"})])
        _, text = Fetcher().get_text(URL)
        assert text == "café"

    def test_utf8_default_with_replacement_never_raises(self, monkeypatch):
        _script_open(monkeypatch, [_Resp(b"ok \xff")])
        _, text = Fetcher().get_text(URL)
        assert text == "ok �"


class TestContentEncoding:
    def test_gzip_body_decoded(self, monkeypatch):
        _script_open(
            monkeypatch, [_Resp(gzip.compress(b"payload"), {"Content-Encoding": "gzip"})]
        )
        _, raw = Fetcher().get_bytes(URL)
        assert raw == b"payload"

    def test_deflate_zlib_body_decoded(self, monkeypatch):
        _script_open(
            monkeypatch, [_Resp(zlib.compress(b"payload"), {"Content-Encoding": "deflate"})]
        )
        _, raw = Fetcher().get_bytes(URL)
        assert raw == b"payload"

    def test_raw_deflate_fallback(self, monkeypatch):
        compressor = zlib.compressobj(wbits=-15)
        body = compressor.compress(b"payload") + compressor.flush()
        _script_open(monkeypatch, [_Resp(body, {"Content-Encoding": "deflate"})])
        _, raw = Fetcher().get_bytes(URL)
        assert raw == b"payload"

    def test_no_accept_encoding_in_default_headers(self, monkeypatch):
        calls = _script_open(monkeypatch, [_Resp(b"ok")])
        Fetcher().get_bytes(URL)
        assert "accept-encoding" not in _sent_headers(calls[0][0])


class TestMaxBytes:
    def test_overrun_raises_client_error_with_context(self, monkeypatch):
        _script_open(monkeypatch, [_Resp(b"x" * 11)])
        with pytest.raises(ClientError) as exc_info:
            Fetcher(max_bytes=10).get_bytes(URL)
        assert exc_info.value.context == {"url": URL, "max_bytes": 10}

    def test_body_at_limit_passes(self, monkeypatch):
        _script_open(monkeypatch, [_Resp(b"x" * 10)])
        _, raw = Fetcher(max_bytes=10).get_bytes(URL)
        assert raw == b"x" * 10

    def test_default_is_unlimited(self, monkeypatch):
        _script_open(monkeypatch, [_Resp(b"x" * 4096)])
        _, raw = Fetcher().get_bytes(URL)
        assert len(raw) == 4096


class TestValidators:
    def test_get_bytes_meta_returns_validators(self, monkeypatch):
        stamp = "Mon, 01 Jan 2024 00:00:00 GMT"
        _script_open(monkeypatch, [_Resp(b"data", {"ETag": '"abc"', "Last-Modified": stamp})])
        final_url, raw, validators = Fetcher().get_bytes_meta(URL)
        assert final_url == URL
        assert raw == b"data"
        assert validators == {"etag": '"abc"', "last_modified": stamp}

    def test_get_bytes_meta_absent_validators_are_none(self, monkeypatch):
        _script_open(monkeypatch, [_Resp(b"data")])
        _, _, validators = Fetcher().get_bytes_meta(URL)
        assert validators == {"etag": None, "last_modified": None}


class TestNotModified:
    def test_definitive_304_true_and_sends_etag(self, monkeypatch):
        calls = _script_open(monkeypatch, [_http_error(304)])
        assert Fetcher().not_modified(URL, etag='"abc"', last_modified=None) is True
        assert _sent_headers(calls[0][0])["if-none-match"] == '"abc"'

    def test_sends_if_modified_since(self, monkeypatch):
        calls = _script_open(monkeypatch, [_http_error(304)])
        stamp = "Mon, 01 Jan 2024 00:00:00 GMT"
        assert Fetcher().not_modified(URL, etag=None, last_modified=stamp) is True
        assert _sent_headers(calls[0][0])["if-modified-since"] == stamp

    def test_200_false(self, monkeypatch):
        _script_open(monkeypatch, [_Resp(b"changed")])
        assert Fetcher().not_modified(URL, etag='"abc"', last_modified=None) is False

    @pytest.mark.parametrize(
        "item", [urllib.error.URLError("boom"), _http_error(500), _http_error(404)]
    )
    def test_error_false_never_raises(self, monkeypatch, item):
        _script_open(monkeypatch, [item])
        assert Fetcher().not_modified(URL, etag='"abc"', last_modified=None) is False

    def test_no_validators_no_request(self, monkeypatch):
        calls = _script_open(monkeypatch, [])
        assert Fetcher().not_modified(URL, etag=None, last_modified=None) is False
        assert calls == []


class _CountingThrottle:
    def __init__(self) -> None:
        self.acquired = 0

    def acquire(self) -> float:
        self.acquired += 1
        return 0.0


class TestThrottle:
    def test_acquired_before_every_request_including_retries_and_hops(
        self, monkeypatch, sleeps
    ):
        throttle = _CountingThrottle()
        script = [
            _http_error(500),
            _http_error(301, {"Location": "https://example.com/moved"}),
            _Resp(b"ok"),
        ]
        calls = _script_open(monkeypatch, script)
        Fetcher(throttle=throttle, retries=1).get_bytes(URL)
        assert throttle.acquired == 3
        assert len(calls) == 3

    def test_not_modified_acquires(self, monkeypatch):
        throttle = _CountingThrottle()
        _script_open(monkeypatch, [_http_error(304)])
        Fetcher(throttle=throttle).not_modified(URL, etag='"x"', last_modified=None)
        assert throttle.acquired == 1


class TestUserAgent:
    def test_kwarg_beats_env(self, monkeypatch):
        monkeypatch.setenv("PF_FETCH_UA", "env-agent/2.0")
        calls = _script_open(monkeypatch, [_Resp(b"ok")])
        Fetcher(user_agent="kwarg-agent/1.0").get_bytes(URL)
        assert _sent_headers(calls[0][0])["user-agent"] == "kwarg-agent/1.0"

    def test_env_beats_default(self, monkeypatch):
        monkeypatch.setenv("PF_FETCH_UA", "env-agent/2.0")
        calls = _script_open(monkeypatch, [_Resp(b"ok")])
        Fetcher().get_bytes(URL)
        assert _sent_headers(calls[0][0])["user-agent"] == "env-agent/2.0"

    def test_default_identifies_pf_core(self, monkeypatch):
        monkeypatch.delenv("PF_FETCH_UA", raising=False)
        calls = _script_open(monkeypatch, [_Resp(b"ok")])
        Fetcher().get_bytes(URL)
        user_agent = _sent_headers(calls[0][0])["user-agent"]
        assert user_agent.startswith("pf-core-fetch/")
        assert "+https://github.com/phierceweb/pf-core" in user_agent


class TestHeaders:
    def test_defaults_sent(self, monkeypatch):
        calls = _script_open(monkeypatch, [_Resp(b"ok")])
        Fetcher().get_bytes(URL)
        sent = _sent_headers(calls[0][0])
        assert sent["accept"] == "*/*"
        assert sent["accept-language"] == "en-US,en;q=0.9"

    def test_headers_kwarg_merges_over_defaults(self, monkeypatch):
        calls = _script_open(monkeypatch, [_Resp(b"ok")])
        Fetcher(headers={"Accept": "text/html", "X-Custom": "1"}).get_bytes(URL)
        sent = _sent_headers(calls[0][0])
        assert sent["accept"] == "text/html"
        assert sent["x-custom"] == "1"
        assert "user-agent" in sent

    def test_default_timeouts_passed_to_open(self, monkeypatch):
        calls = _script_open(monkeypatch, [_Resp(b"a"), _Resp(b"b")])
        Fetcher().get_text(URL)
        Fetcher().get_bytes(URL)
        assert calls[0][1] == 30.0
        assert calls[1][1] == 180.0


class TestBrowserHeaders:
    def test_fresh_dict_each_call(self):
        first = browser_headers()
        first["X-Mutated"] = "1"
        assert "X-Mutated" not in browser_headers()

    def test_fingerprint_keys(self):
        headers = browser_headers()
        for key in ("Sec-Fetch-Dest", "Sec-Fetch-Mode", "Sec-Fetch-Site", "Sec-Fetch-User"):
            assert key in headers
        assert headers["Upgrade-Insecure-Requests"] == "1"
        assert "Chrome/120.0.0.0" in headers["User-Agent"]

    def test_accept_encoding_gzip_deflate_never_br(self):
        assert browser_headers()["Accept-Encoding"] == "gzip, deflate"


class TestModuleFunctions:
    def test_fetch_text(self, monkeypatch):
        _script_open(monkeypatch, [_Resp(b"body")])
        assert fetch_text(URL) == (URL, "body")

    def test_fetch_bytes_passes_retries(self, monkeypatch, sleeps):
        calls = _script_open(monkeypatch, [_http_error(500)])
        with pytest.raises(urllib.error.HTTPError):
            fetch_bytes(URL, retries=0)
        assert sleeps == []
        assert len(calls) == 1

    def test_fetch_bytes_meta(self, monkeypatch):
        _script_open(monkeypatch, [_Resp(b"d", {"ETag": '"e"'})])
        _, raw, validators = fetch_bytes_meta(URL)
        assert raw == b"d"
        assert validators["etag"] == '"e"'

    def test_not_modified(self, monkeypatch):
        _script_open(monkeypatch, [_http_error(304)])
        assert not_modified(URL, etag='"e"', last_modified=None) is True
