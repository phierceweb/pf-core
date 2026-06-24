"""Tests for pf_core.utils.url_safety (SSRF guard)."""

from __future__ import annotations

import pytest

from pf_core.exceptions import InvalidInputError
from pf_core.utils import url_safety
from pf_core.utils.url_safety import assert_public_url, guarded_get


class TestAssertPublicUrl:
    @pytest.mark.parametrize("url", [
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://127.0.0.1/",
        "http://localhost/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://[::1]/",
        "http://0.0.0.0/",
    ])
    def test_blocks_non_public(self, url):
        with pytest.raises(InvalidInputError):
            assert_public_url(url)

    @pytest.mark.parametrize("url", ["http://1.1.1.1/", "https://8.8.8.8/path"])
    def test_allows_public_ip(self, url):
        assert_public_url(url)  # no raise

    @pytest.mark.parametrize("url", [
        "file:///etc/passwd",
        "ftp://example.com/",
        "gopher://x/",
        "//no-scheme",
    ])
    def test_blocks_bad_scheme(self, url):
        with pytest.raises(InvalidInputError):
            assert_public_url(url)

    def test_allow_private_env_opt_out(self, monkeypatch):
        monkeypatch.setenv("URL_FETCH_ALLOW_PRIVATE", "1")
        assert_public_url("http://127.0.0.1/")  # no raise
        # scheme is still enforced even with the opt-out
        with pytest.raises(InvalidInputError):
            assert_public_url("file:///etc/passwd")


class _Resp:
    def __init__(self, status_code, location=None):
        self.status_code = status_code
        self.headers = {"location": location} if location else {}


class _FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[str] = []

    def get(self, url):
        self.calls.append(url)
        return self._responses.pop(0)

    head = get


class TestGuardedRedirects:
    def test_follows_public_redirect(self, monkeypatch):
        monkeypatch.setattr(url_safety, "assert_public_url", lambda *_a, **_k: None)
        client = _FakeClient([_Resp(301, "https://final.example/x"), _Resp(200)])
        resp = guarded_get(client, "https://start.example/")
        assert resp.status_code == 200
        assert client.calls == ["https://start.example/", "https://final.example/x"]

    def test_revalidates_redirect_target(self):
        # start is public; redirect points at loopback → blocked before 2nd fetch
        client = _FakeClient([_Resp(302, "http://127.0.0.1/")])
        with pytest.raises(InvalidInputError):
            guarded_get(client, "http://1.1.1.1/")
        assert client.calls == ["http://1.1.1.1/"]

    def test_max_redirects_bounded(self, monkeypatch):
        monkeypatch.setattr(url_safety, "assert_public_url", lambda *_a, **_k: None)
        client = _FakeClient([_Resp(301, "https://a.example/") for _ in range(10)])
        resp = guarded_get(client, "https://start.example/")
        assert resp.status_code == 301  # stopped at the cap, no infinite loop
