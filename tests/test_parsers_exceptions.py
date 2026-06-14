"""Tests for ``pf_core.parsers.exceptions``.

These exception types form the contract between consumer parsers and
their ingest orchestrator. Tests here cover the exception shape and the
re-export surface; consumers cover the catch-and-handle behavior.
"""

from __future__ import annotations

import pytest

from pf_core.exceptions import AppError
from pf_core.parsers import ParseError, PaywalledPost
from pf_core.parsers.exceptions import (
    ParseError as ExceptionsParseError,
    PaywalledPost as ExceptionsPaywalledPost,
)


class TestInheritance:
    def test_parse_error_is_app_error(self):
        assert issubclass(ParseError, AppError)

    def test_paywalled_post_is_app_error(self):
        assert issubclass(PaywalledPost, AppError)

    def test_parse_error_and_paywalled_post_are_distinct(self):
        """Distinct so callers can disambiguate paywall skips from real errors."""
        assert not issubclass(PaywalledPost, ParseError)
        assert not issubclass(ParseError, PaywalledPost)


class TestReExports:
    def test_parse_error_importable_from_package_root(self):
        assert ParseError is ExceptionsParseError

    def test_paywalled_post_importable_from_package_root(self):
        assert PaywalledPost is ExceptionsPaywalledPost


class TestRaiseAndCatch:
    def test_parse_error_raises_with_message(self):
        with pytest.raises(ParseError) as ei:
            raise ParseError("feed returned 0 items")
        assert "feed returned 0 items" in str(ei.value)

    def test_parse_error_carries_app_error_context(self):
        with pytest.raises(ParseError) as ei:
            raise ParseError("bad shape", context={"source": "example"})
        assert ei.value.context == {"source": "example"}

    def test_parse_error_chains_cause(self):
        original = ValueError("malformed XML")
        with pytest.raises(ParseError) as ei:
            raise ParseError("parse failed", cause=original)
        assert ei.value.__cause__ is original

    def test_paywalled_post_raises_with_message(self):
        with pytest.raises(PaywalledPost) as ei:
            raise PaywalledPost("subscriber-only")
        assert "subscriber-only" in str(ei.value)

    def test_catching_app_error_catches_both(self):
        """Orchestrators that fall back on ``except AppError`` still catch these."""
        for exc_cls in (ParseError, PaywalledPost):
            with pytest.raises(AppError):
                raise exc_cls("test")

    def test_paywalled_does_not_match_parse_error_handler(self):
        """A paywalled-post handler must not catch generic parse errors and vice versa."""
        with pytest.raises(PaywalledPost):
            try:
                raise PaywalledPost("paywalled")
            except ParseError:
                pytest.fail("PaywalledPost should not be caught by ParseError handler")
