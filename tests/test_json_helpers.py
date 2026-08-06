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


class TestExtractJsonArraySelection:
    """Which span wins when several brackets are present.

    A citation marker is itself valid JSON (``[1]`` parses), so "first span
    that parses" cannot discriminate — the rank has to look at content.
    """

    def test_citation_decoy_loses_to_object_payload(self):
        raw = 'Based on the criteria [1], here are the scores:\n[{"a":1}]'
        assert extract_json_array(raw) == [{"a": 1}]

    def test_multi_number_citation_decoy_loses_to_payload(self):
        raw = 'Per sources [1,2], output:\n[{"score": 5}]'
        assert extract_json_array(raw) == [{"score": 5}]

    def test_repeated_citations_before_payload(self):
        raw = 'Ref [1] and note [2]. Final: [{"a":1},{"b":2}]'
        assert extract_json_array(raw) == [{"a": 1}, {"b": 2}]

    def test_non_json_prose_bracket_does_not_block_extraction(self):
        assert extract_json_array('See [Table 1]. Result:\n[{"x":1}]') == [{"x": 1}]

    def test_range_and_placeholder_brackets_do_not_block_extraction(self):
        assert extract_json_array('Scores are on a [0-100] scale:\n[{"s": 88}]') == [{"s": 88}]
        assert extract_json_array("Values [x] then:\n[1, 2, 3]") == [1, 2, 3]

    def test_bracket_inside_quoted_prose_does_not_win(self):
        raw = 'Note: "see [1] for context"\n[{"a": 1}]'
        assert extract_json_array(raw) == [{"a": 1}]

    def test_array_at_offset_zero_wins_over_later_object_array(self):
        """Richness must not invert a payload that leads the response."""
        raw = '[1, 2, 3]\nEach maps to an object like [{"id": 1}]'
        assert extract_json_array(raw) == [1, 2, 3]

    def test_empty_array_at_offset_zero_beats_trailing_citation(self):
        assert extract_json_array("[]\nSee footnote [3].") == []

    def test_first_of_equally_ranked_arrays_still_wins(self):
        assert extract_json_array('[{"a":1}] then later [{"b":2}]') == [{"a": 1}]

    def test_malformed_outer_array_returns_none_so_json_repair_runs(self):
        """A trailing comma must not be salvaged as its own inner fragment.

        Descending into a failed span would return ``[1, 2]`` here — a
        well-formed fragment that pre-empts json_repair, which owns this input.
        """
        assert extract_json_array('[{"a": [1,2]}, {"b": 2},]') is None

    def test_malformed_outer_array_with_unescaped_inner_quote_returns_none(self):
        raw = '[{"quote": "he said "hi" loudly", "tags": ["a"]}]'
        assert extract_json_array(raw) is None

    def test_scan_gives_up_after_failure_cap(self):
        """Past the dead-end cap the scan gives up instead of reaching the payload."""
        assert extract_json_array("[x]" * 70 + '[{"a":1}]') is None


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


class TestExtractJsonObjectSelection:
    """The brace analogue. ``{}`` is the only prose token that is valid JSON,
    so the silent-wrong-data class is narrow here — but the false-null class
    is wide, and json_repair escalates those into invented structure."""

    def test_empty_brace_decoy_loses_to_real_object(self):
        assert extract_json_object('Template {} then real: {"a": 1}') == {"a": 1}

    def test_prose_placeholder_brace_does_not_block_extraction(self):
        assert extract_json_object('Use the {placeholder} pattern:\n{"a": 1}') == {"a": 1}
        assert extract_json_object('f-string {var} here\n{"ok": true}') == {"ok": True}

    def test_unquoted_key_prose_and_latex_braces_do_not_block_extraction(self):
        assert extract_json_object('Format is {name: value}. Result: {"a": 1}') == {"a": 1}
        assert extract_json_object('LaTeX \\frac{1}{2} then {"a": 1}') == {"a": 1}

    def test_malformed_outer_object_returns_none_so_json_repair_runs(self):
        assert extract_json_object('{"x": 1, "y": {"z": 2},}') is None


class TestExtractJsonSelection:
    def test_object_wins_over_prose_bracket_and_nested_array(self):
        """Containment: an array inside an object loses to its container."""
        raw = 'I found [3] events.\n{"events":[{"s":1}], "meta":"x"}'
        assert extract_json(raw) == {"events": [{"s": 1}], "meta": "x"}

    def test_array_payload_still_wins_when_no_object_present(self):
        assert extract_json('See [Table 1]. Result:\n[{"x":1}]') == [{"x": 1}]

    def test_array_of_objects_returns_the_array_not_its_first_element(self):
        """Behavior change, deliberate: previously returned the inner dict."""
        assert extract_json('[{"a":1}] trailing text') == [{"a": 1}]


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

    def test_anchors_on_first_unclosed_bracket_not_first_bracket(self):
        """A complete bracket earlier in the text is prose, not the truncation
        site. Anchoring on it made recovery return None on genuinely truncated
        input, so control fell through to json_repair and the operator got a
        fabricated result with no truncation warning."""
        raw = 'See [Table 1]. Result:\n[{"x":1}, {"y":2}, {"z'
        assert recover_truncated_json(raw) == [{"x": 1}, {"y": 2}]


class TestUnclosedSpanIsNotEntered:
    """An unclosed opener means truncation, so nothing after it is payload.

    Extraction must decline and let `recover_truncated_json` / `json_repair`
    own the input. The prior scan advanced one character past an unclosed
    opener and returned an inner field's value as the answer.
    """

    def test_truncated_records_with_a_list_field_decline(self):
        raw = '[{"id": 1, "tags": ["a","b"]}, {"id": 2, "tags": ["c"'
        assert extract_json_array(raw) is None

    def test_truncated_object_declines(self):
        assert extract_json_object('{"a": 1, "b": {"c": 2}, "d": [1,2]') is None

    def test_truncated_matrix_declines(self):
        assert extract_json('Rows:\n[' + "[1,2,3]," * 20) is None

    def test_unclosed_outer_with_well_formed_inner_declines(self):
        """The closed twin of this input is repaired; the unclosed one must
        not be answered with the inner fragment."""
        assert extract_json_object('{"events": [{"name": "test", "count": 3}]') is None

    def test_complete_span_before_a_trailing_unclosed_bracket_still_wins(self):
        assert extract_json_array('[{"a":1}] and then [oops') == [{"a": 1}]

    def test_prose_bracket_before_truncation_still_declines(self):
        assert extract_json_array('See [Table 1]. Result:\n[{"x":1}, {"y":2}, {"z') is None


class TestEmptyContainerAtOffsetZero:
    """The offset-0 rule requires a non-empty span. An empty container is
    valid JSON, so without that guard it won both the rank and the early exit."""

    def test_empty_object_loses_to_the_real_object(self):
        assert extract_json_object('{}\n\nActually: {"a": 1}') == {"a": 1}

    def test_empty_array_loses_to_the_object_payload(self):
        assert extract_json('[]\n\n{"answer": 42}') == {"answer": 42}

    def test_empty_array_loses_to_a_later_array_payload(self):
        assert extract_json_array('[]\n\nActually:\n[{"a": 1}]') == [{"a": 1}]

    def test_empty_array_still_wins_when_nothing_outranks_it(self):
        assert extract_json_array("[]\nSee footnote [3].") == []


class TestScanCost:
    def test_many_spans_stay_linear(self):
        """The containment filter was pairwise; 16k spans cost 7s of CPU.

        Generous bound — this guards the complexity class, not a target."""
        import time

        raw = "Here are the rows:\n[" + "[1,2,3]," * 16000
        start = time.perf_counter()
        extract_json(raw)
        assert time.perf_counter() - start < 2.0

    def test_nesting_filter_keeps_only_outermost_spans(self):
        from pf_core.utils.json_recovery import _drop_nested

        spans = [(0, 20, "outer"), (2, 8, "inner"), (10, 18, "inner2"), (25, 30, "sibling")]
        assert _drop_nested(spans) == [(0, 20, "outer"), (25, 30, "sibling")]
