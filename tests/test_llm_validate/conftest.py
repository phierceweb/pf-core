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


@pytest.fixture(autouse=True)
def _reset_validate_state():
    """Reset all module-level state between tests."""
    clear_registry()
    clear_cross_field_validators()
    register_tier1_domains(lambda: set())
    register_url_hallucination_rules(lambda: [])
    yield
    clear_registry()
    clear_cross_field_validators()
    register_tier1_domains(lambda: set())
    register_url_hallucination_rules(lambda: [])


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
