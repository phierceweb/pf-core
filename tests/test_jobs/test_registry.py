"""Registry validation: register_kind, get_kind, transitions."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from pf_core.exceptions import ConfigurationError, InvalidInputError
from pf_core.jobs import (
    DEFAULT_STATES,
    DEFAULT_TRANSITIONS,
    all_kinds,
    get_kind,
    register_kind,
)


class Inputs(BaseModel):
    x: int


def test_register_kind_returns_descriptor():
    desc = register_kind(kind="thing")
    assert desc.kind == "thing"
    assert desc.states == DEFAULT_STATES
    assert desc.default_priority == 50


def test_register_kind_defaults_to_default_transitions():
    desc = register_kind(kind="thing")
    # Every DEFAULT_TRANSITIONS entry carried over as a tuple
    for k, v in DEFAULT_TRANSITIONS.items():
        assert desc.transitions[k] == tuple(v)


def test_register_kind_rejects_empty_kind():
    with pytest.raises(ConfigurationError):
        register_kind(kind="")


def test_register_kind_rejects_transitions_from_unknown_state():
    with pytest.raises(ConfigurationError, match="transition source"):
        register_kind(
            kind="x",
            states=["pending", "running"],
            transitions={"bogus": ["running"]},
        )


def test_register_kind_rejects_transitions_to_unknown_state():
    with pytest.raises(ConfigurationError, match="unknown states"):
        register_kind(
            kind="x",
            states=["pending", "running"],
            transitions={"pending": ["mystery"]},
        )


def test_register_kind_rejects_bad_priority():
    with pytest.raises(ConfigurationError, match="default_priority"):
        register_kind(kind="x", default_priority=500)


def test_register_kind_is_idempotent_same_signature():
    desc1 = register_kind(kind="thing", description="first")
    desc2 = register_kind(kind="thing", description="first")
    assert desc1 is desc2 or desc1 == desc2


def test_register_kind_rejects_reregistration_with_different_signature():
    register_kind(kind="thing", description="first")
    with pytest.raises(ConfigurationError, match="already registered"):
        register_kind(kind="thing", description="different")


def test_get_kind_unknown_raises_with_known_list():
    register_kind(kind="a")
    register_kind(kind="b")
    with pytest.raises(ConfigurationError, match=r"Known kinds:.*'a'.*'b'"):
        get_kind("nope")


def test_all_kinds_returns_sorted():
    register_kind(kind="zebra")
    register_kind(kind="apple")
    register_kind(kind="mango")
    assert [k.kind for k in all_kinds()] == ["apple", "mango", "zebra"]


def test_can_transition_uses_registered_table():
    desc = register_kind(
        kind="custom",
        states=["a", "b", "c"],
        transitions={"a": ["b"], "b": ["c"]},
    )
    assert desc.can_transition("a", "b") is True
    assert desc.can_transition("a", "c") is False
    assert desc.can_transition("c", "a") is False  # terminal


def test_validate_inputs_passes_through_without_schema():
    desc = register_kind(kind="no_schema")
    assert desc.validate_inputs({"anything": 1}) == {"anything": 1}


def test_validate_inputs_uses_pydantic_schema():
    desc = register_kind(kind="with_schema", inputs_schema=Inputs)
    result = desc.validate_inputs({"x": 42})
    assert isinstance(result, Inputs)
    assert result.x == 42


def test_validate_inputs_raises_on_schema_mismatch():
    desc = register_kind(kind="with_schema", inputs_schema=Inputs)
    with pytest.raises(InvalidInputError, match="failed schema validation"):
        desc.validate_inputs({"x": "not an int"})


def test_validate_outputs_rejects_non_basemodel_schema():
    desc = register_kind(kind="bad_schema", outputs_schema=dict)
    with pytest.raises(ConfigurationError, match="Unsupported outputs_schema"):
        desc.validate_outputs({"foo": 1})
