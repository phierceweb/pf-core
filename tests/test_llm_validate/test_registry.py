"""Registry: register, get_pipeline, list_agent_types."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from pf_core.exceptions import ConfigurationError
from pf_core.llm.validate import (
    PydanticValidator,
    get_pipeline,
    has_pipeline,
    list_agent_types,
    register,
)

from .conftest import RegSimple


def test_register_with_pydantic_model_auto_wraps():
    register(agent_type="reg_a", shape=RegSimple)
    pipe = get_pipeline("reg_a")
    assert pipe is not None
    assert isinstance(pipe.shape, PydanticValidator)
    assert pipe.shape.model is RegSimple


def test_register_with_prebuilt_pydantic_validator_passes_through():
    pv = PydanticValidator(RegSimple)
    register(agent_type="reg_b", shape=pv)
    assert get_pipeline("reg_b").shape is pv


def test_register_last_wins():
    register(agent_type="dup", shape=RegSimple, schema_version=1)

    class _RegOther(BaseModel):
        other: int

    register(agent_type="dup", shape=_RegOther, schema_version=2)
    pipe = get_pipeline("dup")
    assert pipe.shape.model is _RegOther
    assert pipe.schema_version == 2


def test_list_agent_types_returns_sorted():
    for slug in ("zebra", "alpha", "mango"):
        register(agent_type=slug, shape=RegSimple)
    assert list_agent_types() == ["alpha", "mango", "zebra"]


def test_register_unknown_semantic_raises_configuration_error():
    with pytest.raises(ConfigurationError, match="unknown semantic validator"):
        register(agent_type="x", shape=RegSimple, semantic=["does_not_exist"])


def test_register_unknown_cross_field_raises_key_error():
    with pytest.raises(KeyError, match="not registered"):
        register(agent_type="x", shape=RegSimple, cross_field=["nope"])


def test_get_pipeline_returns_none_for_unknown():
    assert get_pipeline("never_registered") is None


def test_has_pipeline_true_after_register():
    register(agent_type="present", shape=RegSimple)
    assert has_pipeline("present") is True


def test_has_pipeline_false_for_unknown():
    assert has_pipeline("never_registered") is False


def test_has_pipeline_empty_registry():
    # conftest clears the registry between tests
    assert has_pipeline("anything") is False
