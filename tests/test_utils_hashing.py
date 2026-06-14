"""Tests for pf_core.utils.hashing."""

from __future__ import annotations

import hashlib

from pf_core.utils.hashing import content_hash
from pf_core.utils.json import canonical_json


class TestContentHash:
    def test_str_matches_sha256_of_utf8(self):
        assert content_hash("hello") == hashlib.sha256(b"hello").hexdigest()

    def test_bytes_hashed_directly(self):
        assert content_hash(b"hello") == hashlib.sha256(b"hello").hexdigest()

    def test_str_and_bytes_agree(self):
        assert content_hash("café") == content_hash("café".encode("utf-8"))

    def test_dict_hashes_canonical_json(self):
        obj = {"b": 1, "a": 2}
        expected = hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()
        assert content_hash(obj) == expected

    def test_key_order_independent(self):
        assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})

    def test_different_content_differs(self):
        assert content_hash("a") != content_hash("b")

    def test_deterministic(self):
        assert content_hash({"x": [1, 2, 3]}) == content_hash({"x": [1, 2, 3]})

    def test_algo_override(self):
        assert content_hash("hello", algo="md5") == hashlib.md5(b"hello").hexdigest()

    def test_algo_changes_digest(self):
        assert content_hash("hello", algo="md5") != content_hash("hello", algo="sha256")
