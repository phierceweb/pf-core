"""Tests for the pf_core.llm.tracking schema (SQLAlchemy metadata).

Schema correctness is verified against an in-memory SQLite engine. MySQL and
PostgreSQL DDL is exercised in the testcontainer suite (separate run).
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine, event, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from pf_core.llm.tracking import metadata
from pf_core.llm.tracking.schema import (
    ALL_TABLES,
    llm_agent_types,
    llm_models,
    llm_prompts,
    llm_run_configs,
    llm_run_links,
    llm_run_metrics,
    llm_run_outcomes,
    llm_run_payloads,
    llm_run_tags,
    llm_run_validations,
    llm_runs,
)


@pytest.fixture()
def engine() -> Engine:
    """Fresh in-memory SQLite engine with foreign keys enabled."""
    eng = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(eng, "connect")
    def _fk(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys = ON")
        cur.close()

    metadata.create_all(eng)
    return eng


# ---------------------------------------------------------------------------
# Structural tests
# ---------------------------------------------------------------------------


def test_all_tables_created(engine):
    insp = inspect(engine)
    actual = set(insp.get_table_names())
    expected = {t.name for t in ALL_TABLES}
    assert expected.issubset(actual)


def test_all_tables_in_dependency_order():
    """ALL_TABLES is iterated for create/drop — reference tables must come first."""
    names = [t.name for t in ALL_TABLES]
    assert names.index("llm_models") < names.index("llm_runs")
    assert names.index("llm_agent_types") < names.index("llm_prompts")
    assert names.index("llm_prompts") < names.index("llm_runs")
    assert names.index("llm_runs") < names.index("llm_run_payloads")
    assert names.index("llm_runs") < names.index("llm_run_tags")


def test_llm_runs_columns(engine):
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("llm_runs")}
    expected_subset = {
        "id",
        "agent_type_id",
        "model_id",
        "system_prompt_id",
        "user_prompt_id",
        "temperature",
        "top_p",
        "max_tokens",
        "seed",
        "stop_sequences",
        "provider",
        "model_fingerprint",
        "prompt_tokens",
        "completion_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "cost_usd",
        "duration_ms",
        "items_out",
        "status",
        "error",
        "error_class",
        "error_code",
        "http_status",
        "input_hash",
        "created_at",
    }
    assert expected_subset.issubset(cols)


def test_llm_runs_indexes_present(engine):
    insp = inspect(engine)
    idx_names = {i["name"] for i in insp.get_indexes("llm_runs")}
    expected = {
        "idx_llm_runs_created_at",
        "idx_llm_runs_agent_type_created",
        "idx_llm_runs_model_created",
        "idx_llm_runs_status_created",
        "idx_llm_runs_input_hash",
        "idx_llm_runs_fingerprint",
    }
    assert expected.issubset(idx_names)


def test_llm_prompts_unique_constraint(engine):
    insp = inspect(engine)
    uqs = insp.get_unique_constraints("llm_prompts")
    triples = [tuple(u["column_names"]) for u in uqs]
    assert ("agent_type_id", "part", "version") in triples


def test_attachment_tables_have_composite_pk(engine):
    insp = inspect(engine)
    cases = {
        "llm_run_configs": ["llm_run_id", "config_kind"],
        "llm_run_validations": ["llm_run_id", "validator"],
        "llm_run_outcomes": ["llm_run_id", "outcome_kind"],
        "llm_run_tags": ["llm_run_id", "tag"],
        "llm_run_metrics": ["llm_run_id", "metric_name"],
    }
    for table, expected in cases.items():
        pk = insp.get_pk_constraint(table)
        assert pk["constrained_columns"] == expected, table


def test_llm_run_links_three_column_pk(engine):
    insp = inspect(engine)
    pk = insp.get_pk_constraint("llm_run_links")
    assert pk["constrained_columns"] == ["parent_run_id", "child_run_id", "relation"]


def test_llm_run_payloads_pk_is_run_id(engine):
    insp = inspect(engine)
    pk = insp.get_pk_constraint("llm_run_payloads")
    assert pk["constrained_columns"] == ["llm_run_id"]


def test_foreign_keys_cascade_from_llm_runs(engine):
    """Sidecar tables must cascade-delete when the parent llm_runs row is deleted."""
    insp = inspect(engine)
    for table in (
        "llm_run_payloads",
        "llm_run_configs",
        "llm_run_validations",
        "llm_run_outcomes",
        "llm_run_tags",
        "llm_run_metrics",
    ):
        fks = insp.get_foreign_keys(table)
        run_fks = [fk for fk in fks if fk["referred_table"] == "llm_runs"]
        assert run_fks, f"{table} missing FK to llm_runs"
        assert run_fks[0]["options"].get("ondelete", "").upper() == "CASCADE", table


# ---------------------------------------------------------------------------
# Behavioural tests — round-trip data through every table
# ---------------------------------------------------------------------------


def test_minimal_run_roundtrip(engine):
    today = dt.date(2026, 4, 16)
    with engine.begin() as conn:
        model_id = conn.execute(
            llm_models.insert().values(name="claude-opus-4-7")
        ).inserted_primary_key[0]
        agent_id = conn.execute(
            llm_agent_types.insert().values(slug="drafter")
        ).inserted_primary_key[0]
        prompt_id = conn.execute(
            llm_prompts.insert().values(
                agent_type_id=agent_id,
                part="full",
                version=1,
                content="You are a careful drafter.",
                effective_date=today,
            )
        ).inserted_primary_key[0]
        run_id = conn.execute(
            llm_runs.insert().values(
                agent_type_id=agent_id,
                model_id=model_id,
                system_prompt_id=prompt_id,
                temperature=0.2,
                prompt_tokens=1200,
                completion_tokens=800,
                cost_usd=0.0052,
                duration_ms=3100,
                items_out=7,
            )
        ).inserted_primary_key[0]

    with engine.connect() as conn:
        row = conn.execute(select(llm_runs).where(llm_runs.c.id == run_id)).mappings().one()
        assert row["agent_type_id"] == agent_id
        assert row["model_id"] == model_id
        assert row["system_prompt_id"] == prompt_id
        assert float(row["cost_usd"]) == pytest.approx(0.0052)
        assert row["status"] == "success"
        assert row["created_at"] is not None


def test_full_attachments_roundtrip(engine):
    today = dt.date(2026, 4, 16)
    with engine.begin() as conn:
        model_id = conn.execute(
            llm_models.insert().values(name="claude-opus-4-7")
        ).inserted_primary_key[0]
        agent_id = conn.execute(
            llm_agent_types.insert().values(slug="reviewer")
        ).inserted_primary_key[0]
        sys_id = conn.execute(
            llm_prompts.insert().values(
                agent_type_id=agent_id,
                part="system",
                version=1,
                content="System.",
                effective_date=today,
            )
        ).inserted_primary_key[0]
        usr_id = conn.execute(
            llm_prompts.insert().values(
                agent_type_id=agent_id,
                part="user",
                version=1,
                content="User.",
                effective_date=today,
            )
        ).inserted_primary_key[0]
        run_id = conn.execute(
            llm_runs.insert().values(
                agent_type_id=agent_id,
                model_id=model_id,
                system_prompt_id=sys_id,
                user_prompt_id=usr_id,
                stop_sequences=["</done>"],
            )
        ).inserted_primary_key[0]

        conn.execute(
            llm_run_payloads.insert().values(
                llm_run_id=run_id,
                rendered_system="rendered sys",
                rendered_user="rendered usr",
                raw_response='{"ok": true}',
                parsed_output={"ok": True},
            )
        )
        conn.execute(
            llm_run_configs.insert().values(
                llm_run_id=run_id, config_kind="report_config", config_id=42
            )
        )
        conn.execute(
            llm_run_validations.insert().values(
                llm_run_id=run_id,
                validator="url_hallucination",
                severity="warn",
                passed=False,
                details={"failed_urls": ["http://x"]},
            )
        )
        conn.execute(
            llm_run_outcomes.insert().values(
                llm_run_id=run_id, outcome_kind="result_matches_reviewer", score=0.92
            )
        )
        conn.execute(
            llm_run_tags.insert().values(llm_run_id=run_id, tag="env:prod")
        )
        conn.execute(
            llm_run_tags.insert().values(llm_run_id=run_id, tag="eval:golden_v2")
        )
        conn.execute(
            llm_run_metrics.insert().values(
                llm_run_id=run_id, metric_name="tier1_ratio", metric_value=0.85
            )
        )

    with engine.connect() as conn:
        payload = conn.execute(
            select(llm_run_payloads).where(llm_run_payloads.c.llm_run_id == run_id)
        ).mappings().one()
        assert payload["parsed_output"] == {"ok": True}
        assert payload["rendered_system"] == "rendered sys"

        tags = conn.execute(
            select(llm_run_tags.c.tag).where(llm_run_tags.c.llm_run_id == run_id)
        ).scalars().all()
        assert set(tags) == {"env:prod", "eval:golden_v2"}

        metric = conn.execute(
            select(llm_run_metrics).where(llm_run_metrics.c.llm_run_id == run_id)
        ).mappings().one()
        assert metric["metric_value"] == pytest.approx(0.85)


def test_cascade_delete_removes_sidecar_rows(engine):
    with engine.begin() as conn:
        model_id = conn.execute(
            llm_models.insert().values(name="claude-opus-4-7")
        ).inserted_primary_key[0]
        agent_id = conn.execute(
            llm_agent_types.insert().values(slug="drafter")
        ).inserted_primary_key[0]
        run_id = conn.execute(
            llm_runs.insert().values(agent_type_id=agent_id, model_id=model_id)
        ).inserted_primary_key[0]
        conn.execute(
            llm_run_payloads.insert().values(llm_run_id=run_id, raw_response="r")
        )
        conn.execute(llm_run_tags.insert().values(llm_run_id=run_id, tag="x"))

    with engine.begin() as conn:
        conn.execute(llm_runs.delete().where(llm_runs.c.id == run_id))

    with engine.connect() as conn:
        assert (
            conn.execute(
                select(llm_run_payloads).where(llm_run_payloads.c.llm_run_id == run_id)
            ).first()
            is None
        )
        assert (
            conn.execute(
                select(llm_run_tags).where(llm_run_tags.c.llm_run_id == run_id)
            ).first()
            is None
        )


def test_run_links_self_reference(engine):
    with engine.begin() as conn:
        model_id = conn.execute(
            llm_models.insert().values(name="claude-opus-4-7")
        ).inserted_primary_key[0]
        agent_id = conn.execute(
            llm_agent_types.insert().values(slug="drafter")
        ).inserted_primary_key[0]
        parent = conn.execute(
            llm_runs.insert().values(agent_type_id=agent_id, model_id=model_id)
        ).inserted_primary_key[0]
        child = conn.execute(
            llm_runs.insert().values(agent_type_id=agent_id, model_id=model_id)
        ).inserted_primary_key[0]
        conn.execute(
            llm_run_links.insert().values(
                parent_run_id=parent, child_run_id=child, relation="retry"
            )
        )

    with engine.connect() as conn:
        row = conn.execute(
            select(llm_run_links).where(llm_run_links.c.parent_run_id == parent)
        ).mappings().one()
        assert row["child_run_id"] == child
        assert row["relation"] == "retry"


def test_prompts_unique_violates_on_duplicate(engine):
    today = dt.date(2026, 4, 16)
    with engine.begin() as conn:
        agent_id = conn.execute(
            llm_agent_types.insert().values(slug="drafter")
        ).inserted_primary_key[0]
        conn.execute(
            llm_prompts.insert().values(
                agent_type_id=agent_id,
                part="system",
                version=1,
                content="A",
                effective_date=today,
            )
        )
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                llm_prompts.insert().values(
                    agent_type_id=agent_id,
                    part="system",
                    version=1,
                    content="B",
                    effective_date=today,
                )
            )


def test_models_name_unique(engine):
    with engine.begin() as conn:
        conn.execute(llm_models.insert().values(name="claude-opus-4-7"))
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(llm_models.insert().values(name="claude-opus-4-7"))


def test_agent_types_slug_unique(engine):
    with engine.begin() as conn:
        conn.execute(llm_agent_types.insert().values(slug="drafter"))
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(llm_agent_types.insert().values(slug="drafter"))


def test_prompts_part_check_constraint_rejects_invalid(engine):
    today = dt.date(2026, 4, 16)
    with engine.begin() as conn:
        agent_id = conn.execute(
            llm_agent_types.insert().values(slug="drafter")
        ).inserted_primary_key[0]
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                llm_prompts.insert().values(
                    agent_type_id=agent_id,
                    part="bogus",
                    version=1,
                    content="x",
                    effective_date=today,
                )
            )
