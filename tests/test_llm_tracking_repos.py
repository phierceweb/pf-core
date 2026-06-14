"""Tests for the pf_core.llm.tracking repos, stats, and purge helper.

Uses the ``pf_engine`` fixture (in-memory SQLite, patched into
``pf_core.db.connection``) so the repo's ``transaction()`` calls hit the
test engine.
"""

from __future__ import annotations

import datetime as dt

import pytest

from pf_core.llm.tracking import (
    LlmRunLinkRepo,
    LlmRunOutcomeRepo,
    LlmRunRepo,
    LlmRunStatsRepo,
    LlmRunValidationRepo,
    clear_resolver_caches,
    metadata,
    purge_old_payloads,
)
from pf_core.llm.tracking import schema as s


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_caches_between_tests():
    """Drop process-level resolver caches so stale IDs from a prior test don't leak."""
    clear_resolver_caches()
    yield
    clear_resolver_caches()


@pytest.fixture()
def tracking_db(pf_engine):
    """In-memory SQLite engine with all ``llm_*`` tables created."""
    metadata.create_all(pf_engine)
    yield pf_engine
    metadata.drop_all(pf_engine)


# ---------------------------------------------------------------------------
# LlmRunRepo.record() — minimum viable
# ---------------------------------------------------------------------------


def test_record_minimum_viable_returns_id(tracking_db):
    run_id = LlmRunRepo().record(agent_type="drafter", model="claude-opus-4-7")
    assert isinstance(run_id, int)
    assert run_id > 0


def test_record_minimum_viable_persists_status_default(tracking_db):
    run_id = LlmRunRepo().record(agent_type="drafter", model="claude-opus-4-7")
    row = LlmRunRepo().get(run_id)
    assert row is not None
    assert row["status"] == "success"
    assert row["agent_type_id"] is not None
    assert row["model_id"] is not None


def test_record_resolves_agent_and_model_ids(tracking_db):
    run_id = LlmRunRepo().record(agent_type="grader", model="openai/gpt-4o")
    with tracking_db.connect() as conn:
        agent = conn.execute(
            s.llm_agent_types.select().where(s.llm_agent_types.c.slug == "grader")
        ).mappings().one()
        model = conn.execute(
            s.llm_models.select().where(s.llm_models.c.name == "openai/gpt-4o")
        ).mappings().one()
        run = conn.execute(
            s.llm_runs.select().where(s.llm_runs.c.id == run_id)
        ).mappings().one()
    assert run["agent_type_id"] == agent["id"]
    assert run["model_id"] == model["id"]


def test_record_reuses_existing_agent_and_model_ids(tracking_db):
    a = LlmRunRepo().record(agent_type="drafter", model="claude-opus-4-7")
    b = LlmRunRepo().record(agent_type="drafter", model="claude-opus-4-7")
    ra = LlmRunRepo().get(a)
    rb = LlmRunRepo().get(b)
    assert ra["agent_type_id"] == rb["agent_type_id"]
    assert ra["model_id"] == rb["model_id"]


# ---------------------------------------------------------------------------
# LlmRunRepo.record() — extra_run_values hook
# ---------------------------------------------------------------------------


def test_record_extra_run_values_overrides_framework_column(tracking_db):
    """extra_run_values is merged after the framework columns (last wins)."""
    run_id = LlmRunRepo().record(
        agent_type="drafter",
        model="claude-opus-4-7",
        provider="openrouter",
        extra_run_values={"provider": "from-extra"},
    )
    row = LlmRunRepo().get(run_id)
    assert row["provider"] == "from-extra"


def test_record_extra_run_values_writes_project_column(pf_engine):
    """A consumer that adds a project-specific column to ``llm_runs`` can
    write it through ``extra_run_values`` without overriding ``record()``.

    Mirrors the real consumer pattern: append the column to the shared Table
    (as a migration-backed consumer would at import time), then record. The
    column is removed in teardown so the shared schema isn't mutated for
    other tests.
    """
    from sqlalchemy import Column, Integer

    s.llm_runs.append_column(Column("project_widget_id", Integer, nullable=True))
    try:
        metadata.create_all(pf_engine)
        run_id = LlmRunRepo().record(
            agent_type="drafter",
            model="claude-opus-4-7",
            extra_run_values={"project_widget_id": 4242},
        )
        with pf_engine.connect() as conn:
            row = conn.execute(
                s.llm_runs.select().where(s.llm_runs.c.id == run_id)
            ).mappings().one()
        assert row["project_widget_id"] == 4242
    finally:
        metadata.drop_all(pf_engine)
        s.llm_runs._columns.remove(s.llm_runs.c.project_widget_id)


# ---------------------------------------------------------------------------
# LlmRunRepo.record() — sampling, usage, errors
# ---------------------------------------------------------------------------


def test_record_unpacks_sampling_dict(tracking_db):
    run_id = LlmRunRepo().record(
        agent_type="drafter",
        model="claude-opus-4-7",
        sampling={
            "temperature": 0.2,
            "top_p": 0.9,
            "max_tokens": 4096,
            "seed": 7,
            "stop_sequences": ["</done>"],
        },
    )
    row = LlmRunRepo().get(run_id)
    assert row["temperature"] == pytest.approx(0.2)
    assert row["top_p"] == pytest.approx(0.9)
    assert row["max_tokens"] == 4096
    assert row["seed"] == 7
    assert row["stop_sequences"] == ["</done>"]


def test_record_unpacks_usage_dict(tracking_db):
    run_id = LlmRunRepo().record(
        agent_type="drafter",
        model="claude-opus-4-7",
        usage={
            "prompt_tokens": 1200,
            "completion_tokens": 800,
            "cache_read_tokens": 900,
            "cache_write_tokens": 0,
            "reasoning_tokens": 100,
            "cost_usd": 0.0052,
            "duration_ms": 3100,
        },
    )
    row = LlmRunRepo().get(run_id)
    assert row["prompt_tokens"] == 1200
    assert row["completion_tokens"] == 800
    assert row["cache_read_tokens"] == 900
    assert row["reasoning_tokens"] == 100
    assert float(row["cost_usd"]) == pytest.approx(0.0052)
    assert row["duration_ms"] == 3100


def test_record_failed_run_with_error_fields(tracking_db):
    run_id = LlmRunRepo().record(
        agent_type="drafter",
        model="claude-opus-4-7",
        status="failed",
        error="Connection reset by peer",
        error_class="ConnectionError",
        error_code="ECONNRESET",
        http_status=502,
    )
    row = LlmRunRepo().get(run_id)
    assert row["status"] == "failed"
    assert row["error"] == "Connection reset by peer"
    assert row["error_class"] == "ConnectionError"
    assert row["error_code"] == "ECONNRESET"
    assert row["http_status"] == 502


# ---------------------------------------------------------------------------
# LlmRunRepo.record() — input_hash
# ---------------------------------------------------------------------------


def test_record_computes_input_hash_when_omitted(tracking_db):
    run_id = LlmRunRepo().record(
        agent_type="drafter",
        model="claude-opus-4-7",
        rendered_prompts=("system text", "user text"),
        sampling={"temperature": 0.0},
    )
    row = LlmRunRepo().get(run_id)
    assert row["input_hash"] is not None
    assert len(row["input_hash"]) == 64  # SHA256 hex length


def test_record_identical_inputs_produce_identical_hashes(tracking_db):
    a = LlmRunRepo().record(
        agent_type="drafter",
        model="claude-opus-4-7",
        rendered_prompts=("sys", "usr"),
        sampling={"temperature": 0.2, "top_p": 0.9},
        configs={"essay_config": 42},
    )
    b = LlmRunRepo().record(
        agent_type="drafter",
        model="claude-opus-4-7",
        rendered_prompts=("sys", "usr"),
        sampling={"temperature": 0.2, "top_p": 0.9},
        configs={"essay_config": 42},
    )
    ra = LlmRunRepo().get(a)
    rb = LlmRunRepo().get(b)
    assert ra["input_hash"] == rb["input_hash"]


def test_record_different_models_produce_different_hashes(tracking_db):
    a = LlmRunRepo().record(
        agent_type="drafter",
        model="claude-opus-4-7",
        rendered_prompts=("sys", "usr"),
    )
    b = LlmRunRepo().record(
        agent_type="drafter",
        model="openai/gpt-4o",
        rendered_prompts=("sys", "usr"),
    )
    ra = LlmRunRepo().get(a)
    rb = LlmRunRepo().get(b)
    assert ra["input_hash"] != rb["input_hash"]


def test_record_caller_supplied_hash_overrides_compute(tracking_db):
    run_id = LlmRunRepo().record(
        agent_type="drafter",
        model="claude-opus-4-7",
        input_hash="deadbeef" * 8,
    )
    row = LlmRunRepo().get(run_id)
    assert row["input_hash"] == "deadbeef" * 8


# ---------------------------------------------------------------------------
# LlmRunRepo.record() — attachments
# ---------------------------------------------------------------------------


def test_record_writes_payload_when_provided(tracking_db):
    run_id = LlmRunRepo().record(
        agent_type="drafter",
        model="claude-opus-4-7",
        rendered_prompts=("rendered sys", "rendered usr"),
        raw_response='{"ok": true}',
        parsed_output={"ok": True},
    )
    full = LlmRunRepo().get_with_payload(run_id)
    assert full["payload"] is not None
    assert full["payload"]["rendered_system"] == "rendered sys"
    assert full["payload"]["rendered_user"] == "rendered usr"
    assert full["payload"]["raw_response"] == '{"ok": true}'
    assert full["payload"]["parsed_output"] == {"ok": True}


def test_record_skips_payload_when_all_fields_none(tracking_db):
    run_id = LlmRunRepo().record(agent_type="drafter", model="claude-opus-4-7")
    full = LlmRunRepo().get_with_payload(run_id)
    assert full["payload"] is None


def test_record_writes_configs(tracking_db):
    run_id = LlmRunRepo().record(
        agent_type="grader",
        model="claude-opus-4-7",
        configs={"essay_config": 42, "rubric_version": 7},
    )
    with tracking_db.connect() as conn:
        rows = conn.execute(
            s.llm_run_configs.select().where(s.llm_run_configs.c.llm_run_id == run_id)
        ).mappings().fetchall()
    by_kind = {r["config_kind"]: r["config_id"] for r in rows}
    assert by_kind == {"essay_config": 42, "rubric_version": 7}


def test_record_writes_validations(tracking_db):
    run_id = LlmRunRepo().record(
        agent_type="drafter",
        model="claude-opus-4-7",
        validations=[
            ("url_hallucination", True, "info", None),
            ("json_schema", False, "error", {"missing": ["sources"]}),
        ],
    )
    with tracking_db.connect() as conn:
        rows = conn.execute(
            s.llm_run_validations.select().where(
                s.llm_run_validations.c.llm_run_id == run_id
            )
        ).mappings().fetchall()
    by_validator = {r["validator"]: r for r in rows}
    assert by_validator["url_hallucination"]["passed"] is True
    assert by_validator["json_schema"]["passed"] is False
    assert by_validator["json_schema"]["details"] == {"missing": ["sources"]}


def test_record_writes_metrics(tracking_db):
    run_id = LlmRunRepo().record(
        agent_type="drafter",
        model="claude-opus-4-7",
        metrics={"tier1_ratio": 0.85, "n_sources": 12.0},
    )
    with tracking_db.connect() as conn:
        rows = conn.execute(
            s.llm_run_metrics.select().where(
                s.llm_run_metrics.c.llm_run_id == run_id
            )
        ).mappings().fetchall()
    by_name = {r["metric_name"]: r["metric_value"] for r in rows}
    assert by_name["tier1_ratio"] == pytest.approx(0.85)
    assert by_name["n_sources"] == pytest.approx(12.0)


def test_record_writes_tags(tracking_db):
    run_id = LlmRunRepo().record(
        agent_type="drafter",
        model="claude-opus-4-7",
        tags=["env:prod", "experiment:opus47-a"],
    )
    with tracking_db.connect() as conn:
        from sqlalchemy import select as _select
        rows = conn.execute(
            _select(s.llm_run_tags.c.tag).where(s.llm_run_tags.c.llm_run_id == run_id)
        ).scalars().all()
    assert set(rows) == {"env:prod", "experiment:opus47-a"}


def test_record_writes_parent_run_link(tracking_db):
    parent = LlmRunRepo().record(agent_type="drafter", model="claude-opus-4-7")
    child = LlmRunRepo().record(
        agent_type="drafter",
        model="claude-opus-4-7",
        parent_run=(parent, "retry"),
    )
    with tracking_db.connect() as conn:
        link = conn.execute(
            s.llm_run_links.select().where(s.llm_run_links.c.child_run_id == child)
        ).mappings().one()
    assert link["parent_run_id"] == parent
    assert link["relation"] == "retry"


def test_record_atomic_full_payload_round_trip(tracking_db):
    """Smoke-test the kitchen-sink invocation from the plan."""
    parent = LlmRunRepo().record(agent_type="drafter", model="claude-opus-4-7")
    run_id = LlmRunRepo().record(
        agent_type="drafter",
        model="claude-opus-4-7",
        sampling={"temperature": 0.2, "max_tokens": 4096, "seed": 7},
        provider="openrouter",
        model_fingerprint="fp_abc123",
        usage={
            "prompt_tokens": 1200,
            "completion_tokens": 800,
            "cache_read_tokens": 900,
            "cost_usd": 0.0052,
            "duration_ms": 3100,
        },
        items_out=7,
        configs={"beat_version": 11},
        validations=[("url_hallucination", True, "info", None)],
        metrics={"tier1_ratio": 0.85},
        tags=["env:prod"],
        rendered_prompts=("rendered sys", "rendered usr"),
        raw_response="response text",
        parsed_output={"drafts": [1, 2, 3]},
        parent_run=(parent, "retry"),
    )
    full = LlmRunRepo().get_with_payload(run_id)
    assert full["items_out"] == 7
    assert full["provider"] == "openrouter"
    assert full["model_fingerprint"] == "fp_abc123"
    assert full["payload"]["parsed_output"] == {"drafts": [1, 2, 3]}


# ---------------------------------------------------------------------------
# LlmRunRepo read helpers
# ---------------------------------------------------------------------------


def test_get_returns_none_for_missing_id(tracking_db):
    assert LlmRunRepo().get(99999) is None


def test_get_with_payload_returns_none_for_missing_id(tracking_db):
    assert LlmRunRepo().get_with_payload(99999) is None


def test_find_by_hash_returns_all_matching(tracking_db):
    h = "f" * 64
    a = LlmRunRepo().record(
        agent_type="drafter", model="claude-opus-4-7", input_hash=h
    )
    b = LlmRunRepo().record(
        agent_type="drafter", model="claude-opus-4-7", input_hash=h
    )
    c = LlmRunRepo().record(
        agent_type="drafter", model="claude-opus-4-7", input_hash="0" * 64
    )
    matched_ids = {r["id"] for r in LlmRunRepo().find_by_hash(h)}
    assert matched_ids == {a, b}
    assert c not in matched_ids


def test_find_by_hash_empty_when_no_matches(tracking_db):
    assert LlmRunRepo().find_by_hash("nope" * 16) == []


# ---------------------------------------------------------------------------
# LlmRunOutcomeRepo
# ---------------------------------------------------------------------------


def test_outcome_record_roundtrip(tracking_db):
    run_id = LlmRunRepo().record(agent_type="grader", model="claude-opus-4-7")
    LlmRunOutcomeRepo().record(
        run_id, outcome_kind="grade_matches_professor", score=0.92
    )
    outcomes = LlmRunOutcomeRepo().list_for_run(run_id)
    assert len(outcomes) == 1
    assert outcomes[0]["outcome_kind"] == "grade_matches_professor"
    assert outcomes[0]["score"] == pytest.approx(0.92)


def test_outcome_record_replaces_same_kind(tracking_db):
    run_id = LlmRunRepo().record(agent_type="grader", model="claude-opus-4-7")
    LlmRunOutcomeRepo().record(run_id, outcome_kind="draft_accepted", score=0.5)
    LlmRunOutcomeRepo().record(
        run_id, outcome_kind="draft_accepted", score=1.0, notes="reviewed"
    )
    outcomes = LlmRunOutcomeRepo().list_for_run(run_id)
    assert len(outcomes) == 1
    assert outcomes[0]["score"] == pytest.approx(1.0)
    assert outcomes[0]["notes"] == "reviewed"


def test_outcome_record_keeps_distinct_kinds(tracking_db):
    run_id = LlmRunRepo().record(agent_type="grader", model="claude-opus-4-7")
    LlmRunOutcomeRepo().record(run_id, outcome_kind="draft_accepted", score=1.0)
    LlmRunOutcomeRepo().record(run_id, outcome_kind="draft_edited", score=0.7)
    outcomes = LlmRunOutcomeRepo().list_for_run(run_id)
    kinds = {o["outcome_kind"] for o in outcomes}
    assert kinds == {"draft_accepted", "draft_edited"}


# ---------------------------------------------------------------------------
# LlmRunValidationRepo
# ---------------------------------------------------------------------------


def test_validation_record_roundtrip(tracking_db):
    run_id = LlmRunRepo().record(agent_type="drafter", model="claude-opus-4-7")
    LlmRunValidationRepo().record(
        run_id,
        validator="post_hoc_fact_check",
        passed=False,
        severity="warn",
        details={"flagged_claims": 2},
    )
    rows = LlmRunValidationRepo().list_for_run(run_id)
    assert len(rows) == 1
    assert rows[0]["validator"] == "post_hoc_fact_check"
    assert rows[0]["passed"] is False
    assert rows[0]["severity"] == "warn"
    assert rows[0]["details"] == {"flagged_claims": 2}


def test_validation_record_replaces_same_validator(tracking_db):
    run_id = LlmRunRepo().record(agent_type="drafter", model="claude-opus-4-7")
    LlmRunValidationRepo().record(run_id, validator="x", passed=False)
    LlmRunValidationRepo().record(run_id, validator="x", passed=True)
    rows = LlmRunValidationRepo().list_for_run(run_id)
    assert len(rows) == 1
    assert rows[0]["passed"] is True


def test_validation_record_retries_on_mysql_deadlock(tracking_db, monkeypatch):
    """First-try MySQL 1213 deadlock is swallowed; the row lands on retry."""
    from sqlalchemy.exc import OperationalError

    # Speed up the exponential-jitter backoff in the test.
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *_a, **_k: None)

    run_id = LlmRunRepo().record(agent_type="drafter", model="claude-opus-4-7")

    orig = LlmRunValidationRepo._record_once
    calls = {"n": 0}

    def flaky(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError(
                "INSERT INTO llm_run_validations ...",
                {},
                Exception(
                    "(1213, 'Deadlock found when trying to get lock; "
                    "try restarting transaction')"
                ),
            )
        return orig(self, *args, **kwargs)

    monkeypatch.setattr(LlmRunValidationRepo, "_record_once", flaky)

    # The caller MUST NOT see the exception.
    LlmRunValidationRepo().record(
        run_id, validator="deadlock_retry", passed=True, severity="info",
    )

    assert calls["n"] == 2  # first call deadlocked, retry succeeded
    rows = LlmRunValidationRepo().list_for_run(run_id)
    assert len(rows) == 1
    assert rows[0]["validator"] == "deadlock_retry"
    assert rows[0]["passed"] is True


def test_validation_record_gives_up_after_three_deadlocks(tracking_db, monkeypatch):
    """Persistent deadlocks surface to the caller after 3 attempts."""
    from sqlalchemy.exc import OperationalError

    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *_a, **_k: None)

    run_id = LlmRunRepo().record(agent_type="drafter", model="claude-opus-4-7")
    calls = {"n": 0}

    def always_deadlock(self, *args, **kwargs):
        calls["n"] += 1
        raise OperationalError(
            "INSERT INTO llm_run_validations ...",
            {},
            Exception(
                "(1213, 'Deadlock found when trying to get lock; "
                "try restarting transaction')"
            ),
        )

    monkeypatch.setattr(LlmRunValidationRepo, "_record_once", always_deadlock)

    with pytest.raises(OperationalError):
        LlmRunValidationRepo().record(
            run_id, validator="always_deadlock", passed=True,
        )

    assert calls["n"] == 3


def test_validation_record_does_not_retry_non_deadlock_errors(
    tracking_db, monkeypatch,
):
    """Unrelated OperationalErrors must not trigger a retry loop."""
    from sqlalchemy.exc import OperationalError

    run_id = LlmRunRepo().record(agent_type="drafter", model="claude-opus-4-7")
    calls = {"n": 0}

    def syntax_err(self, *args, **kwargs):
        calls["n"] += 1
        raise OperationalError(
            "INSERT INTO llm_run_validations ...",
            {},
            Exception("(1064, 'You have an error in your SQL syntax')"),
        )

    monkeypatch.setattr(LlmRunValidationRepo, "_record_once", syntax_err)

    with pytest.raises(OperationalError):
        LlmRunValidationRepo().record(
            run_id, validator="syntax_err", passed=True,
        )

    assert calls["n"] == 1  # no retry attempted


# ---------------------------------------------------------------------------
# LlmRunLinkRepo
# ---------------------------------------------------------------------------


def test_link_creates_relation(tracking_db):
    a = LlmRunRepo().record(agent_type="drafter", model="claude-opus-4-7")
    b = LlmRunRepo().record(agent_type="drafter", model="claude-opus-4-7")
    LlmRunLinkRepo().link(parent_id=a, child_id=b, relation="critic")
    children = LlmRunLinkRepo().children(a)
    assert len(children) == 1
    assert children[0]["child_run_id"] == b
    assert children[0]["relation"] == "critic"


def test_link_is_idempotent(tracking_db):
    a = LlmRunRepo().record(agent_type="drafter", model="claude-opus-4-7")
    b = LlmRunRepo().record(agent_type="drafter", model="claude-opus-4-7")
    LlmRunLinkRepo().link(parent_id=a, child_id=b, relation="retry")
    LlmRunLinkRepo().link(parent_id=a, child_id=b, relation="retry")
    assert len(LlmRunLinkRepo().children(a)) == 1


def test_link_distinct_relations_coexist(tracking_db):
    a = LlmRunRepo().record(agent_type="drafter", model="claude-opus-4-7")
    b = LlmRunRepo().record(agent_type="drafter", model="claude-opus-4-7")
    LlmRunLinkRepo().link(parent_id=a, child_id=b, relation="retry")
    LlmRunLinkRepo().link(parent_id=a, child_id=b, relation="critic")
    relations = {c["relation"] for c in LlmRunLinkRepo().children(a)}
    assert relations == {"retry", "critic"}


def test_link_children_filter_by_relation(tracking_db):
    a = LlmRunRepo().record(agent_type="drafter", model="claude-opus-4-7")
    b = LlmRunRepo().record(agent_type="drafter", model="claude-opus-4-7")
    c = LlmRunRepo().record(agent_type="drafter", model="claude-opus-4-7")
    LlmRunLinkRepo().link(parent_id=a, child_id=b, relation="retry")
    LlmRunLinkRepo().link(parent_id=a, child_id=c, relation="critic")
    retries = LlmRunLinkRepo().children(a, relation="retry")
    assert {r["child_run_id"] for r in retries} == {b}


def test_link_parents_lookup(tracking_db):
    a = LlmRunRepo().record(agent_type="drafter", model="claude-opus-4-7")
    b = LlmRunRepo().record(agent_type="drafter", model="claude-opus-4-7")
    LlmRunLinkRepo().link(parent_id=a, child_id=b, relation="retry")
    parents = LlmRunLinkRepo().parents(b)
    assert {p["parent_run_id"] for p in parents} == {a}


# ---------------------------------------------------------------------------
# LlmRunStatsRepo
# ---------------------------------------------------------------------------


def test_cost_by_model_aggregates_runs(tracking_db):
    LlmRunRepo().record(
        agent_type="drafter",
        model="claude-opus-4-7",
        usage={
            "prompt_tokens": 1000,
            "cache_read_tokens": 200,
            "completion_tokens": 500,
            "reasoning_tokens": 100,
            "cost_usd": 0.01,
        },
    )
    LlmRunRepo().record(
        agent_type="drafter",
        model="claude-opus-4-7",
        usage={
            "prompt_tokens": 1500,
            "cache_read_tokens": 300,
            "completion_tokens": 700,
            "cost_usd": 0.02,
        },
    )
    LlmRunRepo().record(
        agent_type="drafter",
        model="openai/gpt-4o",
        usage={
            "prompt_tokens": 800,
            "completion_tokens": 400,
            "cost_usd": 0.005,
        },
    )

    since = dt.datetime(2020, 1, 1)
    until = dt.datetime(2099, 1, 1)
    rows = LlmRunStatsRepo().cost_by_model(since, until)
    by_model = {r["model"]: r for r in rows}

    opus = by_model["claude-opus-4-7"]
    assert opus["runs"] == 2
    assert float(opus["total_cost_usd"]) == pytest.approx(0.03)
    assert opus["billable_input"] == (1000 - 200) + (1500 - 300)
    assert opus["cached_input"] == 500
    assert opus["output"] == 1200
    assert opus["reasoning"] == 100

    gpt = by_model["openai/gpt-4o"]
    assert gpt["runs"] == 1
    assert float(gpt["total_cost_usd"]) == pytest.approx(0.005)


def test_cost_by_model_excludes_runs_outside_window(tracking_db):
    """Runs outside [since, until) must not appear."""
    LlmRunRepo().record(
        agent_type="drafter",
        model="claude-opus-4-7",
        usage={"cost_usd": 0.01},
    )
    # Window in the distant past — should yield no rows
    since = dt.datetime(1999, 1, 1)
    until = dt.datetime(2000, 1, 1)
    rows = LlmRunStatsRepo().cost_by_model(since, until)
    assert rows == []


def test_halluc_rate_by_prompt(tracking_db):
    today = dt.date(2026, 4, 16)
    # Create prompt explicitly so we control prompt_id
    with tracking_db.begin() as conn:
        agent_id = conn.execute(
            s.llm_agent_types.insert().values(slug="searcher")
        ).inserted_primary_key[0]
        conn.execute(
            s.llm_models.insert().values(name="perplexity/sonar-pro")
        )
        prompt_id = conn.execute(
            s.llm_prompts.insert().values(
                agent_type_id=agent_id,
                part="system",
                version=3,
                content="Search prompt v3",
                effective_date=today,
            )
        ).inserted_primary_key[0]

    # Two passing and one failing url_hallucination check on prompt v3
    for passed in (True, True, False):
        run_id = LlmRunRepo().record(
            agent_type="searcher",
            model="perplexity/sonar-pro",
            system_prompt_id=prompt_id,
            usage={"cost_usd": 0.001},
        )
        LlmRunValidationRepo().record(
            run_id, validator="url_hallucination", passed=passed
        )

    since = dt.datetime(2020, 1, 1)
    until = dt.datetime(2099, 1, 1)
    rows = LlmRunStatsRepo().halluc_rate_by_prompt("searcher", since, until)
    assert len(rows) == 1
    row = rows[0]
    assert row["prompt_version"] == 3
    assert row["model"] == "perplexity/sonar-pro"
    assert row["runs"] == 3
    assert row["halluc_rate"] == pytest.approx(1 / 3)


def test_retry_success_rate(tracking_db):
    # Two retry chains: one child succeeds, one fails
    parent_a = LlmRunRepo().record(
        agent_type="drafter",
        model="claude-opus-4-7",
        usage={"cost_usd": 0.01},
    )
    LlmRunRepo().record(
        agent_type="drafter",
        model="claude-opus-4-7",
        usage={"cost_usd": 0.02},
        parent_run=(parent_a, "retry"),
        status="success",
    )
    parent_b = LlmRunRepo().record(
        agent_type="drafter",
        model="claude-opus-4-7",
        usage={"cost_usd": 0.01},
    )
    LlmRunRepo().record(
        agent_type="drafter",
        model="claude-opus-4-7",
        usage={"cost_usd": 0.03},
        parent_run=(parent_b, "retry"),
        status="failed",
    )

    since = dt.datetime(2020, 1, 1)
    until = dt.datetime(2099, 1, 1)
    rows = LlmRunStatsRepo().retry_success_rate(since, until)
    by_relation = {r["relation"]: r for r in rows}

    retry = by_relation["retry"]
    assert retry["chains"] == 2
    assert retry["child_success_rate"] == pytest.approx(0.5)


def test_runs_with_all_tags_intersection(tracking_db):
    a = LlmRunRepo().record(
        agent_type="drafter",
        model="claude-opus-4-7",
        tags=["env:prod", "experiment:x"],
    )
    b = LlmRunRepo().record(
        agent_type="drafter",
        model="claude-opus-4-7",
        tags=["env:prod"],
    )
    c = LlmRunRepo().record(
        agent_type="drafter",
        model="claude-opus-4-7",
        tags=["env:prod", "experiment:x", "cohort:1"],
    )
    matched = set(LlmRunStatsRepo().runs_with_all_tags(["env:prod", "experiment:x"]))
    assert matched == {a, c}
    assert b not in matched


def test_runs_with_all_tags_empty_input_returns_empty(tracking_db):
    assert LlmRunStatsRepo().runs_with_all_tags([]) == []


# ---------------------------------------------------------------------------
# purge_old_payloads
# ---------------------------------------------------------------------------


def _backdate_run(engine, run_id: int, when: dt.datetime) -> None:
    """Force a run's created_at into the past so the purge cutoff can see it."""
    with engine.begin() as conn:
        conn.execute(
            s.llm_runs.update()
            .where(s.llm_runs.c.id == run_id)
            .values(created_at=when)
        )


def test_purge_deletes_old_payload(tracking_db):
    run_id = LlmRunRepo().record(
        agent_type="drafter",
        model="claude-opus-4-7",
        rendered_prompts=("sys", "usr"),
        raw_response="response",
    )
    _backdate_run(tracking_db, run_id, dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=120))

    deleted = purge_old_payloads(older_than_days=90)
    assert deleted == 1
    assert LlmRunRepo().get_with_payload(run_id)["payload"] is None
    # Parent run row must survive
    assert LlmRunRepo().get(run_id) is not None


def test_purge_keeps_recent_payload(tracking_db):
    run_id = LlmRunRepo().record(
        agent_type="drafter",
        model="claude-opus-4-7",
        rendered_prompts=("sys", "usr"),
    )
    _backdate_run(tracking_db, run_id, dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=10))
    deleted = purge_old_payloads(older_than_days=90)
    assert deleted == 0
    assert LlmRunRepo().get_with_payload(run_id)["payload"] is not None


def test_purge_keeps_flagged_runs_by_default(tracking_db):
    """A failed run's payload should survive purge unless keep_flagged=False."""
    run_id = LlmRunRepo().record(
        agent_type="drafter",
        model="claude-opus-4-7",
        rendered_prompts=("sys", "usr"),
        status="failed",
    )
    _backdate_run(tracking_db, run_id, dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=120))
    deleted = purge_old_payloads(older_than_days=90)
    assert deleted == 0
    assert LlmRunRepo().get_with_payload(run_id)["payload"] is not None


def test_purge_keeps_payload_with_failed_validation(tracking_db):
    run_id = LlmRunRepo().record(
        agent_type="drafter",
        model="claude-opus-4-7",
        rendered_prompts=("sys", "usr"),
        validations=[("url_hallucination", False, "warn", None)],
    )
    _backdate_run(tracking_db, run_id, dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=120))
    deleted = purge_old_payloads(older_than_days=90)
    assert deleted == 0


def test_purge_keep_flagged_false_drops_everything(tracking_db):
    run_id = LlmRunRepo().record(
        agent_type="drafter",
        model="claude-opus-4-7",
        rendered_prompts=("sys", "usr"),
        status="failed",
    )
    _backdate_run(tracking_db, run_id, dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=120))
    deleted = purge_old_payloads(older_than_days=90, keep_flagged=False)
    assert deleted == 1


def test_purge_negative_age_raises(tracking_db):
    with pytest.raises(ValueError):
        purge_old_payloads(older_than_days=-1)
