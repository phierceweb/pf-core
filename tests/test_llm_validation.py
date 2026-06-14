"""Tests for pf_core.llm.url_check — pluggable dispatcher."""

from __future__ import annotations

from pf_core.llm.url_check import (
    UrlHallucinationRule,
    url_looks_hallucinated,
    validate_urls,
)


def _always(reason: str) -> UrlHallucinationRule:
    def _rule(url: str) -> str | None:
        return reason
    return _rule


def _never() -> UrlHallucinationRule:
    def _rule(url: str) -> str | None:
        return None
    return _rule


def _contains(substr: str, reason: str) -> UrlHallucinationRule:
    def _rule(url: str) -> str | None:
        return reason if substr in url else None
    return _rule


class TestUrlLooksHallucinated:
    def test_empty_rules_returns_none(self):
        assert url_looks_hallucinated("https://example.com/page", []) is None

    def test_single_matching_rule_returns_reason(self):
        rules = [_contains("fake", "test-rule")]
        assert url_looks_hallucinated("https://example.com/fake", rules) == "test-rule"

    def test_single_non_matching_rule_returns_none(self):
        rules = [_contains("fake", "test-rule")]
        assert url_looks_hallucinated("https://example.com/real", rules) is None

    def test_first_matching_rule_wins(self):
        rules = [
            _contains("zzz", "second-rule"),
            _contains("fake", "first-rule"),
            _contains("fake", "third-rule"),
        ]
        # iteration order = rule order in list; second matching rule wins
        # because it's first in list
        assert url_looks_hallucinated("https://example.com/fake", rules) == "first-rule"

    def test_multiple_rules_none_match(self):
        rules = [_contains("xxx", "a"), _contains("yyy", "b")]
        assert url_looks_hallucinated("https://example.com/page", rules) is None

    def test_always_rule_fires_on_any_url(self):
        rules = [_always("flag-everything")]
        assert url_looks_hallucinated("https://anything.test/", rules) == "flag-everything"

    def test_never_rule_passes_any_url(self):
        rules = [_never()]
        assert url_looks_hallucinated("https://anything.test/", rules) is None

    def test_rule_receives_url(self):
        captured: list[str] = []

        def _capture(url: str) -> str | None:
            captured.append(url)
            return None

        url_looks_hallucinated("https://example.com/x", [_capture])
        assert captured == ["https://example.com/x"]


class TestValidateUrls:
    def test_empty_urls(self):
        assert validate_urls([], [_always("r")]) == []

    def test_empty_rules_all_pass(self):
        results = validate_urls(
            ["https://a.test/", "https://b.test/"],
            rules=[],
        )
        assert results == [
            ("https://a.test/", True, None),
            ("https://b.test/", True, None),
        ]

    def test_mixed(self):
        rules = [_contains("bad", "matched-bad")]
        results = validate_urls(
            ["https://example.com/ok", "https://example.com/bad"],
            rules=rules,
        )
        assert results == [
            ("https://example.com/ok", True, None),
            ("https://example.com/bad", False, "matched-bad"),
        ]

    def test_all_flagged(self):
        rules = [_always("flag")]
        results = validate_urls(["https://a.test/", "https://b.test/"], rules)
        assert not any(ok for _, ok, _ in results)
        assert all(reason == "flag" for _, _, reason in results)
