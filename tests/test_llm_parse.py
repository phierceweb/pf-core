"""Tests for pf_core.llm.parse."""

from __future__ import annotations

import pytest

from pf_core.exceptions import InvalidInputError
from pf_core.llm.parse import parse_llm_json


class TestParseLlmJson:
    def test_clean_json_object(self):
        result = parse_llm_json('{"key": "value"}')
        assert result == {"key": "value"}

    def test_clean_json_array(self):
        result = parse_llm_json("[1, 2, 3]")
        assert result == [1, 2, 3]

    def test_markdown_fenced_json(self):
        raw = '```json\n{"key": "val"}\n```'
        result = parse_llm_json(raw)
        assert result == {"key": "val"}

    def test_json_with_trailing_text(self):
        raw = '[{"a":1}]\nHere is the explanation...'
        result = parse_llm_json(raw)
        assert result == [{"a": 1}]

    def test_expect_array_with_array(self):
        result = parse_llm_json("[1, 2, 3]", expect="array")
        assert result == [1, 2, 3]

    def test_expect_array_with_object(self):
        result = parse_llm_json('{"a":1}', expect="array")
        assert result is None

    def test_expect_object_with_object(self):
        result = parse_llm_json('{"a":1}', expect="object")
        assert result == {"a": 1}

    def test_expect_object_with_array(self):
        result = parse_llm_json("[1]", expect="object")
        assert result is None

    def test_truncated_array_recovery(self):
        raw = '[{"a":1},{"b":2},{"c":3'
        result = parse_llm_json(raw, recover=True, expect="array")
        assert result == [{"a": 1}, {"b": 2}]

    def test_truncated_array_no_recovery(self):
        raw = '[{"a":1},{"b":2},{"c":3'
        result = parse_llm_json(raw, recover=False, expect="array")
        assert result is None

    def test_strict_raises(self):
        with pytest.raises(InvalidInputError, match="Failed to parse JSON"):
            parse_llm_json("not json at all!!!", strict=True)

    def test_strict_false_returns_none(self):
        result = parse_llm_json("not json at all!!!", strict=False)
        assert result is None

    def test_empty_string(self):
        result = parse_llm_json("")
        assert result is None

    def test_none_handling(self):
        result = parse_llm_json("")
        assert result is None

    def test_json_in_prose(self):
        raw = 'The answer is [{"name":"test"}] as shown above.'
        result = parse_llm_json(raw)
        assert result == [{"name": "test"}]

    # ── json_repair fallback (Step 5) ────────────────────────────────
    # These exercise the permissive LLM-JSON repair path. Stdlib
    # json.loads rejects each of these inputs; extract_json_* can't
    # find a balanced substring either; recover_truncated_json doesn't
    # apply. json_repair is what carries them across the line.

    def test_unescaped_inner_double_quotes_in_string(self):
        """Model embedded quoted dialogue with unescaped inner ".

        This is a real-world failure mode that drove adding
        json_repair to the parse chain. Stdlib json.loads trips on the
        first inner " treating it as the string terminator.
        """
        raw = '{"what_happened": "She said, "Hello.""}'
        result = parse_llm_json(raw, expect="object")
        assert isinstance(result, dict)
        assert "what_happened" in result
        # Exact character preservation isn't what json_repair guarantees,
        # but the value must contain the quoted content.
        assert "Hello" in result["what_happened"]
        assert "said" in result["what_happened"]

    def test_backslash_escaped_single_quote(self):
        """``\\'`` is invalid in JSON but common in LLM output."""
        raw = r'{"quote": "it\'s a trap"}'
        result = parse_llm_json(raw, expect="object")
        assert isinstance(result, dict)
        assert "trap" in result["quote"]

    def test_trailing_comma_in_array(self):
        raw = '[1, 2, 3,]'
        result = parse_llm_json(raw, expect="array")
        assert result == [1, 2, 3]

    def test_unquoted_keys(self):
        raw = '{events: [{name: "test", count: 3}]}'
        result = parse_llm_json(raw, expect="object")
        assert isinstance(result, dict)
        assert result.get("events", [{}])[0].get("count") == 3

    def test_repair_disabled_by_recover_false(self):
        """``recover=False`` skips json_repair along with truncation recovery."""
        raw = '{"quote": "she said, "hi""}'
        result = parse_llm_json(raw, recover=False, expect="object")
        assert result is None
