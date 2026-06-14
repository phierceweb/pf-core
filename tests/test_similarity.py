"""Tests for pf_core.utils.similarity."""

from __future__ import annotations

import pytest

from pf_core.utils.similarity import is_near_duplicate, jaccard, shingle


class TestShingle:
    def test_basic(self):
        result = shingle("abcdef", k=3)
        assert result == {"abc", "bcd", "cde", "def"}

    def test_short_string(self):
        result = shingle("ab", k=4)
        assert result == {"ab"}

    def test_exact_k_length(self):
        result = shingle("abcd", k=4)
        assert result == {"abcd"}

    def test_k_equals_1(self):
        result = shingle("abc", k=1)
        assert result == {"a", "b", "c"}

    def test_empty_string(self):
        result = shingle("", k=4)
        assert result == {""}

    def test_default_k(self):
        result = shingle("abcdef")
        # Default k=4: "abcd", "bcde", "cdef"
        assert result == {"abcd", "bcde", "cdef"}

    def test_single_char(self):
        result = shingle("x", k=4)
        assert result == {"x"}

    def test_repeated_chars(self):
        result = shingle("aaaa", k=2)
        assert result == {"aa"}


class TestJaccard:
    def test_identical_sets(self):
        s = {"a", "b", "c"}
        assert jaccard(s, s) == 1.0

    def test_disjoint_sets(self):
        assert jaccard({"a", "b"}, {"c", "d"}) == 0.0

    def test_partial_overlap(self):
        result = jaccard({"a", "b"}, {"b", "c"})
        assert result == pytest.approx(1 / 3)

    def test_empty_sets(self):
        assert jaccard(set(), set()) == 0.0

    def test_one_empty(self):
        assert jaccard({"a"}, set()) == 0.0

    def test_subset(self):
        result = jaccard({"a", "b"}, {"a", "b", "c"})
        assert result == pytest.approx(2 / 3)


class TestIsNearDuplicate:
    def test_identical_text(self):
        assert is_near_duplicate("hello world", "hello world") is True

    def test_completely_different(self):
        assert is_near_duplicate("hello world", "xyz 123 !@#") is False

    def test_near_threshold(self):
        # Same text with a single-char typo should still be similar
        text_a = "The quick brown fox jumps over the lazy dog"
        text_b = "The quick brown fox jumps over the lazy dogs"
        assert is_near_duplicate(text_a, text_b) is True

    def test_custom_threshold_strict(self):
        text_a = "The quick brown fox jumps over the lazy dog"
        text_b = "The quick brown fox leaps over the lazy dog"
        # With a very strict threshold, minor edit is enough to differ
        assert is_near_duplicate(text_a, text_b, threshold=0.99) is False

    def test_custom_threshold_loose(self):
        assert is_near_duplicate("abc", "xyz", threshold=0.0) is True

    def test_custom_k(self):
        # Verify k parameter is forwarded: different k values produce
        # different similarity scores for the same pair.
        sa = shingle("abcdef", k=2)
        sb = shingle("abcxef", k=2)
        sim_k2 = jaccard(sa, sb)

        sa8 = shingle("abcdef", k=4)
        sb8 = shingle("abcxef", k=4)
        sim_k4 = jaccard(sa8, sb8)

        # k=2 produces more shared shingles than k=4
        assert sim_k2 > sim_k4

    def test_empty_strings(self):
        # Both empty: shingle returns {""} for each, jaccard = 1.0
        assert is_near_duplicate("", "") is True
