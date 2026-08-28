"""Tests for the authoritative truncation signal on ``parse_llm_json``.

The signal is provider-reported (``usage["finish_reason"]``), never inferred
from the response text. The regression guards below are payloads that
text-based detection wrongly flagged as truncated; with ``truncated=None``
they must parse.
"""

from __future__ import annotations

import logging

import pytest

from pf_core.exceptions import InvalidInputError
from pf_core.llm.parse import parse_llm_json, truncated_from_usage

# Complete payloads whose text *looks* truncated: odd quote parity from an
# unescaped inner quote, or trailing commentary containing a stray brace.
COMPLETE_BUT_TRICKY = [
    ('{"size": "5" long", "ok": true}', {"size": '5" long', "ok": True}),
    (
        '{"size": "5" long", "ok": true}\n\nLet me know if you need anything else.',
        {"size": '5" long', "ok": True},
    ),
    ("{}\n\nNothing here {", {}),
    ("[]\n\nNo matches found. Use { to start a block", ["to start a block"]),
    ('{"a": 1}\n\nNote: use { for blocks', {"a": 1}),
]

# Truncated before anything closed: json_repair seals these and returns a
# plausible-looking partial record with no signal of its own.
NOTHING_CLOSED_YET = [
    ('[{"id": 1, "name": "Alpha", "body": "got cut o', {}),
    ('{"id": 1, "body": "got cut o', {"expect": "object"}),
    ('{"total_usd": 12345', {"expect": "object"}),
    ('[{"id":1,"name":"A"}, {"id":2,"body":"cut o', {}),
]

# Varied corpus for the byte-identical check — clean, fenced, mixed-prose,
# malformed, truncated, and unparseable shapes.
CORPUS = [
    '{"key": "value"}',
    "[1, 2, 3]",
    '```json\n{"key": "val"}\n```',
    '[{"a":1}]\nHere is the explanation...',
    'Result: {}\nActual:\n{"events":[{"summary":"real"}',
    '{"a": 1, "b": {"c": 2}, "d": [1,2]',
    "not json at all",
    "",
    "[]",
    "{}",
    *[raw for raw, _ in COMPLETE_BUT_TRICKY],
    *[raw for raw, _ in NOTHING_CLOSED_YET],
]


class TestTruncatedFromUsage:
    @pytest.mark.parametrize(
        "reason",
        [
            "length",  # OpenAI / OpenRouter
            "max_tokens",  # Anthropic SDK, Claude Code CLI
            "model_context_window_exceeded",  # Anthropic SDK, Claude Code CLI
            "MAX_TOKENS",  # upper-cased provider passthrough
        ],
    )
    def test_truncation_vocabulary(self, reason):
        assert truncated_from_usage({"finish_reason": reason}) is True

    @pytest.mark.parametrize(
        "reason",
        ["stop", "end_turn", "stop_sequence", "tool_use", "tool_calls", "eos", "END_TURN"],
    )
    def test_completion_vocabulary(self, reason):
        assert truncated_from_usage({"finish_reason": reason}) is False

    @pytest.mark.parametrize("reason", ["content_filter", "error", "", "banana"])
    def test_unrecognised_reason_is_unknown_not_complete(self, reason):
        """An unknown token means UNKNOWN. Answering False would assert the
        response is complete on a reason we have never seen."""
        assert truncated_from_usage({"finish_reason": reason}) is None

    def test_absent_and_none_are_unknown(self):
        assert truncated_from_usage({}) is None
        assert truncated_from_usage({"finish_reason": None}) is None
        assert truncated_from_usage(None) is None


class TestNoInferenceFromText:
    """Payloads the two rejected text-inference attempts broke."""

    @pytest.mark.parametrize(("raw", "expected"), COMPLETE_BUT_TRICKY)
    @pytest.mark.parametrize("on_truncation", ["raise", "warn"])
    def test_complete_payload_never_raises(self, raw, expected, on_truncation):
        assert parse_llm_json(raw, on_truncation=on_truncation) == expected

    @pytest.mark.parametrize(("raw", "expected"), COMPLETE_BUT_TRICKY)
    def test_complete_payload_with_known_complete_flag(self, raw, expected):
        assert parse_llm_json(raw, on_truncation="raise", truncated=False) == expected


class TestAuthoritativeTruncation:
    @pytest.mark.parametrize(("raw", "kwargs"), NOTHING_CLOSED_YET)
    def test_raises_when_provider_reported_truncation(self, raw, kwargs):
        with pytest.raises(InvalidInputError, match="truncated"):
            parse_llm_json(raw, on_truncation="raise", truncated=True, **kwargs)

    def test_raises_even_when_the_payload_parses_cleanly(self):
        """The cap can land exactly on a closing brace — still incomplete."""
        with pytest.raises(InvalidInputError, match="truncated"):
            parse_llm_json('{"a": 1}', on_truncation="raise", truncated=True)

    @pytest.mark.parametrize(("raw", "kwargs"), NOTHING_CLOSED_YET)
    def test_warn_returns_the_value_and_logs(self, raw, kwargs, caplog):
        with caplog.at_level(logging.WARNING, logger="pf_core.llm.parse"):
            result = parse_llm_json(raw, on_truncation="warn", truncated=True, **kwargs)
        assert result is not None
        assert sum("parse_llm_json_recovered_truncated" in r.getMessage() for r in caplog.records)

    def test_warn_logs_once_when_step_4_also_recovered(self, caplog):
        raw = '[{"id":1,"name":"A"}, {"id":2,"body":"cut o'
        with caplog.at_level(logging.WARNING, logger="pf_core.llm.parse"):
            result = parse_llm_json(raw, on_truncation="warn", truncated=True)
        assert result == [{"id": 1, "name": "A"}]
        hits = [r for r in caplog.records if "parse_llm_json_recovered_truncated" in r.getMessage()]
        assert len(hits) == 1

    def test_strict_still_governs_an_unparseable_truncated_response(self):
        with pytest.raises(InvalidInputError, match="Failed to parse"):
            parse_llm_json("not json", strict=True, on_truncation="warn", truncated=True)


# (CORPUS index, expect, on_truncation) -> ("ok", value) | ("raised", message),
# captured from the tree before the truncation flag existed. An unknown flag
# must reproduce every row exactly.
BASELINE = [
    (0, "any", "warn", ("ok", {"key": "value"})),
    (0, "any", "raise", ("ok", {"key": "value"})),
    (0, "array", "warn", ("ok", None)),
    (0, "array", "raise", ("ok", None)),
    (0, "object", "warn", ("ok", {"key": "value"})),
    (0, "object", "raise", ("ok", {"key": "value"})),
    (1, "any", "warn", ("ok", [1, 2, 3])),
    (1, "any", "raise", ("ok", [1, 2, 3])),
    (1, "array", "warn", ("ok", [1, 2, 3])),
    (1, "array", "raise", ("ok", [1, 2, 3])),
    (1, "object", "warn", ("ok", None)),
    (1, "object", "raise", ("ok", None)),
    (2, "any", "warn", ("ok", {"key": "val"})),
    (2, "any", "raise", ("ok", {"key": "val"})),
    (2, "array", "warn", ("ok", None)),
    (2, "array", "raise", ("ok", None)),
    (2, "object", "warn", ("ok", {"key": "val"})),
    (2, "object", "raise", ("ok", {"key": "val"})),
    (3, "any", "warn", ("ok", [{"a": 1}])),
    (3, "any", "raise", ("ok", [{"a": 1}])),
    (3, "array", "warn", ("ok", [{"a": 1}])),
    (3, "array", "raise", ("ok", [{"a": 1}])),
    (3, "object", "warn", ("ok", {"a": 1})),
    (3, "object", "raise", ("ok", {"a": 1})),
    (4, "any", "warn", ("ok", [{"summary": "real"}])),
    (
        4,
        "any",
        "raise",
        ("raised", "LLM response was truncated; recovered 1 complete item(s) and dropped the tail"),
    ),
    (4, "array", "warn", ("ok", [{"summary": "real"}])),
    (
        4,
        "array",
        "raise",
        ("raised", "LLM response was truncated; recovered 1 complete item(s) and dropped the tail"),
    ),
    (4, "object", "warn", ("ok", {"events": [{"summary": "real"}]})),
    (4, "object", "raise", ("ok", {"events": [{"summary": "real"}]})),
    (5, "any", "warn", ("ok", {"a": 1, "b": {"c": 2}, "d": [1, 2]})),
    (5, "any", "raise", ("ok", {"a": 1, "b": {"c": 2}, "d": [1, 2]})),
    (5, "array", "warn", ("ok", None)),
    (5, "array", "raise", ("ok", None)),
    (5, "object", "warn", ("ok", {"a": 1, "b": {"c": 2}, "d": [1, 2]})),
    (5, "object", "raise", ("ok", {"a": 1, "b": {"c": 2}, "d": [1, 2]})),
    (6, "any", "warn", ("ok", None)),
    (6, "any", "raise", ("ok", None)),
    (6, "array", "warn", ("ok", None)),
    (6, "array", "raise", ("ok", None)),
    (6, "object", "warn", ("ok", None)),
    (6, "object", "raise", ("ok", None)),
    (7, "any", "warn", ("ok", None)),
    (7, "any", "raise", ("ok", None)),
    (7, "array", "warn", ("ok", None)),
    (7, "array", "raise", ("ok", None)),
    (7, "object", "warn", ("ok", None)),
    (7, "object", "raise", ("ok", None)),
    (8, "any", "warn", ("ok", [])),
    (8, "any", "raise", ("ok", [])),
    (8, "array", "warn", ("ok", [])),
    (8, "array", "raise", ("ok", [])),
    (8, "object", "warn", ("ok", None)),
    (8, "object", "raise", ("ok", None)),
    (9, "any", "warn", ("ok", {})),
    (9, "any", "raise", ("ok", {})),
    (9, "array", "warn", ("ok", None)),
    (9, "array", "raise", ("ok", None)),
    (9, "object", "warn", ("ok", {})),
    (9, "object", "raise", ("ok", {})),
    (10, "any", "warn", ("ok", {"size": '5" long', "ok": True})),
    (10, "any", "raise", ("ok", {"size": '5" long', "ok": True})),
    (10, "array", "warn", ("ok", None)),
    (10, "array", "raise", ("ok", None)),
    (10, "object", "warn", ("ok", {"size": '5" long', "ok": True})),
    (10, "object", "raise", ("ok", {"size": '5" long', "ok": True})),
    (11, "any", "warn", ("ok", {"size": '5" long', "ok": True})),
    (11, "any", "raise", ("ok", {"size": '5" long', "ok": True})),
    (11, "array", "warn", ("ok", None)),
    (11, "array", "raise", ("ok", None)),
    (11, "object", "warn", ("ok", {"size": '5" long', "ok": True})),
    (11, "object", "raise", ("ok", {"size": '5" long', "ok": True})),
    (12, "any", "warn", ("ok", {})),
    (12, "any", "raise", ("ok", {})),
    (12, "array", "warn", ("ok", None)),
    (12, "array", "raise", ("ok", None)),
    (12, "object", "warn", ("ok", {})),
    (12, "object", "raise", ("ok", {})),
    (13, "any", "warn", ("ok", ["to start a block"])),
    (13, "any", "raise", ("ok", ["to start a block"])),
    (13, "array", "warn", ("ok", ["to start a block"])),
    (13, "array", "raise", ("ok", ["to start a block"])),
    (13, "object", "warn", ("ok", None)),
    (13, "object", "raise", ("ok", None)),
    (14, "any", "warn", ("ok", {"a": 1})),
    (14, "any", "raise", ("ok", {"a": 1})),
    (14, "array", "warn", ("ok", [{"a": 1}, ["for blocks"]])),
    (14, "array", "raise", ("ok", [{"a": 1}, ["for blocks"]])),
    (14, "object", "warn", ("ok", {"a": 1})),
    (14, "object", "raise", ("ok", {"a": 1})),
    (15, "any", "warn", ("ok", [{"id": 1, "name": "Alpha", "body": "got cut o"}])),
    (15, "any", "raise", ("ok", [{"id": 1, "name": "Alpha", "body": "got cut o"}])),
    (15, "array", "warn", ("ok", [{"id": 1, "name": "Alpha", "body": "got cut o"}])),
    (15, "array", "raise", ("ok", [{"id": 1, "name": "Alpha", "body": "got cut o"}])),
    (15, "object", "warn", ("ok", None)),
    (15, "object", "raise", ("ok", None)),
    (16, "any", "warn", ("ok", {"id": 1, "body": "got cut o"})),
    (16, "any", "raise", ("ok", {"id": 1, "body": "got cut o"})),
    (16, "array", "warn", ("ok", None)),
    (16, "array", "raise", ("ok", None)),
    (16, "object", "warn", ("ok", {"id": 1, "body": "got cut o"})),
    (16, "object", "raise", ("ok", {"id": 1, "body": "got cut o"})),
    (17, "any", "warn", ("ok", {"total_usd": 12345})),
    (17, "any", "raise", ("ok", {"total_usd": 12345})),
    (17, "array", "warn", ("ok", None)),
    (17, "array", "raise", ("ok", None)),
    (17, "object", "warn", ("ok", {"total_usd": 12345})),
    (17, "object", "raise", ("ok", {"total_usd": 12345})),
    (18, "any", "warn", ("ok", [{"id": 1, "name": "A"}])),
    (
        18,
        "any",
        "raise",
        ("raised", "LLM response was truncated; recovered 1 complete item(s) and dropped the tail"),
    ),
    (18, "array", "warn", ("ok", [{"id": 1, "name": "A"}])),
    (
        18,
        "array",
        "raise",
        ("raised", "LLM response was truncated; recovered 1 complete item(s) and dropped the tail"),
    ),
    (18, "object", "warn", ("ok", None)),
    (18, "object", "raise", ("ok", None)),
]


class TestUnknownIsUnchanged:
    @pytest.mark.parametrize(("index", "expect", "on_truncation", "expected"), BASELINE)
    def test_none_reproduces_pre_flag_behaviour(self, index, expect, on_truncation, expected):
        for truncated in (None, ...):
            kwargs = {} if truncated is ... else {"truncated": truncated}
            try:
                outcome = (
                    "ok",
                    parse_llm_json(
                        CORPUS[index], expect=expect, on_truncation=on_truncation, **kwargs
                    ),
                )
            except InvalidInputError as exc:
                outcome = ("raised", str(exc))
            assert outcome == expected
