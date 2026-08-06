"""Tests for pf_core.utils.url_safety (SSRF guard)."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

import pf_core
from pf_core.exceptions import InvalidInputError
from pf_core.utils import url_safety
from pf_core.utils.url_safety import assert_public_url, guarded_get

_URLS_DOC = Path(pf_core.__file__).resolve().parent / "docs" / "urls.md"


class TestAssertPublicUrl:
    @pytest.mark.parametrize("url", [
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://127.0.0.1/",
        "http://localhost/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://[::1]/",
        "http://0.0.0.0/",
        "http://100.64.0.1/",           # RFC 6598 shared address space, low edge
        "http://100.100.100.200/",      # mid-range — EKS/GKE pod addresses live here
        "http://100.127.255.254/",      # high edge
    ])
    def test_blocks_non_public(self, url):
        with pytest.raises(InvalidInputError):
            assert_public_url(url)

    @pytest.mark.parametrize("url", [
        "http://1.1.1.1/",
        "https://8.8.8.8/path",
        "http://100.63.255.255/",       # last address below the shared-space block
        "http://100.128.0.1/",          # first address above it
    ])
    def test_allows_public_ip(self, url):
        assert_public_url(url)  # no raise

    @pytest.mark.parametrize("url", [
        "http://[64:ff9b::7f00:1]/",    # NAT64 of 127.0.0.1  — is_reserved only
        "http://[64:ff9b::a00:1]/",     # NAT64 of 10.0.0.1   — is_reserved only
        "http://[5f00::1]/",            # SRv6 5f00::/16      — is_reserved only
        "http://224.0.0.1/",            # IPv4 multicast      — is_multicast only
        "http://[ff02::1]/",            # IPv6 multicast      — is_multicast only
    ])
    def test_blocks_addresses_only_the_enumerated_flags_catch(self, url):
        """These all report is_global=True. Rewriting _ip_is_blocked as a bare
        ``not addr.is_global`` would unblock every one of them."""
        with pytest.raises(InvalidInputError):
            assert_public_url(url)

    def test_blocks_ipv4_mapped_shared_space(self):
        assert url_safety._ip_is_blocked("::ffff:100.64.0.1") is True
        assert url_safety._ip_is_blocked("::ffff:8.8.8.8") is False

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

    def test_fails_closed_on_unresolvable_host(self, monkeypatch):
        """A host that won't resolve is blocked, not waved through."""
        import socket

        def boom(*args, **kwargs):
            raise socket.gaierror("Name or service not known")

        monkeypatch.setattr(socket, "getaddrinfo", boom)
        with pytest.raises(InvalidInputError):
            assert_public_url("http://nonexistent.invalid/figure.png")


class TestVettedAddressesAreReturned:
    """The guard resolves the host; callers need those addresses to pin to."""

    def test_returns_resolved_address(self):
        assert assert_public_url("http://1.1.1.1/") == ("1.1.1.1",)

    def test_returns_every_address_deduped_in_order(self, monkeypatch):
        infos = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:2800:220:1::1", 80, 0, 0)),
        ]
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: infos)
        assert assert_public_url("http://example.com/") == (
            "93.184.216.34",
            "2606:2800:220:1::1",
        )

    def test_allow_private_returns_no_vetted_addresses(self, monkeypatch):
        monkeypatch.setenv("URL_FETCH_ALLOW_PRIVATE", "1")
        assert assert_public_url("http://127.0.0.1/") == ()


class TestRebindingIsDocumented:
    """The guard does not survive a DNS rebind; nothing may claim otherwise."""

    def test_module_docstring_states_the_gap(self):
        doc = (url_safety.__doc__ or "").lower()
        assert "rebinding" in doc
        assert "toctou" in doc

    def test_urls_doc_states_the_gap(self):
        assert "rebinding" in _URLS_DOC.read_text().lower()


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
