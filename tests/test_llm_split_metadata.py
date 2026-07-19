"""Tests for pf_core.llm.tracking.split_metadata — metadata dict → (tags, metrics)."""

from __future__ import annotations

from pf_core.llm.tracking import split_metadata


def test_strings_and_bools_become_tags_numbers_become_metrics():
    tags, metrics = split_metadata(
        {
            "source_name": "report.pdf",
            "verified": True,
            "heading_count": 1750,
            "cost_ratio": 0.5,
            "skipped": None,
        }
    )
    assert set(tags) == {"source_name:report.pdf", "verified:true"}
    assert metrics == {"heading_count": 1750.0, "cost_ratio": 0.5}


def test_false_bool_is_tag_not_metric():
    tags, metrics = split_metadata({"flag": False})
    assert tags == ["flag:false"]
    assert metrics == {}


def test_tag_truncated_to_64_chars():
    tags, _ = split_metadata({"k": "v" * 200})
    assert len(tags[0]) == 64
    assert tags[0].startswith("k:vvv")


def test_metric_key_truncated_to_64_chars():
    _, metrics = split_metadata({"k" * 100: 2})
    assert list(metrics) == ["k" * 64]
    assert metrics["k" * 64] == 2.0


def test_empty_dict():
    assert split_metadata({}) == ([], {})
