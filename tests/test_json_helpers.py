"""Tests for pf_core.utils.json_recovery — JSON extraction and recovery."""

from __future__ import annotations

from pf_core.utils.json_recovery import (
    extract_json,
    extract_json_array,
    extract_json_object,
    recover_truncated_json,
    strip_markdown_fences,
)


class TestStripMarkdownFences:
    def test_no_fences(self):
        assert strip_markdown_fences('{"a": 1}') == '{"a": 1}'

    def test_json_fence(self):
        assert strip_markdown_fences('```json\n{"a": 1}\n```') == '{"a": 1}'

    def test_plain_fence(self):
        assert strip_markdown_fences('```\n{"a": 1}\n```') == '{"a": 1}'

    def test_preserves_inner_content(self):
        result = strip_markdown_fences('```json\nline1\nline2\n```')
        assert "line1" in result
        assert "line2" in result

    def test_strips_outer_whitespace(self):
        result = strip_markdown_fences('  ```json\n{"a": 1}\n```  ')
        assert result == '{"a": 1}'


class TestExtractJson:
    def test_plain_object(self):
        assert extract_json('{"key": "value"}') == {"key": "value"}

    def test_plain_array(self):
        assert extract_json('[1, 2, 3]') == [1, 2, 3]

    def test_with_fences(self):
        assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_with_trailing_text(self):
        result = extract_json('{"a": 1} and some commentary')
        assert result == {"a": 1}

    def test_with_leading_text(self):
        result = extract_json('Here is the result: {"a": 1}')
        assert result == {"a": 1}

    def test_invalid_json_returns_none(self):
        assert extract_json("not json at all") is None

    def test_empty_string(self):
        assert extract_json("") is None

    def test_nested_object(self):
        raw = '{"outer": {"inner": [1, 2]}}'
        result = extract_json(raw)
        assert result == {"outer": {"inner": [1, 2]}}


class TestExtractJsonArray:
    def test_plain_array(self):
        assert extract_json_array("[1, 2, 3]") == [1, 2, 3]

    def test_array_with_fences(self):
        assert extract_json_array('```json\n[{"a": 1}]\n```') == [{"a": 1}]

    def test_array_with_trailing_text(self):
        result = extract_json_array('[1, 2] and more text')
        assert result == [1, 2]

    def test_no_array_returns_none(self):
        assert extract_json_array('{"not": "array"}') is None

    def test_empty_string(self):
        assert extract_json_array("") is None

    def test_nested_arrays(self):
        result = extract_json_array("[[1, 2], [3, 4]]")
        assert result == [[1, 2], [3, 4]]

    def test_array_of_objects(self):
        raw = '[{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]'
        result = extract_json_array(raw)
        assert len(result) == 2
        assert result[0]["id"] == 1


class TestExtractJsonObject:
    def test_plain_object(self):
        assert extract_json_object('{"a": 1}') == {"a": 1}

    def test_object_with_fences(self):
        assert extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}

    def test_object_with_surrounding_text(self):
        result = extract_json_object('Result: {"a": 1} end')
        assert result == {"a": 1}

    def test_no_object_returns_none(self):
        assert extract_json_object("[1, 2, 3]") is None

    def test_empty_string(self):
        assert extract_json_object("") is None

    def test_nested_objects(self):
        raw = '{"outer": {"inner": "value"}}'
        result = extract_json_object(raw)
        assert result == {"outer": {"inner": "value"}}


class TestRecoverTruncatedJson:
    def test_truncated_array(self):
        raw = '[{"id": 1, "name": "a"}, {"id": 2, "name": "b"}, {"id": 3, "na'
        result = recover_truncated_json(raw)
        assert result is not None
        assert len(result) == 2
        assert result[0]["id"] == 1
        assert result[1]["id"] == 2

    def test_complete_array_returns_all(self):
        raw = '[{"id": 1}, {"id": 2}]'
        result = recover_truncated_json(raw)
        assert result is not None
        assert len(result) == 2

    def test_no_array_returns_none(self):
        assert recover_truncated_json("just text") is None

    def test_no_complete_objects_returns_none(self):
        assert recover_truncated_json("[{incomplete") is None

    def test_handles_strings_with_braces(self):
        raw = '[{"msg": "hello {world}"}, {"msg": "trunc'
        result = recover_truncated_json(raw)
        assert result is not None
        assert len(result) == 1
        assert result[0]["msg"] == "hello {world}"

    def test_handles_escaped_quotes(self):
        raw = r'[{"msg": "say \"hi\""}, {"msg": "trunc'
        result = recover_truncated_json(raw)
        assert result is not None
        assert len(result) == 1

    def test_with_fences(self):
        raw = '```json\n[{"a": 1}, {"b": 2}, {"c":'
        result = recover_truncated_json(raw)
        assert result is not None
        assert len(result) == 2

    def test_single_truncated_object(self):
        raw = '[{"id": 1, "na'
        result = recover_truncated_json(raw)
        assert result is None

    def test_trailing_comma_handled(self):
        raw = '[{"id": 1}, {"id": 2},'
        result = recover_truncated_json(raw)
        assert result is not None
        assert len(result) == 2
