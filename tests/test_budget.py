"""Tests for pf_core.budget — schema, config, checks, projection, snapshots."""

from __future__ import annotations

import datetime as dt
import logging
import time

import pytest

from pf_core.budget import (
    ALL_BUDGET_TABLES,
    BudgetRepo,
    BudgetSnapshotRepo,
    CostBudgetExceeded,
    CostRateRepo,
    aggregate_spent,
    check_budget,
    clear_config_cache,
    compute_period_end,
    compute_period_start,
    project_cost,
    record_blocked_run,
    record_override,
    refresh_snapshots,
    sync_budgets_from_yaml,
)
from pf_core.budget.check import _clear_no_scope_state, _clear_threshold_state
from pf_core.budget.config import _flatten_scopes, load_yaml
from pf_core.exceptions import ConfigurationError
from pf_core.llm.tracking import (
    LlmRunRepo,
    clear_resolver_caches,
    metadata,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset():
    clear_resolver_caches()
    clear_config_cache()
    _clear_threshold_state()
    _clear_no_scope_state()
    yield
    clear_resolver_caches()
    clear_config_cache()
    _clear_threshold_state()
    _clear_no_scope_state()


@pytest.fixture()
def budget_db(pf_engine):
    metadata.create_all(pf_engine)
    yield pf_engine
    metadata.drop_all(pf_engine)


def _insert_budget(
    *,
    scope_kind: str,
    scope_value: str | None,
    period: str,
    limit_usd: float,
    action: str = "block",
    soft_thresholds=None,
) -> int:
    """Insert a budget row directly (bypasses sync so multiple calls are additive)."""
    from pf_core.budget._schema import llm_budgets
    from pf_core.db.connection import transaction

    with transaction() as conn:
        result = conn.execute(
            llm_budgets.insert().values(
                scope_kind=scope_kind,
                scope_value=scope_value,
                period=period,
                limit_usd=limit_usd,
                action=action,
                soft_thresholds=soft_thresholds,
                enabled=True,
            )
        )
        return int(result.inserted_primary_key[0])


def _spend(agent_type: str, model: str, cost: float, status: str = "success") -> int:
    """Insert an llm_runs row with the given cost."""
    return LlmRunRepo().record(
        agent_type=agent_type,
        model=model,
        usage={"cost_usd": cost, "prompt_tokens": 100, "completion_tokens": 50},
        status=status,
    )


# ---------------------------------------------------------------------------
# Schema & table registration
# ---------------------------------------------------------------------------


def test_budget_tables_on_shared_metadata():
    names = {t.name for t in ALL_BUDGET_TABLES}
    assert names == {"llm_budgets", "llm_budget_snapshots", "llm_cost_rates"}
    assert all(t.name in metadata.tables for t in ALL_BUDGET_TABLES)


def test_create_all_includes_budget_tables(pf_engine):
    metadata.create_all(pf_engine)
    with pf_engine.connect() as conn:
        from sqlalchemy import inspect

        names = set(inspect(conn).get_table_names())
    assert {"llm_budgets", "llm_budget_snapshots", "llm_cost_rates"} <= names


# ---------------------------------------------------------------------------
# Period helpers
# ---------------------------------------------------------------------------


def test_compute_period_start_daily():
    now = dt.datetime(2026, 4, 19, 14, 30, tzinfo=dt.timezone.utc)
    assert compute_period_start("daily", now) == dt.date(2026, 4, 19)


def test_compute_period_start_monthly():
    now = dt.datetime(2026, 4, 19, 14, 30, tzinfo=dt.timezone.utc)
    assert compute_period_start("monthly", now) == dt.date(2026, 4, 1)


def test_compute_period_end_daily():
    assert compute_period_end("daily", dt.date(2026, 4, 19)) == dt.date(2026, 4, 20)


def test_compute_period_end_monthly_december_rollover():
    assert compute_period_end("monthly", dt.date(2026, 12, 1)) == dt.date(2027, 1, 1)


def test_compute_period_start_rejects_unknown():
    with pytest.raises(ValueError):
        compute_period_start("weekly")


# ---------------------------------------------------------------------------
# BudgetRepo sync / find
# ---------------------------------------------------------------------------


def test_sync_inserts_new_budget(budget_db):
    repo = BudgetRepo()
    counts = repo.sync_from_desired(
        [{"scope_kind": "global", "scope_value": None, "period": "daily", "limit_usd": 50.0}]
    )
    assert counts["inserted"] == 1
    found = repo.find(scope_kind="global", scope_value=None, period="daily")
    assert found is not None
    assert float(found["limit_usd"]) == 50.0
    assert found["enabled"] is True


def test_sync_updates_existing_budget(budget_db):
    repo = BudgetRepo()
    repo.sync_from_desired(
        [{"scope_kind": "agent", "scope_value": "drafter", "period": "daily", "limit_usd": 10.0}]
    )
    counts = repo.sync_from_desired(
        [{"scope_kind": "agent", "scope_value": "drafter", "period": "daily", "limit_usd": 25.0}]
    )
    assert counts["updated"] == 1
    row = repo.find(scope_kind="agent", scope_value="drafter", period="daily")
    assert float(row["limit_usd"]) == 25.0


def test_sync_disables_missing_budget(budget_db):
    repo = BudgetRepo()
    repo.sync_from_desired(
        [
            {"scope_kind": "agent", "scope_value": "drafter", "period": "daily", "limit_usd": 10.0},
            {"scope_kind": "agent", "scope_value": "critic", "period": "daily", "limit_usd": 5.0},
        ]
    )
    # Remove critic
    counts = repo.sync_from_desired(
        [{"scope_kind": "agent", "scope_value": "drafter", "period": "daily", "limit_usd": 10.0}]
    )
    assert counts["disabled"] == 1
    critic = repo.find(scope_kind="agent", scope_value="critic", period="daily")
    assert critic is not None
    assert critic["enabled"] is False  # preserved, not deleted


def test_list_for_scopes_includes_global(budget_db):
    _insert_budget(scope_kind="global", scope_value=None, period="daily", limit_usd=100.0)
    _insert_budget(scope_kind="agent", scope_value="drafter", period="daily", limit_usd=10.0)

    rows = BudgetRepo().list_for_scopes(agent_type="drafter")
    kinds = {r["scope_kind"] for r in rows}
    assert kinds == {"global", "agent"}


def test_list_for_scopes_filters_by_tag(budget_db):
    _insert_budget(scope_kind="tag", scope_value="experiment:opus47", period="monthly", limit_usd=100.0)
    rows = BudgetRepo().list_for_scopes(tags=["experiment:opus47"])
    assert len(rows) == 1
    assert rows[0]["scope_value"] == "experiment:opus47"


# ---------------------------------------------------------------------------
# CostRateRepo + projection
# ---------------------------------------------------------------------------


def test_cost_rate_upsert_and_effective(budget_db):
    repo = CostRateRepo()
    repo.upsert(
        model="claude-opus-4-7",
        input_per_1k=0.015,
        output_per_1k=0.075,
        effective_from=dt.date(2026, 1, 1),
    )
    row = repo.get_effective(model="claude-opus-4-7", on_date=dt.date(2026, 4, 19))
    assert row is not None
    assert float(row["input_per_1k"]) == 0.015


def test_cost_rate_respects_effective_to(budget_db):
    repo = CostRateRepo()
    repo.upsert(
        model="claude-opus-4-7",
        input_per_1k=0.01,
        output_per_1k=0.05,
        effective_from=dt.date(2026, 1, 1),
        effective_to=dt.date(2026, 3, 31),
    )
    row = repo.get_effective(model="claude-opus-4-7", on_date=dt.date(2026, 4, 19))
    assert row is None


def test_project_cost_with_rate(budget_db):
    CostRateRepo().upsert(
        model="claude-opus-4-7",
        input_per_1k=0.015,
        output_per_1k=0.075,
    )
    cost = project_cost(
        agent_type="drafter",
        model="claude-opus-4-7",
        estimated_prompt_tokens=1000,
        estimated_completion_tokens=1000,
    )
    # 1.0 * 0.015 + 1.0 * 0.075 = 0.09
    assert cost == pytest.approx(0.09, abs=1e-6)


def test_project_cost_fallback_to_recent_mean(budget_db):
    # No cost rate on file — falls back to recent runs mean
    _spend("drafter", "unknown-model", 0.25)
    _spend("drafter", "unknown-model", 0.35)
    cost = project_cost(
        agent_type="drafter",
        model="unknown-model",
        estimated_prompt_tokens=500,
        estimated_completion_tokens=500,
    )
    assert cost == pytest.approx(0.30, abs=0.01)


def test_project_cost_no_data_returns_zero(budget_db):
    cost = project_cost(agent_type="novel", model="novel-model")
    assert cost == 0.0


# ---------------------------------------------------------------------------
# check_budget
# ---------------------------------------------------------------------------


def test_cost_budget_exceeded_is_flow_exception():
    from pf_core.exceptions import FlowException

    exc = CostBudgetExceeded(
        scope_kind="agent",
        scope_value="drafter",
        period="daily",
        limit_usd=10.0,
        spent_usd=9.5,
        projected_usd=1.0,
    )
    assert isinstance(exc, FlowException)
    # Public shape unchanged: attributes + message survive the re-parenting
    assert exc.scope_kind == "agent"
    assert exc.scope_value == "drafter"
    assert exc.period == "daily"
    assert exc.limit_usd == 10.0
    assert exc.spent_usd == 9.5
    assert exc.projected_usd == 1.0
    assert "budget exceeded: agent:drafter (daily)" in str(exc)


def test_cost_budget_exceeded_caught_by_name():
    with pytest.raises(CostBudgetExceeded):
        raise CostBudgetExceeded(
            scope_kind="global",
            scope_value=None,
            period="monthly",
            limit_usd=100.0,
            spent_usd=99.0,
            projected_usd=2.0,
        )


def _event_records(caplog, event: str) -> list:
    """Records carrying a structlog event_dict whose ``event`` key matches."""
    return [
        r
        for r in caplog.records
        if isinstance(getattr(r, "msg", None), dict) and r.msg.get("event") == event
    ]


def _no_scope_records(caplog) -> list:
    return _event_records(caplog, "budget_no_scopes_matched")


def test_check_budget_passes_when_no_budgets(budget_db, caplog):
    with caplog.at_level(logging.DEBUG, logger="pf_core.budget.check"):
        check_budget(agent_type="drafter", projected_cost_usd=5.00)  # no raise
    records = _no_scope_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].msg["agent_type"] == "drafter"


def test_check_budget_no_scope_warning_dedupes_then_drops_to_debug(budget_db, caplog):
    with caplog.at_level(logging.DEBUG, logger="pf_core.budget.check"):
        check_budget(agent_type="drafter", projected_cost_usd=1.0)
        check_budget(agent_type="drafter", projected_cost_usd=1.0, job_id=7)
        check_budget(agent_type="critic", projected_cost_usd=1.0)
    levels = [r.levelno for r in _no_scope_records(caplog)]
    # Same agent twice (differing only by the unbounded job_id) warns once.
    assert levels == [logging.WARNING, logging.DEBUG, logging.WARNING]


def test_check_budget_no_scope_log_names_every_scope_looked_for(budget_db, caplog):
    with caplog.at_level(logging.WARNING, logger="pf_core.budget.check"):
        check_budget(
            agent_type="drafter",
            projected_cost_usd=1.0,
            job_kind="draft_batch",
            job_id=90210,
            tags=["experiment:opus47"],
        )
    payload = _no_scope_records(caplog)[0].msg
    assert payload["agent_type"] == "drafter"
    assert payload["job_kind"] == "draft_batch"
    assert payload["job_id"] == 90210
    assert payload["tags"] == ["experiment:opus47"]


def test_check_budget_no_scope_warn_set_is_bounded(budget_db, caplog):
    from pf_core.budget.check import _NO_SCOPE_WARN_LIMIT, _NO_SCOPE_WARNED

    with caplog.at_level(logging.DEBUG, logger="pf_core.budget.check"):
        for i in range(_NO_SCOPE_WARN_LIMIT + 50):
            check_budget(projected_cost_usd=0.1, tags=[f"run:{i}"])
    assert len(_NO_SCOPE_WARNED) == _NO_SCOPE_WARN_LIMIT
    warned = [r for r in _no_scope_records(caplog) if r.levelno == logging.WARNING]
    assert len(warned) == _NO_SCOPE_WARN_LIMIT


def test_check_budget_silent_when_a_scope_matches(budget_db, caplog):
    _insert_budget(scope_kind="agent", scope_value="drafter", period="daily", limit_usd=20.0)
    with caplog.at_level(logging.DEBUG, logger="pf_core.budget.check"):
        check_budget(agent_type="drafter", projected_cost_usd=1.0)
    assert _no_scope_records(caplog) == []


def test_check_budget_passes_under_limit(budget_db):
    _insert_budget(scope_kind="agent", scope_value="drafter", period="daily", limit_usd=20.0)
    _spend("drafter", "claude-opus-4-7", 5.0)
    check_budget(agent_type="drafter", projected_cost_usd=1.0)  # 5 + 1 < 20


def test_check_budget_blocks_over_limit(budget_db):
    _insert_budget(scope_kind="agent", scope_value="drafter", period="daily", limit_usd=10.0)
    _spend("drafter", "claude-opus-4-7", 9.5)
    with pytest.raises(CostBudgetExceeded) as excinfo:
        check_budget(agent_type="drafter", projected_cost_usd=1.0)
    exc = excinfo.value
    assert exc.scope_kind == "agent"
    assert exc.scope_value == "drafter"
    assert exc.limit_usd == 10.0
    assert exc.projected_usd == 1.0


def test_check_budget_warn_action_does_not_raise(budget_db):
    _insert_budget(
        scope_kind="agent", scope_value="drafter", period="daily",
        limit_usd=10.0, action="warn"
    )
    _spend("drafter", "claude-opus-4-7", 12.0)
    # warn: log only, no raise
    check_budget(agent_type="drafter", projected_cost_usd=1.0)


def test_check_budget_excludes_cache_hit_and_blocked_from_spend(budget_db):
    _insert_budget(scope_kind="agent", scope_value="drafter", period="daily", limit_usd=10.0)
    _spend("drafter", "claude-opus-4-7", 100.0, status="cache_hit")
    _spend("drafter", "claude-opus-4-7", 100.0, status="budget_blocked")
    # Those shouldn't count — should pass
    check_budget(agent_type="drafter", projected_cost_usd=5.0)


def test_check_budget_global_scope_checked(budget_db):
    _insert_budget(scope_kind="global", scope_value=None, period="daily", limit_usd=5.0)
    _spend("drafter", "claude-opus-4-7", 4.0)
    with pytest.raises(CostBudgetExceeded) as excinfo:
        check_budget(agent_type="drafter", projected_cost_usd=2.0)
    assert excinfo.value.scope_kind == "global"


def test_check_budget_override_passes(budget_db):
    _insert_budget(scope_kind="agent", scope_value="drafter", period="daily", limit_usd=1.0)
    _spend("drafter", "claude-opus-4-7", 100.0)
    # override short-circuits to pass
    check_budget(
        agent_type="drafter",
        projected_cost_usd=50.0,
        override={"reason": "incident recovery", "operator": "ops"},
    )


def test_check_budget_kill_switch(budget_db, monkeypatch):
    _insert_budget(scope_kind="agent", scope_value="drafter", period="daily", limit_usd=1.0)
    _spend("drafter", "claude-opus-4-7", 100.0)
    monkeypatch.setenv("BUDGET_ENFORCEMENT_DISABLED", "true")
    check_budget(agent_type="drafter", projected_cost_usd=50.0)  # no raise


def test_check_budget_block_beats_warn(budget_db):
    # Global (warn) + agent (block) both over; agent block raises first
    _insert_budget(
        scope_kind="global", scope_value=None, period="daily",
        limit_usd=5.0, action="warn"
    )
    _insert_budget(
        scope_kind="agent", scope_value="drafter", period="daily",
        limit_usd=10.0, action="block"
    )
    _spend("drafter", "claude-opus-4-7", 9.0)
    with pytest.raises(CostBudgetExceeded) as excinfo:
        check_budget(agent_type="drafter", projected_cost_usd=2.0)
    # order: global first — but global is warn, so it should proceed to agent (block)
    assert excinfo.value.scope_kind == "agent"


# ---------------------------------------------------------------------------
# Aggregation & snapshots
# ---------------------------------------------------------------------------


def test_aggregate_spent_sums_matching_runs(budget_db):
    _insert_budget(
        scope_kind="agent", scope_value="drafter", period="daily", limit_usd=100.0
    )
    _spend("drafter", "claude-opus-4-7", 2.5)
    _spend("drafter", "claude-opus-4-7", 3.5)
    _spend("critic", "claude-opus-4-7", 5.0)  # different agent — excluded

    budget = BudgetRepo().find(scope_kind="agent", scope_value="drafter", period="daily")
    now = dt.datetime.now(dt.timezone.utc)
    start = compute_period_start("daily", now)
    end = compute_period_end("daily", start)
    total, count = aggregate_spent(budget=budget, period_start=start, period_end=end)
    assert total == pytest.approx(6.0, abs=1e-6)
    assert count == 2


def test_refresh_snapshots_writes_snapshot(budget_db):
    _insert_budget(scope_kind="agent", scope_value="drafter", period="daily", limit_usd=50.0)
    _spend("drafter", "claude-opus-4-7", 4.25)

    n = refresh_snapshots()
    assert n == 1

    budget = BudgetRepo().find(scope_kind="agent", scope_value="drafter", period="daily")
    period_start = compute_period_start("daily")
    snap = BudgetSnapshotRepo().get(budget_id=budget["id"], period_start=period_start)
    assert snap is not None
    assert float(snap["spent_usd"]) == pytest.approx(4.25, abs=1e-6)
    assert snap["run_count"] == 1


def test_refresh_snapshots_filters_by_period(budget_db):
    _insert_budget(scope_kind="agent", scope_value="drafter", period="daily", limit_usd=10.0)
    _insert_budget(scope_kind="agent", scope_value="drafter", period="monthly", limit_usd=100.0)
    n_daily = refresh_snapshots(period="daily")
    assert n_daily == 1


# ---------------------------------------------------------------------------
# Audit — record_blocked_run + record_override
# ---------------------------------------------------------------------------


def test_record_blocked_run_writes_tagged_row(budget_db):
    exc = CostBudgetExceeded(
        scope_kind="agent",
        scope_value="drafter",
        period="daily",
        limit_usd=10.0,
        spent_usd=9.5,
        projected_usd=1.0,
    )
    run_id = record_blocked_run(agent_type="drafter", model="claude-opus-4-7", exc=exc)
    assert run_id > 0

    row = LlmRunRepo().get(run_id)
    assert row["status"] == "budget_blocked"
    assert row["cost_usd"] == 0.0

    # Check tags
    from pf_core.db.connection import transaction
    from pf_core.llm.tracking.schema import llm_run_tags

    with transaction() as conn:
        tags = [
            r[0]
            for r in conn.execute(
                llm_run_tags.select().where(llm_run_tags.c.llm_run_id == run_id)
            ).fetchall()
        ]
        # tuple rows — tag column index depends on table layout; query columns explicitly
        tags = [
            r.tag
            for r in conn.execute(
                llm_run_tags.select().where(llm_run_tags.c.llm_run_id == run_id)
            ).fetchall()
        ]
    assert "budget:blocked" in tags
    assert any(t.startswith("budget:scope=agent:drafter") for t in tags)


def test_record_override_attaches_tag_and_outcome(budget_db):
    run_id = _spend("drafter", "claude-opus-4-7", 5.0)
    record_override(run_id=run_id, reason="manual backfill", operator="ops")

    from pf_core.db.connection import transaction
    from pf_core.llm.tracking.schema import llm_run_tags

    with transaction() as conn:
        tags = [
            r.tag
            for r in conn.execute(
                llm_run_tags.select().where(llm_run_tags.c.llm_run_id == run_id)
            ).fetchall()
        ]
    assert "budget:override" in tags

    from pf_core.llm.tracking.subrepos import LlmRunOutcomeRepo

    outcomes = LlmRunOutcomeRepo().list_for_run(run_id)
    assert any(o["outcome_kind"] == "budget_override" for o in outcomes)
    assert any("manual backfill" in (o["notes"] or "") for o in outcomes)


# ---------------------------------------------------------------------------
# YAML sync
# ---------------------------------------------------------------------------


def test_sync_budgets_from_yaml(budget_db, tmp_path, monkeypatch):
    yaml_text = """
global:
  daily: 50.0
  monthly: 1000.0

agents:
  drafter:
    daily: 20.0
    monthly: 400.0
    action: block
    soft_thresholds: [0.5, 0.8]
  critic:
    daily: 5.0
"""
    cfg = tmp_path / "budgets.yaml"
    cfg.write_text(yaml_text)
    monkeypatch.setenv("BUDGET_CONFIG", str(cfg))

    counts = sync_budgets_from_yaml()
    # global: daily+monthly; drafter: daily+monthly; critic: daily = 5 rows
    assert counts["inserted"] == 5

    repo = BudgetRepo()
    found = repo.find(scope_kind="agent", scope_value="drafter", period="daily")
    assert float(found["limit_usd"]) == 20.0
    assert found["action"] == "block"


def test_sync_budgets_from_yaml_missing_file_is_noop(budget_db, tmp_path, monkeypatch):
    monkeypatch.setenv("BUDGET_CONFIG", str(tmp_path / "does_not_exist.yaml"))
    counts = sync_budgets_from_yaml()
    assert counts == {"inserted": 0, "updated": 0, "disabled": 0}


def test_sync_budgets_from_yaml_empty_file_is_noop(budget_db, tmp_path, monkeypatch):
    cfg = tmp_path / "budgets.yaml"
    cfg.write_text("# nothing declared yet\n")
    monkeypatch.setenv("BUDGET_CONFIG", str(cfg))
    counts = sync_budgets_from_yaml()
    assert counts == {"inserted": 0, "updated": 0, "disabled": 0}


def _seed_drafter_cap(tmp_path, monkeypatch, name: str = "good.yaml"):
    """Sync a single blocking 10.0/day drafter cap and return its config path."""
    cfg = tmp_path / name
    cfg.write_text("agents:\n  drafter:\n    daily: 10.0\n    action: block\n")
    monkeypatch.setenv("BUDGET_CONFIG", str(cfg))
    assert sync_budgets_from_yaml()["inserted"] == 1
    return cfg


def _assert_drafter_cap_still_enforcing():
    found = BudgetRepo().find(scope_kind="agent", scope_value="drafter", period="daily")
    assert found is not None and found["enabled"]
    _spend("drafter", "claude-opus-4-7", 9.5)
    with pytest.raises(CostBudgetExceeded):
        check_budget(agent_type="drafter", projected_cost_usd=1.0)


def test_sync_budgets_from_yaml_malformed_file_is_a_logged_noop(
    budget_db, tmp_path, monkeypatch, caplog
):
    cfg = tmp_path / "budgets.yaml"
    cfg.write_text("agents:\n  drafter:\n   daily: 20.0\n    monthly: [unclosed\n")
    monkeypatch.setenv("BUDGET_CONFIG", str(cfg))
    with caplog.at_level(logging.ERROR, logger="pf_core.budget.config"):
        counts = sync_budgets_from_yaml()
    assert counts == {"inserted": 0, "updated": 0, "disabled": 0}
    records = _event_records(caplog, "budget_config_unreadable")
    assert len(records) == 1
    assert str(cfg) in records[0].msg["error"]


def test_sync_budgets_from_yaml_non_mapping_document_is_a_logged_noop(
    budget_db, tmp_path, monkeypatch, caplog
):
    cfg = tmp_path / "budgets.yaml"
    cfg.write_text("- global\n- agents\n")
    monkeypatch.setenv("BUDGET_CONFIG", str(cfg))
    with caplog.at_level(logging.ERROR, logger="pf_core.budget.config"):
        assert sync_budgets_from_yaml() == {"inserted": 0, "updated": 0, "disabled": 0}
    assert _event_records(caplog, "budget_config_unreadable")


def test_load_yaml_still_raises_so_callers_can_fail_fast(budget_db, tmp_path, monkeypatch):
    cfg = tmp_path / "budgets.yaml"
    cfg.write_text("- global\n- agents\n")
    monkeypatch.setenv("BUDGET_CONFIG", str(cfg))
    with pytest.raises(ConfigurationError):
        load_yaml()


def test_malformed_config_does_not_disable_existing_budgets(budget_db, tmp_path, monkeypatch):
    _seed_drafter_cap(tmp_path, monkeypatch)

    bad = tmp_path / "bad.yaml"
    bad.write_text("agents:\n  drafter:\n   daily: 10.0\n    action: [oops\n")
    monkeypatch.setenv("BUDGET_CONFIG", str(bad))
    assert sync_budgets_from_yaml() == {"inserted": 0, "updated": 0, "disabled": 0}

    _assert_drafter_cap_still_enforcing()


def test_config_that_vanishes_does_not_disable_existing_budgets(
    budget_db, tmp_path, monkeypatch, caplog
):
    cfg = _seed_drafter_cap(tmp_path, monkeypatch)
    cfg.unlink()
    clear_config_cache()

    with caplog.at_level(logging.WARNING, logger="pf_core.budget.repo"):
        assert sync_budgets_from_yaml()["disabled"] == 0
    assert _event_records(caplog, "budget_sync_refused_mass_disable")

    _assert_drafter_cap_still_enforcing()


def test_mistyped_section_name_does_not_disable_existing_budgets(
    budget_db, tmp_path, monkeypatch, caplog
):
    cfg = _seed_drafter_cap(tmp_path, monkeypatch)
    # 'agent' rather than 'agents' — parses cleanly, yields zero scopes.
    cfg.write_text("agent:\n  drafter:\n    daily: 10.0\n    action: block\n")
    clear_config_cache()

    with caplog.at_level(logging.WARNING):
        assert sync_budgets_from_yaml()["disabled"] == 0
    unknown = _event_records(caplog, "budget_config_unknown_sections")
    assert unknown and unknown[0].msg["unknown"] == ["agent"]
    assert _event_records(caplog, "budget_sync_refused_mass_disable")

    _assert_drafter_cap_still_enforcing()


def test_removing_one_scope_still_disables_it(budget_db, tmp_path, monkeypatch):
    cfg = tmp_path / "budgets.yaml"
    cfg.write_text(
        "agents:\n  drafter:\n    daily: 10.0\n  critic:\n    daily: 5.0\n"
    )
    monkeypatch.setenv("BUDGET_CONFIG", str(cfg))
    assert sync_budgets_from_yaml()["inserted"] == 2

    cfg.write_text("agents:\n  drafter:\n    daily: 10.0\n")
    clear_config_cache()
    assert sync_budgets_from_yaml()["disabled"] == 1

    repo = BudgetRepo()
    assert repo.find(scope_kind="agent", scope_value="drafter", period="daily")["enabled"]
    assert not repo.find(scope_kind="agent", scope_value="critic", period="daily")["enabled"]


def test_in_place_edit_to_malformed_serves_last_good_config(
    budget_db, tmp_path, monkeypatch
):
    """The realistic shape: an operator breaks budgets.yaml under a live process."""
    cfg = _seed_drafter_cap(tmp_path, monkeypatch, name="budgets.yaml")
    monkeypatch.setenv("BUDGET_CONFIG_RELOAD_SECONDS", "0")  # reload every call
    cfg.write_text("agents:\n  drafter:\n   daily: 10.0\n    action: [oops\n")

    # Same path, warm cache -> stale-serve, so the sync re-applies the good scopes.
    counts = sync_budgets_from_yaml()
    assert counts["disabled"] == 0
    _assert_drafter_cap_still_enforcing()


@pytest.mark.parametrize(
    "document",
    [
        "agents: nope\n",
        "global:\n  - 5\n",
        "global:\n  daily: 20 USD\n",
        "agents:\n  drafter: 20.0\n",
    ],
)
def test_wrongly_shaped_config_raises_configuration_error(tmp_path, monkeypatch, document):
    cfg = tmp_path / "budgets.yaml"
    cfg.write_text(document)
    monkeypatch.setenv("BUDGET_CONFIG", str(cfg))
    with pytest.raises(ConfigurationError):
        _flatten_scopes(load_yaml())


def test_broken_symlink_is_a_config_error_not_an_absent_file(tmp_path, monkeypatch):
    cfg = tmp_path / "budgets.yaml"
    cfg.symlink_to(tmp_path / "unmounted-volume.yaml")
    monkeypatch.setenv("BUDGET_CONFIG", str(cfg))
    with pytest.raises(ConfigurationError):
        load_yaml()


# ---------------------------------------------------------------------------
# Snapshot + live delta
# ---------------------------------------------------------------------------


def test_check_budget_uses_snapshot_plus_delta(budget_db):
    _insert_budget(scope_kind="agent", scope_value="drafter", period="daily", limit_usd=10.0)
    _spend("drafter", "claude-opus-4-7", 3.0)
    time.sleep(1.1)  # ensure SQLite TIMESTAMP second-tick separation
    refresh_snapshots()  # snapshot = 3.0
    time.sleep(1.1)

    # Spend more after snapshot — delta should be picked up
    _spend("drafter", "claude-opus-4-7", 5.0)

    with pytest.raises(CostBudgetExceeded):
        # snapshot(3) + delta(5) + projected(3) = 11 > 10
        check_budget(agent_type="drafter", projected_cost_usd=3.0)


# ---------------------------------------------------------------------------
# Config reload cache
# ---------------------------------------------------------------------------


class TestConfigReloadCache:
    def test_malformed_reload_ttl_falls_back(self, tmp_path, monkeypatch):
        cfg = tmp_path / "budgets.yaml"
        cfg.write_text("global:\n  daily: 5.0\n")
        monkeypatch.setenv("BUDGET_CONFIG", str(cfg))
        monkeypatch.setenv("BUDGET_CONFIG_RELOAD_SECONDS", "not-a-number")
        from pf_core.budget.config import load_yaml

        assert load_yaml()["global"]["daily"] == 5.0

    def test_config_path_change_invalidates_immediately(self, tmp_path, monkeypatch):
        a = tmp_path / "a.yaml"
        a.write_text("global:\n  daily: 1.0\n")
        b = tmp_path / "b.yaml"
        b.write_text("global:\n  daily: 2.0\n")
        monkeypatch.setenv("BUDGET_CONFIG_RELOAD_SECONDS", "3600")
        monkeypatch.setenv("BUDGET_CONFIG", str(a))
        from pf_core.budget.config import load_yaml

        assert load_yaml()["global"]["daily"] == 1.0
        monkeypatch.setenv("BUDGET_CONFIG", str(b))
        assert load_yaml()["global"]["daily"] == 2.0

    def test_malformed_reload_keeps_last_good_config(self, tmp_path, monkeypatch):
        cfg = tmp_path / "budgets.yaml"
        cfg.write_text("global:\n  daily: 5.0\n")
        monkeypatch.setenv("BUDGET_CONFIG", str(cfg))
        monkeypatch.setenv("BUDGET_CONFIG_RELOAD_SECONDS", "0")  # reload every call
        from pf_core.budget.config import load_yaml

        assert load_yaml()["global"]["daily"] == 5.0
        cfg.write_text("global:\n  daily: 5.0\n   monthly: [oops\n")
        assert load_yaml()["global"]["daily"] == 5.0

    def test_first_load_of_a_malformed_config_raises(self, tmp_path, monkeypatch):
        cfg = tmp_path / "budgets.yaml"
        cfg.write_text("global:\n  daily: 5.0\n   monthly: [oops\n")
        monkeypatch.setenv("BUDGET_CONFIG", str(cfg))
        from pf_core.budget.config import load_yaml

        with pytest.raises(ConfigurationError):
            load_yaml()


# ---------------------------------------------------------------------------
# Unpriced models must not project as free
# ---------------------------------------------------------------------------


def test_project_cost_falls_back_to_shared_pricing_table(budget_db):
    # No llm_cost_rates row, but pf_core.pricing knows this model.
    cost = project_cost(
        agent_type="drafter",
        model="claude-opus-4-7",
        estimated_prompt_tokens=1_000_000,
        estimated_completion_tokens=0,
    )
    assert cost == pytest.approx(15.0)


def test_check_budget_blocks_call_priced_only_by_shared_table(budget_db):
    _insert_budget(scope_kind="agent", scope_value="drafter", period="daily", limit_usd=10.0)
    projected = project_cost(
        agent_type="drafter",
        model="claude-opus-4-7",
        estimated_prompt_tokens=1_000_000,
        estimated_completion_tokens=0,
    )
    with pytest.raises(CostBudgetExceeded):
        check_budget(agent_type="drafter", projected_cost_usd=projected)


def test_project_cost_warns_when_nothing_can_price_the_call(budget_db, caplog):
    with caplog.at_level(logging.WARNING, logger="pf_core.budget.check"):
        cost = project_cost(agent_type="novel", model="novel-model")
    assert cost == 0.0
    assert any("budget_projection_unknown" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Snapshot cutoff — runs created mid-refresh must not be dropped
# ---------------------------------------------------------------------------


def test_snapshot_refresh_counts_run_created_during_refresh(budget_db, monkeypatch):
    import pf_core.budget.snapshot_job as snapshot_job

    _insert_budget(scope_kind="agent", scope_value="drafter", period="daily", limit_usd=100.0)
    real_aggregate = snapshot_job.aggregate_spent

    def _aggregate_then_spend(**kwargs):
        result = real_aggregate(**kwargs)
        time.sleep(1.1)  # SQLite server timestamps tick per second
        _spend("drafter", "claude-opus-4-7", 5.0)
        return result

    monkeypatch.setattr(snapshot_job, "aggregate_spent", _aggregate_then_spend)
    snapshot_job.refresh_snapshots()

    from pf_core.budget.check import _current_spent

    budget = BudgetRepo().find(scope_kind="agent", scope_value="drafter", period="daily")
    assert _current_spent(budget) == pytest.approx(5.0, abs=1e-6)


# ---------------------------------------------------------------------------
# Scope filter — one definition for snapshot and live delta
# ---------------------------------------------------------------------------


def test_aggregate_spent_rejects_unknown_scope_kind(budget_db):
    from pf_core.exceptions import InvalidInputError

    start = compute_period_start("daily")
    budget = {"scope_kind": "cost_center", "scope_value": "eng", "period": "daily"}
    with pytest.raises(InvalidInputError):
        aggregate_spent(
            budget=budget,
            period_start=start,
            period_end=compute_period_end("daily", start),
        )


def test_live_delta_rejects_unknown_scope_kind(budget_db):
    from pf_core.budget.check import _current_spent
    from pf_core.exceptions import InvalidInputError

    _insert_budget(scope_kind="global", scope_value=None, period="daily", limit_usd=100.0)
    refresh_snapshots()
    time.sleep(1.1)
    _spend("drafter", "claude-opus-4-7", 7.0)

    budget = dict(BudgetRepo().find(scope_kind="global", scope_value=None, period="daily"))
    budget["scope_kind"] = "cost_center"
    budget["scope_value"] = "eng"
    with pytest.raises(InvalidInputError):
        _current_spent(budget)


def test_refresh_snapshots_skips_budget_with_unknown_scope_kind(budget_db):
    _insert_budget(scope_kind="cost_center", scope_value="eng", period="daily", limit_usd=1.0)
    _insert_budget(scope_kind="agent", scope_value="drafter", period="daily", limit_usd=50.0)
    _spend("drafter", "claude-opus-4-7", 2.0)

    assert refresh_snapshots() == 1

    budget = BudgetRepo().find(scope_kind="agent", scope_value="drafter", period="daily")
    snap = BudgetSnapshotRepo().get(
        budget_id=budget["id"], period_start=compute_period_start("daily")
    )
    assert float(snap["spent_usd"]) == pytest.approx(2.0, abs=1e-6)


# ---------------------------------------------------------------------------
# Soft-threshold dedupe key
# ---------------------------------------------------------------------------


def test_threshold_dedupe_key_is_the_utc_period_start():
    from pf_core.budget.check import _THRESHOLD_FIRED, _maybe_log_threshold

    for period in ("daily", "monthly"):
        _clear_threshold_state()
        budget = {
            "id": 42,
            "scope_kind": "global",
            "scope_value": None,
            "period": period,
            "limit_usd": 10.0,
            "soft_thresholds": [0.5],
        }
        _maybe_log_threshold(budget=budget, spent_before=0.0, spent_after=6.0)
        assert _THRESHOLD_FIRED == {(42, str(compute_period_start(period)), 0.5)}
