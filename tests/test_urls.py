"""Tests for pf_core.utils.urls."""

from __future__ import annotations

import datetime

import httpx
import pytest

from pf_core.exceptions import InvalidInputError
from pf_core.utils.urls import (
    archive_timestamp_is_round,
    canonical_url,
    check_url,
    domain_of,
    extract_article_metadata,
    extract_path_date,
    fetch_url_content,
    wayback_exists_at,
)


@pytest.fixture(autouse=True)
def _bypass_ssrf_guard(monkeypatch):
    """Neutralize the SSRF guard for status-mapping tests (avoids real DNS).

    The guard itself is covered in test_url_safety.py; wiring is covered by the
    explicit ``*_blocks_ssrf`` tests below, which re-patch it to raise.
    """
    monkeypatch.setattr(
        "pf_core.utils.url_safety.assert_public_url", lambda *_a, **_k: None
    )


# ---------------------------------------------------------------------------
# Helpers for mocking httpx.Client
# ---------------------------------------------------------------------------

class MockResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class MockClient:
    """Configurable stand-in for ``httpx.Client``."""

    def __init__(
        self,
        head_response: MockResponse | None = None,
        get_response: MockResponse | None = None,
        head_error: Exception | None = None,
    ):
        self.head_response = head_response
        self.get_response = get_response
        self.head_error = head_error
        self.init_kwargs: dict = {}

    def head(self, url: str) -> MockResponse:
        if self.head_error:
            raise self.head_error
        assert self.head_response is not None
        return self.head_response

    def get(self, url: str) -> MockResponse:
        assert self.get_response is not None
        return self.get_response

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class TestDomainOf:
    def test_simple_url(self):
        assert domain_of("https://example.com/page") == "example.com"

    def test_strips_www(self):
        assert domain_of("https://www.example.com/page") == "example.com"

    def test_preserves_subdomain(self):
        assert domain_of("https://blog.example.com/page") == "blog.example.com"

    def test_strips_www_with_subdomain(self):
        assert domain_of("https://www.blog.example.com") == "blog.example.com"

    def test_lowercase(self):
        assert domain_of("https://WWW.EXAMPLE.COM") == "example.com"

    def test_empty_string(self):
        assert domain_of("") == ""

    def test_no_scheme(self):
        # urlparse without scheme puts everything in path
        assert domain_of("example.com") == ""

    def test_with_port(self):
        assert domain_of("https://example.com:8080/page") == "example.com"

    def test_with_path_and_query(self):
        assert domain_of("https://example.com/path?q=1&r=2") == "example.com"


class TestCanonicalUrl:
    # -- input edge cases -------------------------------------------------

    def test_empty_string(self):
        assert canonical_url("") == ""

    def test_whitespace_only(self):
        assert canonical_url("   ") == ""

    def test_non_string_returns_empty(self):
        assert canonical_url(None) == ""  # type: ignore[arg-type]
        assert canonical_url(123) == ""   # type: ignore[arg-type]

    def test_no_scheme_returns_empty(self):
        # Without scheme, urlparse puts everything in path — no canonical form.
        assert canonical_url("example.com/story") == ""

    def test_mailto_returns_empty(self):
        assert canonical_url("mailto:user@example.com") == ""

    def test_file_returns_empty(self):
        assert canonical_url("file:///tmp/x") == ""

    def test_strips_surrounding_whitespace(self):
        assert canonical_url("  https://example.com/  ") == "https://example.com/"

    # -- scheme normalization --------------------------------------------

    def test_http_upgraded_to_https(self):
        assert canonical_url("http://example.com/x") == "https://example.com/x"

    def test_https_preserved(self):
        assert canonical_url("https://example.com/x") == "https://example.com/x"

    def test_uppercase_scheme_lowered(self):
        assert canonical_url("HTTPS://example.com/x") == "https://example.com/x"

    # -- host normalization ----------------------------------------------

    def test_uppercase_host_lowered(self):
        assert canonical_url("https://EXAMPLE.COM/X") == "https://example.com/X"

    def test_www_stripped(self):
        assert canonical_url("https://www.example.com/x") == "https://example.com/x"

    def test_subdomain_preserved(self):
        assert canonical_url("https://blog.example.com/x") == "https://blog.example.com/x"

    def test_www_stripped_only_at_leftmost_label(self):
        # "www." embedded later (as part of a host name) is left alone.
        assert canonical_url("https://cdn.www.example.com/x") == "https://cdn.www.example.com/x"

    def test_credentials_stripped(self):
        assert canonical_url("https://user:pass@example.com/x") == "https://example.com/x"

    # -- port normalization ----------------------------------------------

    def test_default_https_port_stripped(self):
        assert canonical_url("https://example.com:443/x") == "https://example.com/x"

    def test_default_http_port_stripped_after_upgrade(self):
        # http://example.com:80/x upgrades to https and drops the port.
        assert canonical_url("http://example.com:80/x") == "https://example.com/x"

    def test_custom_port_preserved(self):
        assert canonical_url("https://example.com:8080/x") == "https://example.com:8080/x"

    # -- path normalization ----------------------------------------------

    def test_empty_path_becomes_root_slash(self):
        assert canonical_url("https://example.com") == "https://example.com/"

    def test_bare_root_slash_preserved(self):
        assert canonical_url("https://example.com/") == "https://example.com/"

    def test_trailing_slash_stripped(self):
        assert canonical_url("https://example.com/foo/") == "https://example.com/foo"

    def test_trailing_slash_stripped_deep(self):
        assert canonical_url("https://example.com/a/b/c/") == "https://example.com/a/b/c"

    def test_path_case_preserved(self):
        # RFC 3986 says path is case-sensitive; don't lowercase.
        assert canonical_url("https://example.com/Foo/Bar") == "https://example.com/Foo/Bar"

    # -- fragment --------------------------------------------------------

    def test_fragment_dropped(self):
        assert canonical_url("https://example.com/x#section-2") == "https://example.com/x"

    def test_fragment_with_query_dropped(self):
        assert canonical_url("https://example.com/x?a=1#frag") == "https://example.com/x?a=1"

    # -- tracking-param stripping ----------------------------------------

    def test_utm_params_stripped(self):
        assert canonical_url(
            "https://example.com/x?utm_source=newsletter&utm_medium=email&utm_campaign=newsletter"
        ) == "https://example.com/x"

    def test_fbclid_stripped(self):
        assert canonical_url("https://example.com/x?fbclid=abc123") == "https://example.com/x"

    def test_gclid_stripped(self):
        assert canonical_url("https://example.com/x?gclid=xyz") == "https://example.com/x"

    def test_mailchimp_params_stripped(self):
        assert canonical_url(
            "https://example.com/x?mc_cid=aaa&mc_eid=bbb"
        ) == "https://example.com/x"

    def test_hubspot_prefixed_params_stripped(self):
        assert canonical_url(
            "https://example.com/x?__hsfp=1&__hssc=2&__hstc=3"
        ) == "https://example.com/x"

    def test_twitter_impression_stripped(self):
        assert canonical_url(
            "https://example.com/x?__twitter_impression=true"
        ) == "https://example.com/x"

    def test_real_params_preserved(self):
        # id / p / page / query are genuine routing params on many CMSes.
        # Don't strip them.
        assert canonical_url(
            "https://example.com/article?id=12345&page=2"
        ) == "https://example.com/article?id=12345&page=2"

    def test_mixed_tracking_and_real_params(self):
        assert canonical_url(
            "https://example.com/article?id=12345&utm_source=twitter&page=2&fbclid=abc"
        ) == "https://example.com/article?id=12345&page=2"

    # -- query param ordering --------------------------------------------

    def test_query_params_sorted(self):
        assert canonical_url("https://example.com/x?b=2&a=1") == \
               canonical_url("https://example.com/x?a=1&b=2")

    def test_query_params_sorted_canonical_form(self):
        # Canonical form has params sorted alphabetically by key.
        assert canonical_url(
            "https://example.com/x?z=3&a=1&m=2"
        ) == "https://example.com/x?a=1&m=2&z=3"

    def test_empty_query_value_preserved(self):
        assert canonical_url("https://example.com/x?flag=") == "https://example.com/x?flag="

    # -- idempotence -----------------------------------------------------

    def test_idempotent_simple(self):
        x = canonical_url("https://www.example.com/x?utm_source=y#frag")
        assert canonical_url(x) == x

    def test_idempotent_complex(self):
        x = canonical_url(
            "HTTP://User:Pass@WWW.Example.com:80/Path/?z=3&utm_medium=email&a=1#section"
        )
        assert canonical_url(x) == x
        assert x == "https://example.com/Path?a=1&z=3"

    # -- integration-ish -------------------------------------------------

    def test_newsletter_style_url_canonicalizes(self):
        # A real-world shape of how newsletter links arrive: a URL with newsletter
        # utm markers and sometimes a fragment.
        raw = (
            "https://example.com/article/quarterly-report-published-"
            "a1b2c3d4?utm_source=newsletter&utm_medium=email"
            "&utm_campaign=newsletter#read-more"
        )
        assert canonical_url(raw) == (
            "https://example.com/article/"
            "quarterly-report-published-a1b2c3d4"
        )

    def test_cross_tracking_variants_match(self):
        # Same article, three different share paths.
        newsletter = "https://example.com/article/foo?utm_source=newsletter"
        twitter = "https://example.com/article/foo?s=20&fbclid=abc"
        bare = "https://www.example.com/article/foo/"
        canon_newsletter = canonical_url(newsletter)
        canon_twitter = canonical_url(twitter)
        canon_bare = canonical_url(bare)
        # The Twitter variant keeps ?s=20 (we don't strip generic `s`), so
        # newsletter and bare match; twitter differs by `s=20` only.
        assert canon_newsletter == canon_bare == "https://example.com/article/foo"
        assert canon_twitter == "https://example.com/article/foo?s=20"


class TestArchiveTimestampIsRound:
    def test_round_midnight(self):
        url = "https://web.archive.org/web/20250101000000/https://example.com"
        assert archive_timestamp_is_round(url) is True

    def test_non_round(self):
        url = "https://web.archive.org/web/20250115143527/https://example.com"
        assert archive_timestamp_is_round(url) is False

    def test_non_archive_url(self):
        assert archive_timestamp_is_round("https://example.com") is False

    def test_short_timestamp(self):
        url = "https://web.archive.org/web/2025/https://example.com"
        assert archive_timestamp_is_round(url) is False

    def test_partial_round(self):
        # Ends in 000000 but not 14 digits
        url = "https://web.archive.org/web/20250101000000123/https://example.com"
        assert archive_timestamp_is_round(url) is False


class TestCheckUrl:
    """Tests for :func:`check_url`."""

    def _patch_client(self, monkeypatch, mock: MockClient):
        """Replace ``httpx.Client`` so it returns *mock*."""
        captured: dict = {}

        def factory(**kwargs):
            captured.update(kwargs)
            mock.init_kwargs = kwargs
            return mock

        monkeypatch.setattr(httpx, "Client", factory)
        return captured

    # -- status category mapping ------------------------------------------

    def test_ok_response(self, monkeypatch):
        mock = MockClient(head_response=MockResponse(200))
        self._patch_client(monkeypatch, mock)
        assert check_url("https://example.com") == (200, "ok")

    def test_not_found(self, monkeypatch):
        mock = MockClient(head_response=MockResponse(404))
        self._patch_client(monkeypatch, mock)
        assert check_url("https://example.com") == (404, "not_found")

    def test_forbidden(self, monkeypatch):
        mock = MockClient(head_response=MockResponse(403))
        self._patch_client(monkeypatch, mock)
        assert check_url("https://example.com") == (403, "forbidden")

    def test_gone(self, monkeypatch):
        mock = MockClient(head_response=MockResponse(410))
        self._patch_client(monkeypatch, mock)
        assert check_url("https://example.com") == (410, "gone")

    def test_other_status(self, monkeypatch):
        mock = MockClient(head_response=MockResponse(301))
        self._patch_client(monkeypatch, mock)
        assert check_url("https://example.com") == (301, "http_301")

    # -- HEAD → GET fallback ----------------------------------------------

    def test_head_405_falls_back_to_get(self, monkeypatch):
        mock = MockClient(
            head_response=MockResponse(405),
            get_response=MockResponse(200),
        )
        self._patch_client(monkeypatch, mock)
        assert check_url("https://example.com") == (200, "ok")

    def test_head_error_falls_back_to_get(self, monkeypatch):
        mock = MockClient(
            head_error=httpx.HTTPError("connection reset"),
            get_response=MockResponse(200),
        )
        self._patch_client(monkeypatch, mock)
        assert check_url("https://example.com") == (200, "ok")

    # -- error handling ---------------------------------------------------

    def test_timeout(self, monkeypatch):
        mock = MockClient(head_error=httpx.TimeoutException("timed out"))
        # The TimeoutException must escape the Client context manager,
        # so we need the GET fallback to also raise.
        mock.get_response = None

        def raise_timeout(**kwargs):
            return _TimeoutClient()

        monkeypatch.setattr(httpx, "Client", raise_timeout)
        assert check_url("https://example.com") == (0, "timeout")

    def test_network_error(self, monkeypatch):
        def raise_error(**kwargs):
            return _ErrorClient()

        monkeypatch.setattr(httpx, "Client", raise_error)
        assert check_url("https://example.com") == (0, "error")

    # -- timeout parameter ------------------------------------------------

    def test_custom_timeout(self, monkeypatch):
        mock = MockClient(head_response=MockResponse(200))
        captured = self._patch_client(monkeypatch, mock)
        check_url("https://example.com", timeout=15)
        assert captured["timeout"] == 15

    def test_default_timeout_from_env(self, monkeypatch):
        monkeypatch.setenv("URL_CHECK_TIMEOUT", "12")
        mock = MockClient(head_response=MockResponse(200))
        captured = self._patch_client(monkeypatch, mock)
        check_url("https://example.com")
        assert captured["timeout"] == 12

    # -- TLS verification -------------------------------------------------

    def test_verifies_tls_by_default(self, monkeypatch):
        monkeypatch.delenv("URL_CHECK_VERIFY_TLS", raising=False)
        mock = MockClient(head_response=MockResponse(200))
        captured = self._patch_client(monkeypatch, mock)
        check_url("https://example.com")
        assert captured["verify"] is True

    def test_verify_tls_disabled_via_env(self, monkeypatch):
        monkeypatch.setenv("URL_CHECK_VERIFY_TLS", "0")
        mock = MockClient(head_response=MockResponse(200))
        captured = self._patch_client(monkeypatch, mock)
        check_url("https://example.com")
        assert captured["verify"] is False

    # -- SSRF guard wiring ------------------------------------------------

    def test_blocked_url_returns_error(self, monkeypatch):
        def boom(*_a, **_k):
            raise InvalidInputError("blocked")

        monkeypatch.setattr("pf_core.utils.url_safety.assert_public_url", boom)
        assert check_url("http://169.254.169.254/latest/meta-data/") == (0, "error")


# -- helper clients that always raise ------------------------------------

class _TimeoutClient:
    """Client whose HEAD and GET both raise ``TimeoutException``."""

    def head(self, url: str):
        raise httpx.TimeoutException("timed out")

    def get(self, url: str):
        raise httpx.TimeoutException("timed out")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class _ErrorClient:
    """Client whose HEAD and GET both raise a generic ``Exception``."""

    def head(self, url: str):
        raise Exception("DNS failure")

    def get(self, url: str):
        raise Exception("DNS failure")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


# ---------------------------------------------------------------------------
# extract_path_date
# ---------------------------------------------------------------------------

class TestExtractPathDate:
    def test_dated_path_with_trailing_segments(self):
        assert extract_path_date(
            "https://www.example.com/2025/03/15/us/news/story.html"
        ) == datetime.date(2025, 3, 15)

    def test_dated_path_leading_section(self):
        assert extract_path_date(
            "https://www.example.org/news/2024/12/01/year-in-review/"
        ) == datetime.date(2024, 12, 1)

    def test_single_digit_month_and_day(self):
        assert extract_path_date(
            "https://example.com/2025/3/5/story"
        ) == datetime.date(2025, 3, 5)

    def test_no_date_in_path(self):
        assert extract_path_date("https://example.com/article/abc") is None

    def test_invalid_calendar_date(self):
        # Feb 30 is not a real date
        assert extract_path_date("https://example.com/2025/02/30/story") is None

    def test_out_of_range_month(self):
        assert extract_path_date("https://example.com/2025/13/01/story") is None

    def test_hyphen_date_does_not_match(self):
        # /YYYY-MM-DD/ is a different pattern; we require slashes.
        assert extract_path_date("https://example.com/2025-03-15/story") is None

    def test_date_in_query_string_ignored(self):
        assert extract_path_date(
            "https://example.com/article?date=2025/03/15"
        ) is None

    def test_pre_2000_year(self):
        # Reject 1899 (we match 19xx/20xx only)
        assert extract_path_date("https://example.com/1899/01/01/old") is None

    def test_post_2099_year(self):
        # Year 2100 is outside our 19xx/20xx range
        assert extract_path_date("https://example.com/2100/01/01/future") is None

    def test_first_date_wins(self):
        # If the path contains two dates (unusual), take the first.
        assert extract_path_date(
            "https://example.com/2025/03/15/slug/2024/12/01/other"
        ) == datetime.date(2025, 3, 15)

    def test_empty_url(self):
        assert extract_path_date("") is None

    def test_date_at_end_no_trailing_slash(self):
        assert extract_path_date(
            "https://example.com/news/2025/03/15"
        ) == datetime.date(2025, 3, 15)


# ---------------------------------------------------------------------------
# wayback_exists_at
# ---------------------------------------------------------------------------

class _WaybackResponse:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


class _WaybackClient:
    """Stub for ``httpx.Client`` that captures CDX GET calls."""

    def __init__(self, response: _WaybackResponse, capture: dict | None = None):
        self.response = response
        self.capture = capture if capture is not None else {}

    def get(self, url: str, params: dict | None = None):
        self.capture["url"] = url
        self.capture["params"] = params or {}
        return self.response

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class _WaybackFailingClient:
    """Stub that always raises the configured exception on GET."""

    def __init__(self, exc: Exception):
        self.exc = exc

    def get(self, url: str, params: dict | None = None):
        raise self.exc

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class TestWaybackExistsAt:
    def _patch(self, monkeypatch, client):
        def factory(**_kwargs):
            return client
        monkeypatch.setattr(httpx, "Client", factory)

    def test_empty_url(self):
        assert wayback_exists_at("") == (False, None)

    def test_snapshot_found(self, monkeypatch):
        text = (
            '[["timestamp","original"],'
            '["20250315123045","https://www.example.com/story"]]'
        )
        client = _WaybackClient(_WaybackResponse(200, text))
        self._patch(monkeypatch, client)
        exists, snapshot = wayback_exists_at(
            "https://www.example.com/story",
            at=datetime.date(2025, 3, 15),
        )
        assert exists is True
        assert snapshot == (
            "https://web.archive.org/web/20250315123045/"
            "https://www.example.com/story"
        )

    def test_no_snapshot(self, monkeypatch):
        client = _WaybackClient(_WaybackResponse(200, '[]'))
        self._patch(monkeypatch, client)
        assert wayback_exists_at(
            "https://www.example.com/missing",
            at=datetime.date(2025, 3, 15),
        ) == (False, None)

    def test_only_header_row(self, monkeypatch):
        # CDX returns just the header row when there are zero hits.
        client = _WaybackClient(_WaybackResponse(200, '[["timestamp","original"]]'))
        self._patch(monkeypatch, client)
        assert wayback_exists_at(
            "https://www.example.com/x",
            at=datetime.date(2025, 3, 15),
        ) == (False, None)

    def test_api_error_status_returns_false(self, monkeypatch):
        client = _WaybackClient(_WaybackResponse(503, ''))
        self._patch(monkeypatch, client)
        assert wayback_exists_at("https://www.example.com/x") == (False, None)

    def test_malformed_json_returns_false(self, monkeypatch):
        client = _WaybackClient(_WaybackResponse(200, 'not json'))
        self._patch(monkeypatch, client)
        assert wayback_exists_at("https://www.example.com/x") == (False, None)

    def test_timeout_returns_false(self, monkeypatch):
        self._patch(monkeypatch, _WaybackFailingClient(
            httpx.TimeoutException("timed out")
        ))
        assert wayback_exists_at("https://www.example.com/x") == (False, None)

    def test_network_error_returns_false(self, monkeypatch):
        self._patch(monkeypatch, _WaybackFailingClient(Exception("dns fail")))
        assert wayback_exists_at("https://www.example.com/x") == (False, None)

    def test_tolerance_applied_to_params(self, monkeypatch):
        capture: dict = {}
        client = _WaybackClient(_WaybackResponse(200, '[]'), capture=capture)
        self._patch(monkeypatch, client)
        wayback_exists_at(
            "https://example.com/x",
            at=datetime.date(2025, 3, 15),
            tolerance_days=7,
        )
        assert capture["params"]["from"] == "20250308"
        assert capture["params"]["to"] == "20250322"

    def test_no_date_omits_date_params(self, monkeypatch):
        capture: dict = {}
        client = _WaybackClient(_WaybackResponse(200, '[]'), capture=capture)
        self._patch(monkeypatch, client)
        wayback_exists_at("https://example.com/x")
        assert "from" not in capture["params"]
        assert "to" not in capture["params"]

    def test_columns_in_unexpected_order(self, monkeypatch):
        # CDX may return columns in any order; we locate them by name.
        text = (
            '[["original","timestamp"],'
            '["https://example.com/page","20250510101010"]]'
        )
        client = _WaybackClient(_WaybackResponse(200, text))
        self._patch(monkeypatch, client)
        exists, snapshot = wayback_exists_at("https://example.com/page")
        assert exists is True
        assert "20250510101010" in snapshot
        assert snapshot.endswith("https://example.com/page")

    def test_missing_required_column(self, monkeypatch):
        # Header lacks one of the columns we need.
        text = '[["foo","bar"],["a","b"]]'
        client = _WaybackClient(_WaybackResponse(200, text))
        self._patch(monkeypatch, client)
        assert wayback_exists_at("https://example.com/x") == (False, None)

    def test_verifies_tls_by_default(self, monkeypatch):
        monkeypatch.delenv("URL_CHECK_VERIFY_TLS", raising=False)
        captured: dict = {}
        client = _WaybackClient(_WaybackResponse(200, '[]'))

        def factory(**kwargs):
            captured.update(kwargs)
            return client

        monkeypatch.setattr(httpx, "Client", factory)
        wayback_exists_at("https://example.com/x")
        assert captured["verify"] is True


# ---------------------------------------------------------------------------
# fetch_url_content
# ---------------------------------------------------------------------------

class _ContentResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


class _ContentClient:
    """Stub for httpx.Client that captures GET calls and returns a canned body."""

    def __init__(self, response: _ContentResponse | None = None,
                 get_error: Exception | None = None):
        self.response = response
        self.get_error = get_error
        self.captured: dict = {}

    def get(self, url: str):
        self.captured["url"] = url
        if self.get_error is not None:
            raise self.get_error
        return self.response

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class TestFetchUrlContent:
    def _patch(self, monkeypatch, client):
        monkeypatch.setattr(httpx, "Client", lambda **_kw: client)

    def test_empty_url(self):
        assert fetch_url_content("") == (0, "error", "")

    def test_200_returns_body(self, monkeypatch):
        client = _ContentClient(_ContentResponse(200, "<html>hi</html>"))
        self._patch(monkeypatch, client)
        code, cat, body = fetch_url_content("https://example.com/")
        assert code == 200
        assert cat == "ok"
        assert body == "<html>hi</html>"

    def test_404_returns_empty_body(self, monkeypatch):
        client = _ContentClient(_ContentResponse(404, "not found"))
        self._patch(monkeypatch, client)
        code, cat, body = fetch_url_content("https://example.com/missing")
        assert code == 404
        assert cat == "not_found"
        assert body == ""

    def test_403_returns_empty_body(self, monkeypatch):
        client = _ContentClient(_ContentResponse(403, "forbidden"))
        self._patch(monkeypatch, client)
        code, cat, body = fetch_url_content("https://example.com/paywall")
        assert cat == "forbidden"
        assert body == ""

    def test_timeout_returns_error(self, monkeypatch):
        client = _ContentClient(get_error=httpx.TimeoutException("timed out"))
        self._patch(monkeypatch, client)
        assert fetch_url_content("https://example.com/") == (0, "timeout", "")

    def test_network_error_returns_error(self, monkeypatch):
        client = _ContentClient(get_error=Exception("dns"))
        self._patch(monkeypatch, client)
        assert fetch_url_content("https://example.com/") == (0, "error", "")

    def test_body_truncated_at_max_size(self, monkeypatch):
        # Build a body larger than the 512 KB cap
        big = "x" * (600 * 1024)
        client = _ContentClient(_ContentResponse(200, big))
        self._patch(monkeypatch, client)
        _, _, body = fetch_url_content("https://example.com/")
        assert len(body.encode("utf-8", errors="ignore")) <= 512 * 1024

    def test_verifies_tls_by_default(self, monkeypatch):
        # The fetched body flows to downstream LLMs — TLS must be verified
        # unless an operator explicitly opts out.
        monkeypatch.delenv("URL_CHECK_VERIFY_TLS", raising=False)
        captured: dict = {}
        client = _ContentClient(_ContentResponse(200, "ok"))

        def factory(**kwargs):
            captured.update(kwargs)
            return client

        monkeypatch.setattr(httpx, "Client", factory)
        fetch_url_content("https://example.com/")
        assert captured["verify"] is True

    def test_verify_tls_disabled_via_env(self, monkeypatch):
        monkeypatch.setenv("URL_CHECK_VERIFY_TLS", "0")
        captured: dict = {}
        client = _ContentClient(_ContentResponse(200, "ok"))

        def factory(**kwargs):
            captured.update(kwargs)
            return client

        monkeypatch.setattr(httpx, "Client", factory)
        fetch_url_content("https://example.com/")
        assert captured["verify"] is False

    def test_blocked_url_returns_error(self, monkeypatch):
        def boom(*_a, **_k):
            raise InvalidInputError("blocked")

        monkeypatch.setattr("pf_core.utils.url_safety.assert_public_url", boom)
        assert fetch_url_content("http://127.0.0.1/") == (0, "error", "")


# ---------------------------------------------------------------------------
# extract_article_metadata
# ---------------------------------------------------------------------------

class TestExtractArticleMetadata:
    def test_empty_html(self):
        assert extract_article_metadata("") == {
            "title": "", "description": "", "og_title": "",
            "og_description": "", "twitter_title": "",
            "twitter_description": "", "first_paragraph": "",
        }

    def test_title_tag(self):
        html = "<html><head><title>Hello World</title></head></html>"
        result = extract_article_metadata(html)
        assert result["title"] == "Hello World"

    def test_meta_description(self):
        html = """
        <html><head>
          <meta name="description" content="A short description.">
        </head></html>
        """
        result = extract_article_metadata(html)
        assert result["description"] == "A short description."

    def test_og_and_twitter_tags(self):
        html = """
        <html><head>
          <meta property="og:title" content="OG Title">
          <meta property="og:description" content="OG desc">
          <meta name="twitter:title" content="TW Title">
          <meta name="twitter:description" content="TW desc">
        </head></html>
        """
        result = extract_article_metadata(html)
        assert result["og_title"] == "OG Title"
        assert result["og_description"] == "OG desc"
        assert result["twitter_title"] == "TW Title"
        assert result["twitter_description"] == "TW desc"

    def test_first_paragraph_from_article(self):
        html = """
        <html><body>
          <article>
            <p>This is the first substantive paragraph, long enough to count.</p>
            <p>And this one would be second.</p>
          </article>
        </body></html>
        """
        result = extract_article_metadata(html)
        assert "first substantive paragraph" in result["first_paragraph"]
        assert "second" not in result["first_paragraph"]

    def test_skips_short_paragraphs(self):
        """Very short <p> tags (e.g., captions) are skipped."""
        html = """
        <html><body>
          <article>
            <p>Tiny.</p>
            <p>This is the actually substantive paragraph of the article.</p>
          </article>
        </body></html>
        """
        result = extract_article_metadata(html)
        assert "substantive paragraph" in result["first_paragraph"]

    def test_no_article_tag_returns_empty_paragraph(self):
        """Without <article> or <main>, no first-paragraph capture."""
        html = "<html><body><p>Loose paragraph outside main region.</p></body></html>"
        result = extract_article_metadata(html)
        assert result["first_paragraph"] == ""

    def test_whitespace_collapsed(self):
        html = "<title>  Too   many    spaces  </title>"
        result = extract_article_metadata(html)
        assert result["title"] == "Too many spaces"

    def test_malformed_html_does_not_raise(self):
        html = "<html><title>x<p>y<article><p>long enough paragraph text here.</p>"
        result = extract_article_metadata(html)
        # Goal: no exception; at least title captured
        assert "x" in result["title"] or result["title"] == ""
