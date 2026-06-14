"""Shared fixtures for the jobs test package."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from pf_core.jobs import clear_registry, register_kind
from pf_core.llm.tracking import clear_resolver_caches, metadata


class SimpleInputs(BaseModel):
    widget_ids: list[int]
    config_id: int | None = None


class SimpleOutputs(BaseModel):
    n_processed: int


@pytest.fixture(autouse=True)
def _reset_jobs_state():
    """Reset registry + resolver caches between tests."""
    clear_registry()
    clear_resolver_caches()
    yield
    clear_registry()
    clear_resolver_caches()


@pytest.fixture()
def jobs_db(pf_engine):
    """In-memory SQLite engine with jobs + llm_* tables created."""
    metadata.create_all(pf_engine)
    yield pf_engine
    metadata.drop_all(pf_engine)


@pytest.fixture()
def simple_kind():
    """Register a minimal kind with default state machine + Pydantic schemas."""
    return register_kind(
        kind="simple_pass",
        inputs_schema=SimpleInputs,
        outputs_schema=SimpleOutputs,
    )


@pytest.fixture()
def strict_kind():
    """Register a kind with a custom state machine — pending → running → done only."""
    return register_kind(
        kind="strict_flow",
        states=["pending", "running", "succeeded", "failed"],
        transitions={
            "pending": ["running"],
            "running": ["succeeded", "failed"],
            "failed": ["pending"],
        },
    )
