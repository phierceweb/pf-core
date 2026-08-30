"""Shared fixtures and helpers for the validate test package."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, Field

from pf_core.llm.tracking import clear_resolver_caches, metadata
from pf_core.llm.validate import (
    clear_cross_field_validators,
    clear_registry,
    register_tier1_domains,
    register_url_hallucination_rules,
)
from pf_core.llm.validate import _cross_field, _registry, _semantic


@pytest.fixture(autouse=True)
def _reset_validate_state():
    """Give each test a clean registry, then restore the pre-test state.

    The registries are process-global; other test modules (test_llm_step)
    register pipelines at import time, so teardown must restore rather than
    clear or their registrations vanish for the rest of the session.
    """
    saved_pipelines = dict(_registry._REGISTRY)
    saved_cross_field = dict(_cross_field._CROSS_FIELD_VALIDATORS)
    saved_tier1 = _semantic._TIER1_HOOK
    saved_url_rules = _semantic._URL_RULES_HOOK
    clear_registry()
    clear_cross_field_validators()
    register_tier1_domains(lambda: set())
    register_url_hallucination_rules(lambda: [])
    yield
    _registry._REGISTRY.clear()
    _registry._REGISTRY.update(saved_pipelines)
    _cross_field._CROSS_FIELD_VALIDATORS.clear()
    _cross_field._CROSS_FIELD_VALIDATORS.update(saved_cross_field)
    register_tier1_domains(saved_tier1)
    register_url_hallucination_rules(saved_url_rules)


@pytest.fixture()
def tracking_db(pf_engine):
    """In-memory SQLite engine with all ``llm_*`` tables created."""
    clear_resolver_caches()
    metadata.create_all(pf_engine)
    yield pf_engine
    metadata.drop_all(pf_engine)
    clear_resolver_caches()


# Shared models -------------------------------------------------------------


class RegSimple(BaseModel):
    name: str


class PydOk(BaseModel):
    headline: str = Field(min_length=1)
    score: int


class PydForbid(BaseModel):
    a: str
    model_config = {"extra": "forbid"}


class Doc(BaseModel):
    headline: str = ""
    body: str = ""
    sources: list[str] = []
    published_at: str | None = None
    model_config = {"extra": "allow"}


def payload(**fields) -> str:
    return json.dumps(fields)
