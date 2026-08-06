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

    def test_truncated_recovery_warns_about_dropped_tail(self, caplog):
        """Truncation recovery drops the incomplete tail — that must be visible
        (a batch pipeline silently losing every long response's tail is the
        failure this warning exists to surface), not a DEBUG line."""
        import logging

        raw = '[{"a":1},{"b":2},{"c":3'
        with caplog.at_level(logging.WARNING, logger="pf_core.llm.parse"):
            parse_llm_json(raw, recover=True, expect="array")
        assert any(
            "parse_llm_json_recovered_truncated" in r.getMessage()
            for r in caplog.records
        )

    def test_truncated_array_no_recovery(self):
        raw = '[{"a":1},{"b":2},{"c":3'
        result = parse_llm_json(raw, recover=False, expect="array")
        assert result is None

    def test_on_truncation_raise_rejects_the_salvaged_prefix(self):
        """A caller that needs completeness must be able to fail loudly instead
        of receiving a short list that looks like a full answer."""
        raw = '[{"a":1},{"b":2},{"c":3'
        with pytest.raises(InvalidInputError, match="truncated"):
            parse_llm_json(raw, expect="array", on_truncation="raise")

    def test_on_truncation_defaults_to_warn(self):
        raw = '[{"a":1},{"b":2},{"c":3'
        assert parse_llm_json(raw, expect="array") == [{"a": 1}, {"b": 2}]
        assert parse_llm_json(raw, expect="array", on_truncation="warn") == [
            {"a": 1},
            {"b": 2},
        ]

    def test_on_truncation_raise_leaves_complete_responses_alone(self):
        raw = '[{"a":1},{"b":2}]'
        assert parse_llm_json(raw, expect="array", on_truncation="raise") == [
            {"a": 1},
            {"b": 2},
        ]

    def test_invalid_on_truncation_rejected(self):
        with pytest.raises(InvalidInputError, match="on_truncation"):
            parse_llm_json("[1]", on_truncation="ignore")

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

    # ── span selection composed with the repair chain ────────────────
    # Extraction picking the wrong span doesn't just return the wrong
    # value — it pre-empts json_repair, which would have been right.

    def test_prose_bracket_no_longer_fabricates_nested_wrapper(self):
        """Previously returned ``[['Table 1'], [{'x': 1}]]`` — json_repair
        conjuring a wrapper around the decoy and the payload."""
        raw = 'See [Table 1]. Result:\n[{"x":1}]'
        assert parse_llm_json(raw, expect="array") == [{"x": 1}]

    def test_citation_marker_no_longer_wins(self):
        raw = 'Based on the criteria [1], here are the scores:\n[{"a":1}]'
        assert parse_llm_json(raw, expect="array") == [{"a": 1}]

    def test_expect_any_prefers_container_by_content_not_position(self):
        raw = 'I found [3] events.\n{"events":[{"s":1}], "meta":"x"}'
        assert parse_llm_json(raw, expect="any") == {"events": [{"s": 1}], "meta": "x"}

    def test_expect_object_and_any_agree_on_placeholder_brace(self):
        """``expect="object"`` and ``expect="any"`` must resolve the same input
        identically."""
        raw = 'Use the {placeholder} pattern:\n{"a": 1}'
        assert parse_llm_json(raw, expect="object") == {"a": 1}
        assert parse_llm_json(raw, expect="any") == {"a": 1}

    def test_trailing_comma_still_repaired_by_json_repair(self):
        """Extraction must decline a malformed span rather than salvage a
        fragment of it — otherwise json_repair never runs."""
        raw = '[{"a": [1,2]}, {"b": 2},]'
        assert parse_llm_json(raw, expect="array") == [{"a": [1, 2]}, {"b": 2}]

    def test_unquoted_outer_key_with_well_formed_inner_still_repaired(self):
        """Unlike ``test_unquoted_keys`` above, the inner fragment here is
        well-formed — so a scan that descended into the failed outer span
        would return it and silently drop the wrapper."""
        raw = '{events: [{"name": "test", "count": 3}]}'
        assert parse_llm_json(raw, expect="object") == {
            "events": [{"name": "test", "count": 3}]
        }

    def test_truncation_after_prose_bracket_recovers_and_warns(self):
        raw = 'See [Table 1]. Result:\n[{"x":1}, {"y":2}, {"z'
        assert parse_llm_json(raw, expect="array") == [{"x": 1}, {"y": 2}]

    def test_truncated_records_with_inner_list_recover_and_warn(self, caplog):
        """Extraction answering from inside the truncated container skipped
        both recovery and repair, so the tail vanished with no warning."""
        import logging

        raw = '[{"id": 1, "tags": ["a","b"]}, {"id": 2, "tags": ["c"'
        with caplog.at_level(logging.WARNING, logger="pf_core.llm.parse"):
            result = parse_llm_json(raw, expect="array")
        assert result == [{"id": 1, "tags": ["a", "b"]}]
        assert any(
            "parse_llm_json_recovered_truncated" in r.getMessage()
            for r in caplog.records
        )

    def test_truncated_object_still_reaches_json_repair(self):
        raw = '```json\n{"a": 1, "b": {"c": 2}, "d": [1,2]'
        assert parse_llm_json(raw, expect="object") == {"a": 1, "b": {"c": 2}, "d": [1, 2]}

    def test_empty_container_at_offset_zero_does_not_win(self):
        assert parse_llm_json('[]\n\n{"answer": 42}', expect="any") == {"answer": 42}

    def test_empty_decoy_does_not_strand_a_truncated_payload(self):
        """An empty container satisfies step 3 on its own, which would leave
        the truncated real payload unreachable behind it."""
        raw = 'No prior events: []\nHere is the output:\n{"events":[{"summary":"real"}'
        assert parse_llm_json(raw, expect="any") == [{"summary": "real"}]

    def test_empty_decoy_agrees_across_expect_modes(self):
        raw = 'Result: {}\nActual:\n{"events":[{"summary":"real"}'
        assert parse_llm_json(raw, expect="any") == [{"summary": "real"}]
        assert parse_llm_json(raw, expect="object") == {"events": [{"summary": "real"}]}

    def test_a_genuinely_empty_container_is_still_returned(self):
        assert parse_llm_json("[]", expect="any") == []
        assert parse_llm_json("{}", expect="object") == {}
        assert parse_llm_json("Nothing found: []", expect="any") == []
