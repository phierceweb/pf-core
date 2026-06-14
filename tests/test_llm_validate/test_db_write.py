"""DB write path: signals + schema-version tag persistence."""

from __future__ import annotations

import json

from pf_core.llm.tracking import LlmRunRepo
from pf_core.llm.tracking import schema as ts
from pf_core.llm.validate import parse_and_validate, register

from .conftest import PydOk


def test_pipeline_writes_signals_and_tag_to_db(tracking_db):
    register(
        agent_type="db_a", shape=PydOk,
        semantic=["url_sanity"], schema_version=3,
    )
    run_id = LlmRunRepo().record(agent_type="db_a", model="claude-opus-4-7")

    res = parse_and_validate(
        json.dumps({"headline": "hi", "score": 1}),
        agent_type="db_a", run_id=run_id,
    )
    assert res.ok is True

    with tracking_db.connect() as conn:
        rows = conn.execute(
            ts.llm_run_validations.select().where(
                ts.llm_run_validations.c.llm_run_id == run_id
            )
        ).mappings().fetchall()
        tags = conn.execute(
            ts.llm_run_tags.select().where(
                ts.llm_run_tags.c.llm_run_id == run_id
            )
        ).mappings().fetchall()

    by_validator = {r["validator"]: r for r in rows}
    assert "db_a_shape" in by_validator
    assert "url_sanity" in by_validator
    assert by_validator["db_a_shape"]["passed"] is True
    assert by_validator["db_a_shape"]["severity"] == "error"
    assert "schema:db_a_v3" in {t["tag"] for t in tags}


def test_pipeline_re_call_replaces_prior_validation_rows(tracking_db):
    register(agent_type="db_b", shape=PydOk)
    run_id = LlmRunRepo().record(agent_type="db_b", model="claude-opus-4-7")

    parse_and_validate(
        json.dumps({"headline": "hi", "score": 1}),
        agent_type="db_b", run_id=run_id,
    )
    with tracking_db.connect() as conn:
        first = conn.execute(
            ts.llm_run_validations.select().where(
                ts.llm_run_validations.c.llm_run_id == run_id
            )
        ).mappings().fetchall()
    assert all(r["passed"] for r in first)

    parse_and_validate(
        json.dumps({"headline": "hi"}),  # missing score
        agent_type="db_b", run_id=run_id,
    )
    with tracking_db.connect() as conn:
        second = conn.execute(
            ts.llm_run_validations.select().where(
                (ts.llm_run_validations.c.llm_run_id == run_id)
                & (ts.llm_run_validations.c.validator == "db_b_shape")
            )
        ).mappings().fetchall()
    assert len(second) == 1
    assert second[0]["passed"] is False


def test_pipeline_no_pipeline_fallback_with_run_id_does_not_write(tracking_db):
    run_id = LlmRunRepo().record(agent_type="other", model="claude-opus-4-7")
    res = parse_and_validate(
        "{}", agent_type="never_registered", run_id=run_id,
        missing_pipeline="fallback",
    )
    assert res.ok is False

    with tracking_db.connect() as conn:
        rows = conn.execute(
            ts.llm_run_validations.select().where(
                ts.llm_run_validations.c.llm_run_id == run_id
            )
        ).mappings().fetchall()
    assert rows == []
