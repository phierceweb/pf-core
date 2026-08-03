"""Tests for pf_core.utils.article_fetch.

Tests that don't require the ``articles`` extra (trafilatura/htmldate)
run unconditionally. Tests that exercise the live fetch + extract are
gated on the deps being importable.
"""
from __future__ import annotations

import datetime

import pytest

from pf_core.utils import article_fetch as af
from pf_core.utils.article_fetch import (
    FETCHER_VERSION,
    _live_fetch_with_retry,
    _looks_paywalled,
    _parse_iso_date,
    _first_str,
    _empty_result,
)


# Deps gate. Tests inside `_HAS_DEPS_BLOCK` skip when extras missing.
try:
    import trafilatura  # noqa: F401
    import htmldate  # noqa: F401
    HAS_DEPS = True
except ImportError:
    HAS_DEPS = False


class TestModuleSurface:
    def test_fetcher_version_is_int(self):
        assert isinstance(FETCHER_VERSION, int)
        assert FETCHER_VERSION >= 1

    def test_fetched_article_dataclass_fields(self):
        art = _empty_result("https://example.com/x", fetch_status="ok")
        assert art.url == "https://example.com/x"
        assert art.fetch_status == "ok"
        assert art.used_wayback is False
        assert art.title == ""
        assert art.body == ""
        assert isinstance(art.raw_meta, dict)


class TestPaywallHeuristic:
    def test_short_body_no_cta_not_paywalled(self):
        assert not _looks_paywalled("Short article body without any CTAs.")

    def test_short_body_with_cta_is_paywalled(self):
        assert _looks_paywalled(
            "This is a teaser for a paywalled article. Subscribe to continue reading."
        )

    def test_long_body_with_cta_not_paywalled(self):
        body = "Real article content " * 50  # > 500 chars
        body += " Subscribe to continue."
        assert not _looks_paywalled(body)

    def test_empty_body_not_paywalled(self):
        assert not _looks_paywalled("")

    def test_mixed_case_marker_caught(self):
        assert _looks_paywalled("Short tease. SIGN IN TO CONTINUE.")


class TestParseIsoDate:
    def test_iso_date_string(self):
        from datetime import date
        assert _parse_iso_date("2026-04-15") == date(2026, 4, 15)

    def test_iso_datetime_string(self):
        from datetime import date
        assert _parse_iso_date("2026-04-15T12:00:00") == date(2026, 4, 15)

    def test_timezoned_iso(self):
        from datetime import date
        assert _parse_iso_date("2026-04-15T12:00:00+00:00") == date(2026, 4, 15)

    def test_date_object_passthrough(self):
        from datetime import date
        d = date(2026, 4, 15)
        assert _parse_iso_date(d) == d

    def test_none_returns_none(self):
        assert _parse_iso_date(None) is None

    def test_empty_string_returns_none(self):
        assert _parse_iso_date("") is None

    def test_garbage_returns_none(self):
        assert _parse_iso_date("not a date") is None

    def test_invalid_calendar_date_returns_none(self):
        assert _parse_iso_date("2026-13-99") is None

    def test_non_string_non_date_returns_none(self):
        assert _parse_iso_date(12345) is None
        assert _parse_iso_date([]) is None


class TestFirstStr:
    def test_string_passthrough(self):
        assert _first_str("hello") == "hello"

    def test_none_to_empty(self):
        assert _first_str(None) == ""

    def test_list_returns_first_nonempty_string(self):
        assert _first_str(["", "first", "second"]) == "first"

    def test_list_all_empty_returns_empty(self):
        assert _first_str(["", "  ", ""]) == ""

    def test_list_no_strings_returns_empty(self):
        assert _first_str([1, 2, 3]) == ""

    def test_other_types_stringified(self):
        assert _first_str(42) == "42"


class TestImportError:
    """When extras missing, fetch_article raises ImportError on first call.

    Module import must still succeed so consumers can introspect / type-check.
    """

    def test_module_imports_without_deps(self):
        # If we got here, the module imported. That's the test.
        from pf_core.utils import article_fetch
        assert article_fetch.fetch_article is not None

    def test_helpful_error_when_deps_missing(self, monkeypatch):
        # Force the dep gate off and check the error message.
        from pf_core.utils import article_fetch
        monkeypatch.setattr(article_fetch, "_HAS_DEPS", False)
        with pytest.raises(ImportError, match="articles"):
            article_fetch.fetch_article("https://example.com/x")


@pytest.fixture
def _fast_retry(monkeypatch):
    """Drop retry waits so the retry-exhaustion tests run in milliseconds."""
    monkeypatch.setattr(af, "_RETRY_INITIAL_WAIT", 0.0)
    monkeypatch.setattr(af, "_RETRY_MAX_WAIT", 0.0)


class TestLiveFetchWithRetry:
    """Maps `fetch_url_content` outcomes to (fetch_status, body) pairs.

    Doesn't need the ``articles`` extra — the retry helper only uses
    `pf_core.utils.urls.fetch_url_content`, which we mock.
    """

    def test_2xx_with_body_returns_ok(self, monkeypatch):
        monkeypatch.setattr(
            af, "fetch_url_content", lambda url: (200, "ok", "<html>x</html>"),
        )
        status, body = _live_fetch_with_retry("https://example.com/x")
        assert status == "ok"
        assert body == "<html>x</html>"

    def test_404_returns_not_found_no_retry(self, monkeypatch):
        calls = []
        def fake(url):
            calls.append(url)
            return 404, "not_found", ""
        monkeypatch.setattr(af, "fetch_url_content", fake)
        status, body = _live_fetch_with_retry("https://example.com/x")
        assert (status, body) == ("not_found", "")
        assert len(calls) == 1  # deterministic — no retry

    def test_410_returns_not_found(self, monkeypatch):
        monkeypatch.setattr(
            af, "fetch_url_content", lambda url: (410, "gone", ""),
        )
        status, body = _live_fetch_with_retry("https://example.com/x")
        assert status == "not_found"

    def test_401_returns_paywalled(self, monkeypatch):
        monkeypatch.setattr(
            af, "fetch_url_content", lambda url: (401, "http_401", ""),
        )
        status, body = _live_fetch_with_retry("https://example.com/x")
        assert status == "paywalled"

    def test_403_forbidden_category_returns_paywalled(self, monkeypatch):
        monkeypatch.setattr(
            af, "fetch_url_content", lambda url: (403, "forbidden", ""),
        )
        status, body = _live_fetch_with_retry("https://example.com/x")
        assert status == "paywalled"

    def test_500_returns_blocked(self, monkeypatch):
        monkeypatch.setattr(
            af, "fetch_url_content", lambda url: (500, "http_500", ""),
        )
        status, body = _live_fetch_with_retry("https://example.com/x")
        assert status == "blocked"

    def test_429_returns_blocked(self, monkeypatch):
        # 429 is transient at the HTTP layer but `fetch_url_content`
        # surfaces it as `http_429` (not `timeout`/`error`), so the
        # retry helper does not retry — it categorizes as blocked.
        monkeypatch.setattr(
            af, "fetch_url_content", lambda url: (429, "http_429", ""),
        )
        status, body = _live_fetch_with_retry("https://example.com/x")
        assert status == "blocked"

    def test_timeout_exhausts_retries(self, monkeypatch, _fast_retry):
        calls = []
        def fake(url):
            calls.append(url)
            return 0, "timeout", ""
        monkeypatch.setattr(af, "fetch_url_content", fake)
        status, body = _live_fetch_with_retry("https://example.com/x")
        assert (status, body) == ("timeout", "")
        assert len(calls) == af._RETRY_ATTEMPTS

    def test_error_exhausts_retries(self, monkeypatch, _fast_retry):
        calls = []
        def fake(url):
            calls.append(url)
            return 0, "error", ""
        monkeypatch.setattr(af, "fetch_url_content", fake)
        status, body = _live_fetch_with_retry("https://example.com/x")
        assert (status, body) == ("error", "")
        assert len(calls) == af._RETRY_ATTEMPTS

    def test_empty_2xx_body_is_treated_as_transient(self, monkeypatch, _fast_retry):
        # Empty body on a 2xx is suspicious — could be a soft block. The
        # retry helper raises a transient error so tenacity retries it.
        calls = []
        def fake(url):
            calls.append(url)
            return 200, "ok", ""
        monkeypatch.setattr(af, "fetch_url_content", fake)
        status, body = _live_fetch_with_retry("https://example.com/x")
        # All retries returned empty body → exhausts as 'error'
        assert status == "error"
        assert len(calls) == af._RETRY_ATTEMPTS

    def test_recovers_after_transient_failure(self, monkeypatch, _fast_retry):
        # First attempt fails with timeout; second succeeds.
        results = iter([(0, "timeout", ""), (200, "ok", "<html>ok</html>")])
        monkeypatch.setattr(af, "fetch_url_content", lambda url: next(results))
        status, body = _live_fetch_with_retry("https://example.com/x")
        assert status == "ok"
        assert body == "<html>ok</html>"


class TestFetchArticleWaybackFlag:
    """Verify env-var / kwarg gating for the Wayback fallback.

    The tests mock both `fetch_url_content` and `wayback_exists_at` and
    drive the live fetch into states (paywalled, not_found) that don't
    invoke the extractor — so they don't need the ``articles`` extra.
    `_HAS_DEPS` is forced True to bypass the import-time gate.
    """

    @pytest.fixture(autouse=True)
    def _force_deps_present(self, monkeypatch):
        monkeypatch.setattr(af, "_HAS_DEPS", True)

    def test_kwarg_overrides_env(self, monkeypatch):
        # Even with env disabled, an explicit True kwarg should attempt
        # wayback. We mock fetch_url_content to fail (paywalled) and
        # wayback_exists_at to return False, so the call still resolves
        # to a paywalled stub — the assertion is that wayback_exists_at
        # was consulted.
        monkeypatch.setenv("PF_ARTICLE_WAYBACK_FALLBACK", "0")
        wb_calls = []
        def fake_wb(url, *, at=None, **kw):
            wb_calls.append(url)
            return False, None
        monkeypatch.setattr(
            af, "fetch_url_content", lambda url: (403, "forbidden", ""),
        )
        monkeypatch.setattr(af, "wayback_exists_at", fake_wb)
        result = af.fetch_article("https://example.com/x", wayback_fallback=True)
        assert result.fetch_status == "paywalled"
        assert len(wb_calls) == 1

    def test_env_disables_wayback(self, monkeypatch):
        monkeypatch.setenv("PF_ARTICLE_WAYBACK_FALLBACK", "0")
        wb_calls = []
        monkeypatch.setattr(
            af, "fetch_url_content", lambda url: (403, "forbidden", ""),
        )
        monkeypatch.setattr(
            af, "wayback_exists_at",
            lambda url, **kw: (wb_calls.append(url) or (False, None)),
        )
        result = af.fetch_article("https://example.com/x")
        assert result.fetch_status == "paywalled"
        assert wb_calls == []  # not consulted

    def test_not_found_skips_wayback(self, monkeypatch):
        wb_calls = []
        monkeypatch.setattr(
            af, "fetch_url_content", lambda url: (404, "not_found", ""),
        )
        monkeypatch.setattr(
            af, "wayback_exists_at",
            lambda url, **kw: (wb_calls.append(url) or (False, None)),
        )
        result = af.fetch_article("https://example.com/x")
        assert result.fetch_status == "not_found"
        assert wb_calls == []  # 404 short-circuits

    def test_empty_string_returns_error_stub(self, monkeypatch):
        result = af.fetch_article("")
        assert result.fetch_status == "error"
        assert result.url == ""

    def test_whitespace_only_returns_error_stub(self, monkeypatch):
        result = af.fetch_article("   ")
        assert result.fetch_status == "error"

    def test_non_string_returns_error_stub(self, monkeypatch):
        result = af.fetch_article(None)  # type: ignore[arg-type]
        assert result.fetch_status == "error"


def _as_transport_text(raw: bytes) -> str:
    """Decode ``raw`` the way the HTTP layer does.

    Detection runs on decoded text, never on bytes, so a test that feeds a str
    literal built from the raw signature proves nothing — non-ASCII signature
    bytes have already become U+FFFD by the time we see them.

    This mirrors ``fetch_url_content`` rather than defining it;
    ``TestBinaryDetectionOverRealTransport`` is what pins the two together.
    """
    import httpx

    return httpx.Response(200, content=raw).text


class TestLooksBinary:
    """Magic-byte detection on the already-decoded response text."""

    @pytest.mark.parametrize("raw,label", [
        (b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj", "pdf"),
        (b"PK\x03\x04\x14\x00\x06\x00", "zip/ooxml"),
        (b"%!PS-Adobe-3.0", "postscript"),
        (b"GIF89a\x01\x00", "gif"),
        (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR", "png"),
    ])
    def test_known_signatures_survive_transport_decode(self, raw, label):
        assert af.looks_binary(_as_transport_text(raw)) == label

    @pytest.mark.parametrize("raw", [
        b"\xff\xd8\xff\xe0\x00\x10JFIF",  # jpeg
        b"\x1f\x8b\x08\x00",               # gzip
    ])
    def test_replacement_char_only_signatures_are_not_claimed(self, raw):
        # These decode to bare U+FFFD runs, which are too weak to match on.
        # They must fall through to the extractor and land on `no_content`
        # rather than being falsely detected here.
        assert af.looks_binary(_as_transport_text(raw)) is None

    @pytest.mark.parametrize("payload", [
        "",
        "<html><body>Real article</body></html>",
        "﻿<!DOCTYPE html><html></html>",       # BOM-prefixed HTML
        "\n\n  <html>indented</html>",              # leading whitespace
        "Plain text with no markup at all.",
        "The report (%PDF- format) was released.",  # signature not at offset 0
    ])
    def test_non_binary_not_detected(self, payload):
        assert af.looks_binary(payload) is None

    def test_only_the_head_is_scanned(self):
        # A signature buried past the window must not trip detection.
        assert af.looks_binary("<html>" + "x" * 5000 + "%PDF-") is None


class TestBinaryDetectionOverRealTransport:
    """`looks_binary` must fire on what `fetch_url_content` actually returns.

    The two are coupled through the body's decode-error mode: a signature byte
    outside ASCII survives as U+FFFD under ``replace`` but vanishes under
    ``ignore``. Every other test in this file feeds `looks_binary` a hand-decoded
    string, so only this one notices if the fetch path's decoding drifts.
    """

    @pytest.mark.parametrize("raw,label", [
        (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR", "png"),
        (b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj", "pdf"),
        (b"GIF89a\x01\x00", "gif"),
        (b"PK\x03\x04\x14\x00\x06\x00", "zip/ooxml"),
    ])
    def test_signature_survives_the_real_fetch_path(self, monkeypatch, raw, label):
        import httpx

        from pf_core.utils import urls as urls_mod

        def handler(request):
            return httpx.Response(200, content=raw)

        real_client = httpx.Client

        def mock_client(**kwargs):
            kwargs.pop("verify", None)
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(urls_mod.httpx, "Client", mock_client)
        code, category, body = urls_mod.fetch_url_content("https://example.com/f")

        assert (code, category) == (200, "ok")
        assert af.looks_binary(body) == label


class TestFetchStatusVocabulary:
    def test_exported_set_is_complete(self):
        assert af.FETCH_STATUSES == frozenset({
            "ok", "paywalled", "not_found", "blocked", "timeout", "error",
            "unsupported_content_type", "no_content",
        })

    def test_fetcher_version_bumped_for_new_statuses(self):
        # The two new statuses reclassify previously-"ok" cache rows, so
        # consumers must be forced to re-read.
        assert FETCHER_VERSION >= 4


class TestBinaryBodyRejection:
    """A 2xx carrying binary must never reach the extractor."""

    def test_pdf_body_returns_unsupported_content_type(self, monkeypatch):
        pdf_text = _as_transport_text(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj")
        monkeypatch.setattr(
            af, "fetch_url_content", lambda url: (200, "ok", pdf_text),
        )
        status, body = _live_fetch_with_retry("https://example.com/report.pdf")
        assert status == "unsupported_content_type"
        assert body == ""  # mojibake discarded, never cached

    def test_html_body_still_ok(self, monkeypatch):
        monkeypatch.setattr(
            af, "fetch_url_content",
            lambda url: (200, "ok", "<html><body>hi</body></html>"),
        )
        status, _ = _live_fetch_with_retry("https://example.com/x")
        assert status == "ok"

    def test_unsupported_content_type_skips_wayback(self, monkeypatch):
        wb_calls = []
        monkeypatch.setattr(
            af, "fetch_url_content", lambda url: (200, "ok", "%PDF-1.4 junk"),
        )
        monkeypatch.setattr(
            af, "wayback_exists_at",
            lambda url, **kw: (wb_calls.append(url) or (False, None)),
        )
        result = af.fetch_article("https://example.com/a.pdf")
        assert result.fetch_status == "unsupported_content_type"
        assert result.body == ""
        # The archive replays the same bytes — a PDF is still a PDF.
        assert wb_calls == []


@pytest.mark.skipif(not HAS_DEPS, reason="requires the 'articles' extra")
class TestNoContent:
    """A 2xx whose body the extractor cannot recover is not a success."""

    def test_unextractable_html_is_no_content(self):
        art = af._extract_from_html(
            url="https://example.com/x",
            final_url="https://example.com/x",
            html="<html><head><title>t</title></head><body></body></html>",
            used_wayback=False,
            outlet="example.com",
            canon="https://example.com/x",
        )
        assert art.fetch_status == "no_content"
        assert art.body == ""

    def test_real_article_is_ok(self):
        html = (
            "<html><body><article><p>"
            + ("Substantive reporting text. " * 20)
            + "</p></article></body></html>"
        )
        art = af._extract_from_html(
            url="https://example.com/x",
            final_url="https://example.com/x",
            html=html,
            used_wayback=False,
            outlet="example.com",
            canon="https://example.com/x",
        )
        assert art.fetch_status == "ok"
        assert art.body

    def test_no_content_falls_through_to_wayback(self, monkeypatch):
        wb_calls = []
        monkeypatch.setattr(
            af, "fetch_url_content",
            lambda url: (200, "ok", "<html><body></body></html>"),
        )
        monkeypatch.setattr(
            af, "wayback_exists_at",
            lambda url, **kw: (wb_calls.append(url) or (False, None)),
        )
        result = af.fetch_article("https://example.com/js-only")
        assert result.fetch_status == "no_content"
        assert wb_calls == ["https://example.com/js-only"]

    def test_no_content_keeps_metadata_the_extractor_recovered(self, monkeypatch):
        # A JS-rendered shell still carries a publish date. Downgrading the
        # status must not also drop what extraction did find.
        html = (
            '<html><head><title>Council approves budget</title>'
            '<meta property="article:published_time" content="2026-03-04">'
            '</head><body><div id="root"></div></body></html>'
        )
        monkeypatch.setattr(
            af, "fetch_url_content", lambda url: (200, "ok", html),
        )
        monkeypatch.setattr(
            af, "wayback_exists_at", lambda url, **kw: (False, None),
        )
        result = af.fetch_article("https://example.com/js-only")
        assert result.fetch_status == "no_content"
        assert result.body == ""
        assert result.date_published == datetime.date(2026, 3, 4)

    def test_empty_wayback_snapshot_does_not_mask_live_status(self, monkeypatch):
        def fake_fetch(url):
            if "web.archive.org" in url:
                return 200, "ok", "<html><body></body></html>"
            return 403, "forbidden", ""

        monkeypatch.setattr(af, "fetch_url_content", fake_fetch)
        monkeypatch.setattr(
            af, "wayback_exists_at",
            lambda url, **kw: (True, "https://web.archive.org/web/1/" + url),
        )
        result = af.fetch_article("https://example.com/x")
        # The live verdict (paywalled) is more specific than the snapshot's.
        assert result.fetch_status == "paywalled"


class TestExtractorLoggersQuieted:
    def test_trafilatura_and_htmldate_are_capped(self):
        import logging

        from pf_core.utils import _article_extract

        if not _article_extract._HAS_DEPS:
            pytest.skip("requires the 'articles' extra")
        for name in ("trafilatura", "htmldate"):
            assert logging.getLogger(name).level >= logging.CRITICAL

    def test_env_var_overrides_level(self, monkeypatch):
        import logging

        from pf_core.utils import _article_extract

        monkeypatch.setenv("PF_ARTICLE_EXTRACTOR_LOG_LEVEL", "DEBUG")
        try:
            _article_extract._quiet_extractor_loggers()
            assert logging.getLogger("trafilatura").level == logging.DEBUG
        finally:
            monkeypatch.delenv("PF_ARTICLE_EXTRACTOR_LOG_LEVEL", raising=False)
            _article_extract._quiet_extractor_loggers()

    @pytest.mark.parametrize("bogus", ["nonsense", "BASIC_FORMAT", ""])
    def test_malformed_level_falls_back_instead_of_raising(
        self, monkeypatch, bogus,
    ):
        import logging

        from pf_core.utils import _article_extract

        monkeypatch.setenv("PF_ARTICLE_EXTRACTOR_LOG_LEVEL", bogus)
        try:
            _article_extract._quiet_extractor_loggers()
            assert logging.getLogger("trafilatura").level == logging.CRITICAL
        finally:
            monkeypatch.delenv("PF_ARTICLE_EXTRACTOR_LOG_LEVEL", raising=False)
            _article_extract._quiet_extractor_loggers()
