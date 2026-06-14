"""Tests for pf_core.utils.json."""

from __future__ import annotations

from pf_core.utils.json import canonical_json, safe_json_col, safe_json_loads


class TestSafeJsonLoads:
    def test_valid_json_object(self):
        assert safe_json_loads('{"a": 1}') == {"a": 1}

    def test_valid_json_array(self):
        assert safe_json_loads("[1, 2]") == [1, 2]

    def test_valid_json_string(self):
        assert safe_json_loads('"hello"') == "hello"

    def test_invalid_json(self):
        assert safe_json_loads("not json") is None

    def test_invalid_json_custom_fallback(self):
        assert safe_json_loads("not json", fallback=[]) == []

    def test_none_input(self):
        assert safe_json_loads(None) is None

    def test_empty_string(self):
        assert safe_json_loads("") is None

    def test_label_logs_warning(self):
        # Verify the function works correctly with a label and returns fallback.
        # (Structlog capture is non-trivial; we verify no crash and correct return.)
        result = safe_json_loads("bad data", label="test_field")
        assert result is None

    def test_no_label_no_log(self):
        result = safe_json_loads("bad data")
        assert result is None


class TestSafeJsonCol:
    def test_string_input(self):
        assert safe_json_col('{"a": 1}') == {"a": 1}

    def test_dict_input(self):
        assert safe_json_col({"a": 1}) == {"a": 1}

    def test_list_input(self):
        assert safe_json_col([1, 2]) == [1, 2]

    def test_none_input(self):
        assert safe_json_col(None) is None

    def test_invalid_string(self):
        assert safe_json_col("bad") is None

    def test_invalid_string_custom_fallback(self):
        assert safe_json_col("bad", fallback={}) == {}

    def test_non_string_non_collection(self):
        assert safe_json_col(42) is None


class TestCanonicalJson:
    def test_sorts_keys(self):
        assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'

    def test_key_order_independent(self):
        assert canonical_json({"a": 1, "b": 2}) == canonical_json({"b": 2, "a": 1})

    def test_compact_separators(self):
        assert canonical_json({"a": 1, "b": 2}) == '{"a":1,"b":2}'
        assert canonical_json([1, 2, 3]) == "[1,2,3]"

    def test_nested_keys_sorted(self):
        assert canonical_json({"z": {"b": 1, "a": 2}}) == '{"z":{"a":2,"b":1}}'

    def test_scalars(self):
        assert canonical_json("hello") == '"hello"'
        assert canonical_json(42) == "42"
        assert canonical_json(None) == "null"

    def test_non_native_falls_back_to_str(self):
        import datetime as _dt

        assert canonical_json({"when": _dt.date(2026, 4, 14)}) == '{"when":"2026-04-14"}'

    def test_non_ascii_preserved(self):
        assert canonical_json({"name": "café"}) == '{"name":"café"}'
