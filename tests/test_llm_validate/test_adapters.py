"""Shape-validator adapters: PydanticValidator and JsonSchemaValidator."""

from __future__ import annotations

import pytest

from pf_core.exceptions import ConfigurationError
from pf_core.llm.validate import JsonSchemaValidator, PydanticValidator

from .conftest import PydForbid, PydOk


def test_pydantic_validator_happy_path():
    pv = PydanticValidator(PydOk)
    instance, sig = pv.validate_shape({"headline": "hi", "score": 5}, agent_type="t")
    assert isinstance(instance, PydOk)
    assert instance.headline == "hi"
    assert sig.passed is True
    assert sig.severity == "error"
    assert sig.details is None
    assert sig.validator == "t_shape"


def test_pydantic_validator_missing_required_field():
    pv = PydanticValidator(PydOk)
    instance, sig = pv.validate_shape({"headline": "hi"}, agent_type="t")
    assert instance is None
    assert sig.passed is False
    assert "errors" in sig.details
    assert any(e["loc"] == ("score",) for e in sig.details["errors"])


def test_pydantic_validator_wrong_type():
    pv = PydanticValidator(PydOk)
    instance, sig = pv.validate_shape(
        {"headline": "hi", "score": "not-an-int"}, agent_type="t"
    )
    assert instance is None
    assert sig.passed is False


def test_pydantic_validator_extra_forbid_rejects_extras():
    pv = PydanticValidator(PydForbid)
    instance, sig = pv.validate_shape({"a": "x", "b": "extra!"}, agent_type="t")
    assert instance is None
    assert sig.passed is False


def test_jsonschema_validator_raises_without_extra(monkeypatch):
    """When ``jsonschema`` isn't importable, constructor must fail loud
    with a remediation message naming the extra. Simulate the no-extra
    install state by setting ``sys.modules['jsonschema'] = None`` —
    Python's import machinery treats that as "import already failed"
    and raises ``ImportError`` on the next ``from jsonschema import …``.
    Lets the test exercise the branch in any install matrix instead of
    skipping when the extra is present."""
    import sys

    monkeypatch.setitem(sys.modules, "jsonschema", None)
    with pytest.raises(
        ConfigurationError, match=r"pip install pf-core\[jsonschema\]"
    ):
        JsonSchemaValidator({"type": "object"})
