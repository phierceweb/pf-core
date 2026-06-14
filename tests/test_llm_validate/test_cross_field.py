"""Cross-field decorator + pipeline integration."""

from __future__ import annotations

from pf_core.llm.validate import (
    ValidationSignal,
    cross_field_validator,
    get_cross_field_validator,
    list_cross_field_validators,
    parse_and_validate,
    register,
)

from .conftest import Doc, payload


def test_cross_field_decorator_registers():
    @cross_field_validator("xf_pass")
    def _xf(parsed, *, context):  # noqa: ARG001
        return ValidationSignal("xf_pass", "info", passed=True)

    fn = get_cross_field_validator("xf_pass")
    assert fn is _xf
    assert fn.name == "xf_pass"
    assert "xf_pass" in list_cross_field_validators()


def test_cross_field_passing_signal_keeps_ok_true():
    @cross_field_validator("xf_ok")
    def _xf(parsed, *, context):  # noqa: ARG001
        return ValidationSignal("xf_ok", "info", passed=True)

    register(agent_type="cf", shape=Doc, cross_field=["xf_ok"])
    res = parse_and_validate(payload(headline="x"), agent_type="cf")
    assert res.ok is True
    assert any(s.validator == "xf_ok" and s.passed for s in res.signals)


def test_cross_field_error_signal_makes_ok_false():
    @cross_field_validator("xf_err")
    def _xf(parsed, *, context):  # noqa: ARG001
        return ValidationSignal("xf_err", "error", passed=False, details={"why": "no"})

    register(agent_type="cf", shape=Doc, cross_field=["xf_err"])
    res = parse_and_validate(payload(headline="x"), agent_type="cf")
    assert res.ok is False
    assert any(f.validator == "xf_err" for f in res.failures)


def test_cross_field_receives_validation_context():
    seen: dict = {}

    @cross_field_validator("xf_ctx")
    def _xf(parsed, *, context):  # noqa: ARG001
        seen.update(context)
        return ValidationSignal("xf_ctx", "info", passed=True)

    register(agent_type="cf", shape=Doc, cross_field=["xf_ctx"])
    parse_and_validate(
        payload(headline="x"), agent_type="cf",
        validation_context={"report_id": 42, "guideline": "abc"},
    )
    assert seen == {"report_id": 42, "guideline": "abc"}


def test_cross_field_re_register_overwrites():
    @cross_field_validator("xf_overwrite")
    def _first(parsed, *, context):  # noqa: ARG001
        return ValidationSignal("xf_overwrite", "info", passed=True)

    @cross_field_validator("xf_overwrite")
    def _second(parsed, *, context):  # noqa: ARG001
        return ValidationSignal("xf_overwrite", "error", passed=False)

    assert get_cross_field_validator("xf_overwrite") is _second
