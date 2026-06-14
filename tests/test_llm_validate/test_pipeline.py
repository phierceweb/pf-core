"""parse_and_validate pipeline behavior + ValidationResult helpers."""

from __future__ import annotations

import json

import pytest

from pf_core.exceptions import PipelineNotRegisteredError
from pf_core.llm.validate import (
    ValidationResult,
    ValidationSignal,
    cross_field_validator,
    get_pipeline,
    parse_and_validate,
    register,
)

from .conftest import Doc, PydOk, payload


def test_pipeline_happy_path_returns_model_instance():
    register(agent_type="hp", shape=PydOk)
    res = parse_and_validate(
        json.dumps({"headline": "hi", "score": 1}), agent_type="hp",
    )
    assert res.ok is True
    assert isinstance(res.value, PydOk)
    assert res.value.headline == "hi"
    assert any(s.validator == "hp_shape" and s.passed for s in res.signals)


def test_pipeline_unregistered_agent_raises_by_default():
    with pytest.raises(PipelineNotRegisteredError) as exc_info:
        parse_and_validate('{"a":1}', agent_type="never", run_id=999)
    err = exc_info.value
    assert err.agent_type == "never"
    assert err.known_agents == []
    assert "never" in str(err)
    assert "missing_pipeline='fallback'" in str(err)


def test_pipeline_unregistered_agent_raise_lists_known_agents():
    register(agent_type="registered_a", shape=PydOk)
    register(agent_type="registered_b", shape=PydOk)
    with pytest.raises(PipelineNotRegisteredError) as exc_info:
        parse_and_validate('{"a":1}', agent_type="typo")
    err = exc_info.value
    assert err.agent_type == "typo"
    assert set(err.known_agents) == {"registered_a", "registered_b"}
    assert "registered_a" in str(err)


def test_pipeline_unregistered_agent_fallback_returns_signal():
    res = parse_and_validate(
        '{"a":1}', agent_type="never", run_id=999,
        missing_pipeline="fallback",
    )
    assert res.ok is False
    assert res.value is None
    assert len(res.signals) == 1
    sig = res.signals[0]
    assert sig.validator == "no_pipeline_registered"
    assert sig.severity == "error"
    assert sig.details["agent_type"] == "never"
    assert sig.details["known_agents"] == []


def test_pipeline_fallback_signal_lists_known_agents():
    register(agent_type="real", shape=PydOk)
    res = parse_and_validate(
        '{"a":1}', agent_type="typo",
        missing_pipeline="fallback",
    )
    sig = res.signals[0]
    assert sig.details["known_agents"] == ["real"]


def test_pipeline_unparseable_json_returns_parse_error():
    register(agent_type="hp", shape=PydOk)
    res = parse_and_validate("this is not json at all", agent_type="hp")
    assert res.ok is False
    assert res.value is None
    sig = next(s for s in res.signals if s.validator == "hp_parse")
    assert sig.severity == "error"
    assert sig.passed is False


def test_pipeline_shape_failure_skips_semantic_and_cross_field():
    @cross_field_validator("xf_skip")
    def _xf(parsed, *, context):  # noqa: ARG001
        raise AssertionError("should not run when shape fails")

    register(agent_type="sf", shape=PydOk,
             semantic=["url_sanity"], cross_field=["xf_skip"])
    res = parse_and_validate(json.dumps({"headline": "hi"}), agent_type="sf")
    assert res.ok is False
    validators = {s.validator for s in res.signals}
    assert "sf_shape" in validators
    assert "url_sanity" not in validators
    assert "xf_skip" not in validators


def test_pipeline_stages_shape_only_skips_other_stages():
    @cross_field_validator("xf_skip2")
    def _xf(parsed, *, context):  # noqa: ARG001
        raise AssertionError("cross_field should not run")

    register(agent_type="so", shape=PydOk,
             semantic=["url_sanity"], cross_field=["xf_skip2"])
    res = parse_and_validate(
        json.dumps({"headline": "hi", "score": 1}),
        agent_type="so", stages=("shape",),
    )
    validators = {s.validator for s in res.signals}
    assert "so_shape" in validators
    assert "url_sanity" not in validators
    assert "xf_skip2" not in validators


def test_pipeline_stages_shape_semantic_skips_cross_field():
    @cross_field_validator("xf_skip3")
    def _xf(parsed, *, context):  # noqa: ARG001
        raise AssertionError("cross_field should not run")

    register(agent_type="ss", shape=Doc,
             semantic=["url_sanity"], cross_field=["xf_skip3"])
    res = parse_and_validate(
        payload(headline="x"), agent_type="ss",
        stages=("shape", "semantic"),
    )
    validators = {s.validator for s in res.signals}
    assert "url_sanity" in validators
    assert "xf_skip3" not in validators


def test_pipeline_semantic_exception_recorded_not_propagated():
    register(agent_type="exc", shape=Doc, semantic=["url_sanity"])
    pipe = get_pipeline("exc")

    def _boom(parsed, *, context):  # noqa: ARG001
        raise RuntimeError("boom")

    _boom.name = "boomer"
    pipe.semantic = [_boom]

    res = parse_and_validate(payload(headline="x"), agent_type="exc")
    assert res.ok is False
    sig = next(s for s in res.signals if s.validator == "boomer")
    assert sig.severity == "error"
    assert sig.passed is False
    assert "boom" in sig.details["exception"]


def test_pipeline_cross_field_exception_recorded_not_propagated():
    @cross_field_validator("xf_explode")
    def _xf(parsed, *, context):  # noqa: ARG001
        raise RuntimeError("kaboom")

    register(agent_type="cfx", shape=Doc, cross_field=["xf_explode"])
    res = parse_and_validate(payload(headline="x"), agent_type="cfx")
    assert res.ok is False
    sig = next(s for s in res.signals if s.validator == "xf_explode")
    assert sig.severity == "error"
    assert "kaboom" in sig.details["exception"]


def test_pipeline_run_id_none_runs_in_memory_no_db():
    """When run_id is None the pipeline must not touch any DB at all.

    Verified by simply not setting up tracking tables — if the pipeline
    tried to write, the missing tables would surface as an error.
    """
    register(agent_type="mem", shape=PydOk, semantic=["url_sanity"])
    res = parse_and_validate(
        json.dumps({"headline": "hi", "score": 1}),
        agent_type="mem", run_id=None,
    )
    assert res.ok is True
    assert isinstance(res.value, PydOk)
    assert len(res.signals) >= 2  # shape + url_sanity


def test_validation_result_failures_and_warnings_split():
    res = ValidationResult(
        ok=False, value=None,
        signals=[
            ValidationSignal("a", "error", passed=False),
            ValidationSignal("b", "warn", passed=False),
            ValidationSignal("c", "info", passed=False),
            ValidationSignal("d", "error", passed=True),
        ],
    )
    assert [f.validator for f in res.failures] == ["a"]
    assert {w.validator for w in res.warnings} == {"b", "c"}
