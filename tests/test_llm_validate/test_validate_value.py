"""validate_value — the pipeline on an already-parsed (non-JSON-sourced) value."""

from __future__ import annotations

import pytest

from pf_core.exceptions import PipelineNotRegisteredError
from pf_core.llm.tracking import LlmRunRepo
from pf_core.llm.tracking import schema as ts
from pf_core.llm.validate import (
    cross_field_validator,
    register,
    validate_value,
)

from .conftest import Doc, PydOk


def test_validate_value_happy_path_returns_model_instance():
    register(agent_type="vv", shape=PydOk)
    res = validate_value({"headline": "hi", "score": 1}, agent_type="vv")
    assert res.ok is True
    assert isinstance(res.value, PydOk)
    assert res.value.headline == "hi"
    assert any(s.validator == "vv_shape" and s.passed for s in res.signals)


def test_validate_value_shape_failure_returns_fail_signal():
    register(agent_type="vv_bad", shape=PydOk)
    res = validate_value({"headline": "hi"}, agent_type="vv_bad")  # missing score
    assert res.ok is False
    assert res.value is None
    sig = next(s for s in res.signals if s.validator == "vv_bad_shape")
    assert sig.passed is False
    assert sig.severity == "error"


def test_validate_value_runs_semantic_and_cross_field():
    @cross_field_validator("vv_xf")
    def _xf(parsed, *, context):  # noqa: ARG001
        from pf_core.llm.validate import ValidationSignal

        return ValidationSignal("vv_xf", "info", passed=True)

    register(agent_type="vv_full", shape=Doc, semantic=["url_sanity"], cross_field=["vv_xf"])
    res = validate_value({"headline": "x"}, agent_type="vv_full")
    validators = {s.validator for s in res.signals}
    assert {"vv_full_shape", "url_sanity", "vv_xf"} <= validators


def test_validate_value_stages_subsetting_skips_other_stages():
    @cross_field_validator("vv_xf_skip")
    def _xf(parsed, *, context):  # noqa: ARG001
        raise AssertionError("cross_field should not run")

    register(agent_type="vv_sub", shape=Doc, semantic=["url_sanity"], cross_field=["vv_xf_skip"])
    res = validate_value({"headline": "x"}, agent_type="vv_sub", stages=("shape",))
    validators = {s.validator for s in res.signals}
    assert "vv_sub_shape" in validators
    assert "url_sanity" not in validators
    assert "vv_xf_skip" not in validators


def test_validate_value_missing_pipeline_raises_by_default():
    with pytest.raises(PipelineNotRegisteredError) as exc_info:
        validate_value({"a": 1}, agent_type="never_vv")
    assert exc_info.value.agent_type == "never_vv"


def test_validate_value_missing_pipeline_fallback_returns_signal():
    res = validate_value({"a": 1}, agent_type="never_vv", missing_pipeline="fallback")
    assert res.ok is False
    assert res.value is None
    assert len(res.signals) == 1
    sig = res.signals[0]
    assert sig.validator == "no_pipeline_registered"
    assert sig.severity == "error"
    assert sig.details["agent_type"] == "never_vv"


def test_validate_value_writes_signals_and_tag_to_db(tracking_db):
    register(agent_type="vv_db", shape=PydOk, semantic=["url_sanity"], schema_version=4)
    run_id = LlmRunRepo().record(agent_type="vv_db", model="claude-opus-4-7")

    res = validate_value(
        {"headline": "hi", "score": 1},
        agent_type="vv_db",
        run_id=run_id,
    )
    assert res.ok is True

    with tracking_db.connect() as conn:
        rows = (
            conn.execute(
                ts.llm_run_validations.select().where(ts.llm_run_validations.c.llm_run_id == run_id)
            )
            .mappings()
            .fetchall()
        )
        tags = (
            conn.execute(ts.llm_run_tags.select().where(ts.llm_run_tags.c.llm_run_id == run_id))
            .mappings()
            .fetchall()
        )

    by_validator = {r["validator"]: r for r in rows}
    assert "vv_db_shape" in by_validator
    assert "url_sanity" in by_validator
    assert by_validator["vv_db_shape"]["passed"] is True
    assert "schema:vv_db_v4" in {t["tag"] for t in tags}


def test_validate_value_run_id_none_runs_in_memory_no_db():
    register(agent_type="vv_mem", shape=PydOk)
    res = validate_value({"headline": "hi", "score": 1}, agent_type="vv_mem", run_id=None)
    assert res.ok is True
    assert isinstance(res.value, PydOk)
