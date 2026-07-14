"""Tests for pf_core.eval._golden (GoldenSetRepo)."""

from __future__ import annotations

import logging

import pytest

from pf_core.eval._golden import GoldenSetRepo
from pf_core.llm.tracking import (
    llm_agent_types,
    llm_models,
    llm_run_outcomes,
    llm_run_payloads,
    llm_runs,
    metadata,
    clear_resolver_caches,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_caches():
    clear_resolver_caches()
    yield
    clear_resolver_caches()


@pytest.fixture
def tracking_db(pf_engine):
    """SQLite in-memory with all tracking + jobs tables."""
    metadata.create_all(pf_engine)
    yield pf_engine
    metadata.drop_all(pf_engine)


@pytest.fixture
def seed_run(tracking_db):
    """Insert one llm_runs row + payload sidecar. Returns run_id."""
    with tracking_db.begin() as conn:
        model_id = conn.execute(
            llm_models.insert().values(name="test-model-golden")
        ).inserted_primary_key[0]
        agent_id = conn.execute(
            llm_agent_types.insert().values(slug="golden_test_agent")
        ).inserted_primary_key[0]
        run_id = conn.execute(
            llm_runs.insert().values(
                agent_type_id=agent_id,
                model_id=model_id,
                status="success",
            )
        ).inserted_primary_key[0]
        conn.execute(
            llm_run_payloads.insert().values(
                llm_run_id=run_id,
                rendered_system="Be helpful.",
                rendered_user="Draft me a summary.",
                raw_response='{"title": "Good Draft"}',
                parsed_output={"title": "Good Draft"},
            )
        )
    return run_id


# ---------------------------------------------------------------------------
# add / remove / list
# ---------------------------------------------------------------------------


def test_add_and_list(tracking_db, seed_run):
    repo = GoldenSetRepo()
    repo.add(seed_run, version="golden_v1", notes="canonical")

    rows = repo.list(version="golden_v1")
    ids = [r["id"] for r in rows]
    assert seed_run in ids


def test_add_idempotent(tracking_db, seed_run):
    """Re-adding the same run should not raise and avoids duplicate tags."""
    repo = GoldenSetRepo()
    repo.add(seed_run, version="golden_v1")
    repo.add(seed_run, version="golden_v1")

    rows = repo.list(version="golden_v1")
    assert len([r for r in rows if r["id"] == seed_run]) == 1


def test_remove(tracking_db, seed_run):
    repo = GoldenSetRepo()
    repo.add(seed_run, version="golden_v1")
    repo.remove(seed_run, version="golden_v1")

    rows = repo.list(version="golden_v1")
    assert all(r["id"] != seed_run for r in rows)


def test_remove_preserves_outcomes(tracking_db, seed_run):
    """Removing from golden set must NOT delete the golden_approved outcome."""
    from sqlalchemy import select

    repo = GoldenSetRepo()
    repo.add(seed_run, version="golden_v1", notes="keep me")
    repo.remove(seed_run, version="golden_v1")

    with tracking_db.connect() as conn:
        row = conn.execute(
            select(llm_run_outcomes).where(
                llm_run_outcomes.c.llm_run_id == seed_run
            )
        ).mappings().fetchone()
    assert row is not None
    assert row["outcome_kind"] == "golden_approved"


def test_list_filtered_by_version(tracking_db, seed_run):
    repo = GoldenSetRepo()
    repo.add(seed_run, version="golden_v1")
    repo.add(seed_run, version="golden_v2")

    v1 = [r["id"] for r in repo.list(version="golden_v1")]
    v2 = [r["id"] for r in repo.list(version="golden_v2")]
    assert seed_run in v1
    assert seed_run in v2


# ---------------------------------------------------------------------------
# get_payload
# ---------------------------------------------------------------------------


def test_get_payload(tracking_db, seed_run):
    repo = GoldenSetRepo()
    repo.add(seed_run, version="golden_v1")

    payload = repo.get_payload(seed_run)
    assert payload is not None
    assert payload["rendered_user"] == "Draft me a summary."
    assert payload["parsed_output"] == {"title": "Good Draft"}


def test_get_payload_none(tracking_db):
    """Run with no payload sidecar returns None."""
    with tracking_db.begin() as conn:
        model_id = conn.execute(
            llm_models.insert().values(name="test-model-nopayload")
        ).inserted_primary_key[0]
        agent_id = conn.execute(
            llm_agent_types.insert().values(slug="no_payload_agent2")
        ).inserted_primary_key[0]
        run_id = conn.execute(
            llm_runs.insert().values(
                agent_type_id=agent_id, model_id=model_id, status="success"
            )
        ).inserted_primary_key[0]

    repo = GoldenSetRepo()
    assert repo.get_payload(run_id) is None


# ---------------------------------------------------------------------------
# get_ground_truth
# ---------------------------------------------------------------------------


def test_ground_truth_roundtrip(tracking_db, seed_run):
    repo = GoldenSetRepo()
    repo.add(
        seed_run,
        version="golden_v1",
        ground_truth={"expected_grade": 85.0, "tier1_ratio": 0.9},
    )
    gt = repo.get_ground_truth(seed_run)
    assert gt == {"expected_grade": 85.0, "tier1_ratio": 0.9}


def test_ground_truth_idempotent(tracking_db, seed_run):
    """Re-adding with updated ground_truth overwrites old values."""
    repo = GoldenSetRepo()
    repo.add(seed_run, version="golden_v1", ground_truth={"score": 1.0})
    repo.add(seed_run, version="golden_v1", ground_truth={"score": 2.0})

    gt = repo.get_ground_truth(seed_run)
    assert gt["score"] == 2.0


def test_ground_truth_empty(tracking_db, seed_run):
    repo = GoldenSetRepo()
    repo.add(seed_run, version="golden_v1")
    gt = repo.get_ground_truth(seed_run)
    assert gt == {}


# ---------------------------------------------------------------------------
# list() — enriched output
# ---------------------------------------------------------------------------


def test_list_includes_agent_type_slug(tracking_db, seed_run):
    """list() returns agent_type_slug and model_name in each row."""
    repo = GoldenSetRepo()
    repo.add(seed_run, version="golden_v1")

    rows = repo.list(version="golden_v1")
    assert len(rows) == 1
    assert rows[0]["agent_type_slug"] == "golden_test_agent"
    assert rows[0]["model_name"] == "test-model-golden"


# ---------------------------------------------------------------------------
# seed_from_outcomes
# ---------------------------------------------------------------------------


def test_seed_from_outcomes_basic(tracking_db, seed_run):
    """seed_from_outcomes promotes runs that have the given outcome_kind."""
    with tracking_db.begin() as conn:
        conn.execute(
            llm_run_outcomes.insert().values(
                llm_run_id=seed_run, outcome_kind="draft_accepted", score=1.0
            )
        )

    repo = GoldenSetRepo()
    seeded = repo.seed_from_outcomes(version="golden_v1", outcome_kind="draft_accepted")

    assert seed_run in seeded
    golden_ids = [r["id"] for r in repo.list(version="golden_v1")]
    assert seed_run in golden_ids


def test_seed_from_outcomes_dry_run(tracking_db, seed_run):
    """dry_run=True returns candidates without modifying the golden set."""
    with tracking_db.begin() as conn:
        conn.execute(
            llm_run_outcomes.insert().values(
                llm_run_id=seed_run, outcome_kind="draft_accepted"
            )
        )

    repo = GoldenSetRepo()
    candidates = repo.seed_from_outcomes(
        version="golden_v1", outcome_kind="draft_accepted", dry_run=True
    )

    assert seed_run in candidates
    assert repo.list(version="golden_v1") == []


def test_seed_from_outcomes_agent_type_filter(tracking_db):
    """agent_type filter restricts seeding to the matching agent."""
    with tracking_db.begin() as conn:
        mid = conn.execute(
            llm_models.insert().values(name="m-seed-filter")
        ).inserted_primary_key[0]
        aid1 = conn.execute(
            llm_agent_types.insert().values(slug="seed_drafter")
        ).inserted_primary_key[0]
        aid2 = conn.execute(
            llm_agent_types.insert().values(slug="seed_classifier")
        ).inserted_primary_key[0]

        run1 = conn.execute(
            llm_runs.insert().values(agent_type_id=aid1, model_id=mid, status="success")
        ).inserted_primary_key[0]
        run2 = conn.execute(
            llm_runs.insert().values(agent_type_id=aid2, model_id=mid, status="success")
        ).inserted_primary_key[0]

        conn.execute(
            llm_run_outcomes.insert(),
            [
                {"llm_run_id": run1, "outcome_kind": "accepted"},
                {"llm_run_id": run2, "outcome_kind": "accepted"},
            ],
        )

    repo = GoldenSetRepo()
    seeded = repo.seed_from_outcomes(
        version="golden_v1", outcome_kind="accepted", agent_type="seed_drafter"
    )

    assert run1 in seeded
    assert run2 not in seeded


def test_seed_from_outcomes_limit(tracking_db):
    """limit parameter caps the number of promoted runs."""
    with tracking_db.begin() as conn:
        mid = conn.execute(
            llm_models.insert().values(name="m-seed-limit")
        ).inserted_primary_key[0]
        aid = conn.execute(
            llm_agent_types.insert().values(slug="seed_limit_agent")
        ).inserted_primary_key[0]

        run_ids = []
        for _ in range(5):
            rid = conn.execute(
                llm_runs.insert().values(
                    agent_type_id=aid, model_id=mid, status="success"
                )
            ).inserted_primary_key[0]
            run_ids.append(rid)

        conn.execute(
            llm_run_outcomes.insert(),
            [{"llm_run_id": rid, "outcome_kind": "completed"} for rid in run_ids],
        )

    repo = GoldenSetRepo()
    seeded = repo.seed_from_outcomes(
        version="golden_v1", outcome_kind="completed", limit=3
    )
    assert len(seeded) == 3


def test_seed_from_outcomes_empty(tracking_db):
    """No matching outcomes returns empty list without error."""
    repo = GoldenSetRepo()
    seeded = repo.seed_from_outcomes(version="golden_v1", outcome_kind="nonexistent_kind")
    assert seeded == []


# ---------------------------------------------------------------------------
# Promote-time payload warnings
# ---------------------------------------------------------------------------


def _seed_bare_run(tracking_db, *, slug: str, payload_values: dict | None) -> int:
    """Insert one run; attach a payload row only if payload_values is given."""
    with tracking_db.begin() as conn:
        mid = conn.execute(
            llm_models.insert().values(name=f"warn-model-{slug}")
        ).inserted_primary_key[0]
        aid = conn.execute(
            llm_agent_types.insert().values(slug=slug)
        ).inserted_primary_key[0]
        run_id = conn.execute(
            llm_runs.insert().values(agent_type_id=aid, model_id=mid, status="success")
        ).inserted_primary_key[0]
        if payload_values is not None:
            conn.execute(
                llm_run_payloads.insert().values(llm_run_id=run_id, **payload_values)
            )
    return run_id


def test_add_warns_when_run_has_no_payload(tracking_db, caplog):
    """A golden with no llm_run_payloads row cannot replay — warn at promote time."""
    run_id = _seed_bare_run(tracking_db, slug="warn_nopayload_agent", payload_values=None)
    with caplog.at_level(logging.WARNING, logger="pf_core.eval._golden"):
        GoldenSetRepo().add(run_id, version="warn_v1")
    assert any("golden_missing_payload" in r.getMessage() for r in caplog.records)


def test_add_warns_when_parsed_output_empty(tracking_db, caplog):
    """JSON-null / empty parsed_output degrades structured_diff — warn at promote time."""
    run_id = _seed_bare_run(
        tracking_db,
        slug="warn_nullparsed_agent",
        payload_values={"rendered_user": "Q", "raw_response": "{}", "parsed_output": None},
    )
    with caplog.at_level(logging.WARNING, logger="pf_core.eval._golden"):
        GoldenSetRepo().add(run_id, version="warn_v1")
    assert any("golden_missing_parsed_output" in r.getMessage() for r in caplog.records)


def test_add_does_not_warn_on_complete_payload(tracking_db, seed_run, caplog):
    with caplog.at_level(logging.WARNING, logger="pf_core.eval._golden"):
        GoldenSetRepo().add(seed_run, version="warn_v1")
    assert not [r for r in caplog.records if "golden_missing" in r.getMessage()]
