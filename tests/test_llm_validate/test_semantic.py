"""Built-in semantic validators driven through ``register`` + ``parse_and_validate``."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from pf_core.exceptions import ConfigurationError
from pf_core.llm.validate import (
    parse_and_validate,
    register,
    register_tier1_domains,
    register_url_hallucination_rules,
)

from .conftest import Doc, payload


# url_sanity ----------------------------------------------------------------


def _flag_fake_substring():
    """Test rule: flag any URL containing the literal token 'fake'."""

    def _rule(url: str) -> str | None:
        return "contains fake" if "fake" in url else None

    return _rule


def test_url_sanity_no_hook_returns_passing_info():
    register(agent_type="u", shape=Doc, semantic=["url_sanity"])
    res = parse_and_validate(
        payload(sources=["https://anything.test/anything"]), agent_type="u",
    )
    sig = next(s for s in res.signals if s.validator == "url_sanity")
    assert sig.passed is True
    assert sig.severity == "info"
    assert sig.details["reason"] == "no url hallucination rules registered"


def test_url_sanity_passes_when_no_urls_match_rules():
    register_url_hallucination_rules(lambda: [_flag_fake_substring()])
    register(agent_type="u", shape=Doc, semantic=["url_sanity"])
    res = parse_and_validate(
        payload(sources=["https://example.com/real-story"]), agent_type="u",
    )
    sig = next(s for s in res.signals if s.validator == "url_sanity")
    assert sig.passed is True
    assert sig.severity == "info"


def test_url_sanity_warns_on_flagged_url_default_severity():
    register_url_hallucination_rules(lambda: [_flag_fake_substring()])
    register(agent_type="u", shape=Doc, semantic=["url_sanity"])
    res = parse_and_validate(
        payload(sources=["https://example.com/fake-story-2025"]), agent_type="u",
    )
    assert res.ok is True  # warn does not flip ok
    sig = next(s for s in res.signals if s.validator == "url_sanity")
    assert sig.passed is False
    assert sig.severity == "warn"
    assert "flagged" in sig.details
    assert sig.details["flagged"][0]["reason"] == "contains fake"


def test_url_sanity_severity_override_to_error():
    register_url_hallucination_rules(lambda: [_flag_fake_substring()])
    register(agent_type="u", shape=Doc, semantic=["url_sanity:error"])
    res = parse_and_validate(
        payload(sources=["https://example.com/fake-story-2025"]), agent_type="u",
    )
    assert res.ok is False
    sig = next(s for s in res.signals if s.validator == "url_sanity")
    assert sig.severity == "error"
    assert sig.passed is False


# tier1_ratio ---------------------------------------------------------------


def test_tier1_ratio_meets_threshold():
    register_tier1_domains(lambda: {"apnews.com"})
    register(agent_type="t", shape=Doc, semantic=["tier1_ratio:0.6"])
    raw = payload(sources=[
        "https://apnews.com/article/abc",
        "https://apnews.com/article/def",
        "https://example.com/story",
    ])
    res = parse_and_validate(raw, agent_type="t")
    sig = next(s for s in res.signals if s.validator == "tier1_ratio")
    assert sig.passed is True
    assert sig.details["ratio"] >= 0.6


def test_tier1_ratio_below_threshold_fails():
    register_tier1_domains(lambda: {"apnews.com"})
    register(agent_type="t", shape=Doc, semantic=["tier1_ratio:0.6"])
    raw = payload(sources=[
        "https://apnews.com/article/abc",
        "https://example.com/a",
        "https://example.com/b",
    ])
    res = parse_and_validate(raw, agent_type="t")
    sig = next(s for s in res.signals if s.validator == "tier1_ratio")
    assert sig.passed is False
    assert sig.severity == "warn"
    assert sig.details["ratio"] < 0.6


def test_tier1_ratio_no_hook_returns_passing_info():
    register(agent_type="t", shape=Doc, semantic=["tier1_ratio:0.6"])
    res = parse_and_validate(
        payload(sources=["https://apnews.com/article/abc"]), agent_type="t",
    )
    sig = next(s for s in res.signals if s.validator == "tier1_ratio")
    assert sig.passed is True
    assert sig.severity == "info"
    assert sig.details["reason"] == "no tier1 domain hook registered"


# field_non_empty -----------------------------------------------------------


def test_field_non_empty_pass_and_fail():
    register(agent_type="f", shape=Doc, semantic=["field_non_empty:headline,body"])
    ok = parse_and_validate(payload(headline="x", body="y"), agent_type="f")
    bad = parse_and_validate(payload(headline="", body="y"), agent_type="f")
    ok_sig = next(s for s in ok.signals if s.validator == "field_non_empty")
    bad_sig = next(s for s in bad.signals if s.validator == "field_non_empty")
    assert ok_sig.passed is True
    assert bad_sig.passed is False
    assert "headline" in bad_sig.details["empty_fields"]


def test_field_non_empty_missing_key_fails():
    register(agent_type="f", shape=Doc, semantic=["field_non_empty:custom_field"])
    res = parse_and_validate(payload(headline="x"), agent_type="f")
    sig = next(s for s in res.signals if s.validator == "field_non_empty")
    assert sig.passed is False
    assert "custom_field" in sig.details["empty_fields"]


# min_items -----------------------------------------------------------------


def test_min_items_pass():
    register(agent_type="m", shape=Doc, semantic=["min_items:sources:2"])
    res = parse_and_validate(payload(sources=["a", "b"]), agent_type="m")
    sig = next(s for s in res.signals if s.validator == "min_items")
    assert sig.passed is True


def test_min_items_below_threshold_fails():
    register(agent_type="m", shape=Doc, semantic=["min_items:sources:2"])
    res = parse_and_validate(payload(sources=["a"]), agent_type="m")
    sig = next(s for s in res.signals if s.validator == "min_items")
    assert sig.passed is False
    assert sig.details["actual"] == 1
    assert sig.details["minimum"] == 2


def test_min_items_not_a_list_fails():
    class _LooseDoc(BaseModel):
        sources: int = 0
        model_config = {"extra": "allow"}

    register(agent_type="m2", shape=_LooseDoc, semantic=["min_items:sources:2"])
    res = parse_and_validate(payload(sources=5), agent_type="m2")
    sig = next(s for s in res.signals if s.validator == "min_items")
    assert sig.passed is False
    assert sig.details["reason"] == "not a list"


# no_duplicate_urls ---------------------------------------------------------


def test_no_duplicate_urls_pass():
    register(agent_type="n", shape=Doc, semantic=["no_duplicate_urls"])
    res = parse_and_validate(
        payload(sources=["https://a.com/x", "https://b.com/y"]), agent_type="n",
    )
    sig = next(s for s in res.signals if s.validator == "no_duplicate_urls")
    assert sig.passed is True


def test_no_duplicate_urls_detects_dupes_in_nested():
    register(agent_type="n", shape=Doc, semantic=["no_duplicate_urls"])
    raw = payload(sources=["https://a.com/x", "https://a.com/x"])
    res = parse_and_validate(raw, agent_type="n")
    sig = next(s for s in res.signals if s.validator == "no_duplicate_urls")
    assert sig.passed is False
    assert "https://a.com/x" in sig.details["duplicates"]
    assert sig.details["duplicates"]["https://a.com/x"] >= 2


# date_range ----------------------------------------------------------------


_DATE_SPEC = "date_range:published_at:2020-01-01:today"


def test_date_range_in_range():
    register(agent_type="d", shape=Doc, semantic=[_DATE_SPEC])
    res = parse_and_validate(payload(published_at="2024-06-01"), agent_type="d")
    sig = next(s for s in res.signals if s.validator == "date_range")
    assert sig.passed is True


def test_date_range_before_start_fails():
    register(agent_type="d", shape=Doc, semantic=[_DATE_SPEC])
    res = parse_and_validate(payload(published_at="1999-06-01"), agent_type="d")
    sig = next(s for s in res.signals if s.validator == "date_range")
    assert sig.passed is False


def test_date_range_missing_field_fails():
    register(agent_type="d", shape=Doc, semantic=[_DATE_SPEC])
    res = parse_and_validate(payload(headline="x"), agent_type="d")
    sig = next(s for s in res.signals if s.validator == "date_range")
    assert sig.passed is False
    assert sig.details["reason"] == "missing"


def test_date_range_bad_string_fails():
    register(agent_type="d", shape=Doc, semantic=[_DATE_SPEC])
    res = parse_and_validate(payload(published_at="not a date"), agent_type="d")
    sig = next(s for s in res.signals if s.validator == "date_range")
    assert sig.passed is False
    assert sig.details["reason"] == "not an ISO date"


# Spec-validation -----------------------------------------------------------


def test_malformed_min_items_spec_raises_at_register():
    with pytest.raises(ConfigurationError, match="min_items"):
        register(agent_type="bad", shape=Doc, semantic=["min_items:foo"])
