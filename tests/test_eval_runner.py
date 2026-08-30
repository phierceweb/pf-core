"""Tests for pf_core.eval._runner (EvalRunner) and _report (EvalReport/EvalResult)."""

from __future__ import annotations

import pytest

from pf_core.eval._config import AgentEvalConfig, EvalConfig, clear_config_cache
from pf_core.eval._report import EvalReport, EvalResult
from pf_core.llm.tracking import clear_resolver_caches, metadata


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset():
    clear_resolver_caches()
    clear_config_cache()
    yield
    clear_resolver_caches()
    clear_config_cache()


@pytest.fixture
def tracking_db(pf_engine):
    metadata.create_all(pf_engine)
    yield pf_engine
    metadata.drop_all(pf_engine)


# ---------------------------------------------------------------------------
# EvalResult
# ---------------------------------------------------------------------------


def test_eval_result_fields():
    r = EvalResult(golden_id=1, run_id=10, score=0.9, passed=True)
    assert r.golden_id == 1
    assert r.run_id == 10
    assert r.score == 0.9
    assert r.passed is True
    assert r.error is None


def test_eval_result_with_error():
    r = EvalResult(golden_id=1, run_id=-1, score=0.0, passed=False, error="timeout")
    assert r.error == "timeout"


# ---------------------------------------------------------------------------
# EvalReport
# ---------------------------------------------------------------------------


def _make_report(scores: list[float], threshold: float = 0.85) -> EvalReport:
    cfg = AgentEvalConfig(pass_threshold=threshold)
    results = [
        EvalResult(
            golden_id=i + 1,
            run_id=100 + i,
            score=s,
            passed=(s >= threshold),
        )
        for i, s in enumerate(scores)
    ]
    return EvalReport(
        agent_type="drafter",
        version="golden_v1",
        target={"model": "test-model"},
        results=results,
        cfg=cfg,
    )


def test_report_mean_score():
    report = _make_report([0.8, 0.9, 1.0])
    assert report.mean_score == pytest.approx(0.9)


def test_report_mean_score_empty():
    report = _make_report([])
    assert report.mean_score == 0.0


def test_report_passed():
    report = _make_report([0.9, 0.95, 0.87], threshold=0.85)
    assert report.passed is True


def test_report_failed():
    report = _make_report([0.5, 0.6, 0.7], threshold=0.85)
    assert report.passed is False


def test_report_pass_rate():
    report = _make_report([1.0, 0.5, 0.0], threshold=0.85)
    assert report.pass_rate == pytest.approx(1 / 3)


def test_report_excludes_error_runs_from_mean():
    cfg = AgentEvalConfig(pass_threshold=0.5)
    results = [
        EvalResult(golden_id=1, run_id=10, score=1.0, passed=True),
        EvalResult(golden_id=2, run_id=-1, score=0.0, passed=False, error="crash"),
    ]
    report = EvalReport(
        agent_type="drafter",
        version="golden_v1",
        target={},
        results=results,
        cfg=cfg,
    )
    assert report.mean_score == 1.0


def test_report_summary_contains_key_info():
    report = _make_report([0.9, 0.95], threshold=0.85)
    summary = report.summary()
    assert "drafter" in summary
    assert "golden_v1" in summary
    assert "PASS" in summary


def test_report_summary_fail_mode():
    report = _make_report([0.4, 0.5], threshold=0.85)
    assert "FAIL" in report.summary()


def test_report_write_html(tmp_path):
    report = _make_report([0.8, 0.9, 0.7])
    out_path = tmp_path / "report.html"
    report.write_html(str(out_path))
    html = out_path.read_text()
    assert "<!DOCTYPE html>" in html
    assert "drafter" in html
    assert "golden_v1" in html


# ---------------------------------------------------------------------------
# EvalRunner — integration test with mocked _run_single_replay
# ---------------------------------------------------------------------------


def test_eval_runner_run_dispatches_to_each_golden(tracking_db, monkeypatch):
    """EvalRunner.run() calls _run_single_replay once per golden member."""
    from pf_core.eval._golden import GoldenSetRepo
    from pf_core.eval._runner import EvalRunner
    from pf_core.llm.tracking import llm_agent_types, llm_models, llm_run_payloads, llm_runs

    # Seed two golden runs
    golden_ids = []
    with tracking_db.begin() as conn:
        mid = conn.execute(
            llm_models.insert().values(name="orig-model-runner-test")
        ).inserted_primary_key[0]
        aid = conn.execute(
            llm_agent_types.insert().values(slug="runner_test_agent")
        ).inserted_primary_key[0]
        for i in range(2):
            run_id = conn.execute(
                llm_runs.insert().values(agent_type_id=aid, model_id=mid, status="success")
            ).inserted_primary_key[0]
            conn.execute(
                llm_run_payloads.insert().values(
                    llm_run_id=run_id,
                    rendered_user=f"Question {i}",
                    parsed_output={"answer": i},
                )
            )
            golden_ids.append(run_id)

    repo = GoldenSetRepo()
    for gid in golden_ids:
        repo.add(gid, version="runner_test_v1")

    calls = []

    def _fake_replay(self, *, golden_id, **kwargs):
        calls.append(golden_id)
        return EvalResult(golden_id=golden_id, run_id=9000 + golden_id, score=1.0, passed=True)

    monkeypatch.setattr(EvalRunner, "_run_single_replay", _fake_replay)

    cfg = EvalConfig({"agents": {"runner_test_agent": {"compare": "structured_diff"}}})
    runner = EvalRunner.__new__(EvalRunner)
    runner._cfg = cfg

    report = runner.run(
        version="runner_test_v1",
        agent_type="runner_test_agent",
        target={"model": "fake"},
    )

    assert sorted(calls) == sorted(golden_ids)
    assert len(report.results) == 2
    assert report.mean_score == 1.0
    assert report.passed is True


def test_replay_runs_do_not_join_the_golden_set(tracking_db, monkeypatch):
    """A replay must not carry the golden-membership tag (``eval:<version>``)
    — that tag IS membership, so copying it onto replays makes every eval
    contaminate its own golden set (replays-of-replays on the next run)."""
    from sqlalchemy import select

    from pf_core.eval._golden import GoldenSetRepo
    from pf_core.eval._runner import EvalRunner
    from pf_core.llm.tracking import (
        llm_agent_types,
        llm_models,
        llm_run_payloads,
        llm_run_tags,
        llm_runs,
    )

    with tracking_db.begin() as conn:
        mid = conn.execute(llm_models.insert().values(name="tagfix-model")).inserted_primary_key[0]
        aid = conn.execute(
            llm_agent_types.insert().values(slug="tagfix_agent")
        ).inserted_primary_key[0]
        gid = conn.execute(
            llm_runs.insert().values(agent_type_id=aid, model_id=mid, status="success")
        ).inserted_primary_key[0]
        conn.execute(
            llm_run_payloads.insert().values(
                llm_run_id=gid, rendered_user="Q", parsed_output={"answer": 1}
            )
        )

    repo = GoldenSetRepo()
    repo.add(gid, version="tagfix_v1")

    class _FakeClient:
        def chat(self, *, messages, model="", **kwargs):
            return '{"answer": 1}', {"duration_ms": 1}

    monkeypatch.setattr("pf_core.clients.openrouter.get_client", lambda *a, **k: _FakeClient())

    cfg = EvalConfig({"agents": {"tagfix_agent": {"compare": "structured_diff"}}})
    runner = EvalRunner.__new__(EvalRunner)
    runner._cfg = cfg

    report = runner.run(
        version="tagfix_v1",
        agent_type="tagfix_agent",
        target={"model": "candidate-model"},
        tag_as="experiment:tagfix",
    )
    assert report.results and report.results[0].score == 1.0

    replay_id = report.results[0].run_id
    with tracking_db.connect() as conn:
        replay_tags = {
            r[0]
            for r in conn.execute(
                select(llm_run_tags.c.tag).where(llm_run_tags.c.llm_run_id == replay_id)
            )
        }
    assert "eval:tagfix_v1" not in replay_tags
    assert "eval:replay:tagfix_v1" in replay_tags
    assert "experiment:tagfix" in replay_tags
    assert len(repo.list(version="tagfix_v1")) == 1


def test_golden_with_empty_parsed_output_falls_back_to_raw_response(tracking_db, monkeypatch):
    """Consumers that validate post-record can store JSON-null ``parsed_output``
    (SQL ``IS NOT NULL`` can't see it). The runner must fall back to parsing the
    stored ``raw_response`` instead of scoring every replay against ``{}``."""
    from pf_core.eval._golden import GoldenSetRepo
    from pf_core.eval._runner import EvalRunner
    from pf_core.llm.tracking import (
        llm_agent_types,
        llm_models,
        llm_run_payloads,
        llm_runs,
    )

    golden_json = '{"category": "a", "confidence": 0.9}'
    with tracking_db.begin() as conn:
        mid = conn.execute(llm_models.insert().values(name="fallback-model")).inserted_primary_key[
            0
        ]
        aid = conn.execute(
            llm_agent_types.insert().values(slug="fallback_agent")
        ).inserted_primary_key[0]
        gid = conn.execute(
            llm_runs.insert().values(agent_type_id=aid, model_id=mid, status="success")
        ).inserted_primary_key[0]
        conn.execute(
            llm_run_payloads.insert().values(
                llm_run_id=gid,
                rendered_user="Q",
                raw_response=golden_json,
                parsed_output=None,
            )
        )

    GoldenSetRepo().add(gid, version="fallback_v1")

    class _FakeClient:
        def chat(self, *, messages, model="", **kwargs):
            return golden_json, {"duration_ms": 1}

    monkeypatch.setattr("pf_core.clients.openrouter.get_client", lambda *a, **k: _FakeClient())

    cfg = EvalConfig(
        {
            "agents": {
                "fallback_agent": {
                    "compare": "structured_diff",
                    "diff_fields": ["category", "confidence"],
                }
            }
        }
    )
    runner = EvalRunner.__new__(EvalRunner)
    runner._cfg = cfg

    report = runner.run(
        version="fallback_v1", agent_type="fallback_agent", target={"model": "candidate"}
    )
    assert report.results[0].error is None
    assert report.results[0].score == 1.0


def _seed_golden(tracking_db, *, slug: str, parsed_output, raw_response=None) -> int:
    from pf_core.llm.tracking import llm_agent_types, llm_models, llm_run_payloads, llm_runs

    with tracking_db.begin() as conn:
        mid = conn.execute(
            llm_models.insert().values(name=f"seed-model-{slug}")
        ).inserted_primary_key[0]
        aid = conn.execute(llm_agent_types.insert().values(slug=slug)).inserted_primary_key[0]
        gid = conn.execute(
            llm_runs.insert().values(agent_type_id=aid, model_id=mid, status="success")
        ).inserted_primary_key[0]
        conn.execute(
            llm_run_payloads.insert().values(
                llm_run_id=gid,
                rendered_user="Q",
                raw_response=raw_response,
                parsed_output=parsed_output,
            )
        )
    return gid


def test_replay_resolves_client_through_the_router(tracking_db, monkeypatch):
    """Replays run on the router-resolved backend — not a hardcoded OpenRouter
    client — so an agent's eval measures the transport it uses in production."""
    from pf_core.eval._golden import GoldenSetRepo
    from pf_core.eval._runner import EvalRunner

    gid = _seed_golden(tracking_db, slug="routed_agent", parsed_output={"answer": 1})
    GoldenSetRepo().add(gid, version="routed_v1")

    models_called = []

    class _RoutedClient:
        def chat(self, *, messages, model="", **kwargs):
            models_called.append(model)
            return '{"answer": 1}', {"duration_ms": 1}

    def _fake_resolve(slug, *, backend=None, model_override=None):
        assert slug == "routed_agent"
        return (
            _RoutedClient(),
            {"model": model_override or "routed-model", "temperature": 0.1},
            "fake_backend",
        )

    monkeypatch.setattr("pf_core.eval._runner.resolve_agent", _fake_resolve)

    def _boom(*a, **k):
        raise AssertionError("hardcoded OpenRouter client must not be used")

    monkeypatch.setattr("pf_core.clients.openrouter.get_client", _boom)

    cfg = EvalConfig({"agents": {"routed_agent": {"compare": "structured_diff"}}})
    runner = EvalRunner.__new__(EvalRunner)
    runner._cfg = cfg

    # No target model: the router supplies it — previously impossible for
    # nested-form agents.
    report = runner.run(version="routed_v1", agent_type="routed_agent", target={})
    assert report.results[0].error is None
    assert report.results[0].score == 1.0
    assert models_called == ["routed-model"]


def test_replay_non_config_router_error_becomes_error_result(tracking_db, monkeypatch):
    """Only ConfigurationError degrades to the OpenRouter fallback; any other
    resolution failure surfaces as an error result, never silently swallowed."""
    from pf_core.eval._golden import GoldenSetRepo
    from pf_core.eval._runner import EvalRunner

    gid = _seed_golden(tracking_db, slug="broken_resolve_agent", parsed_output={"a": 1})
    GoldenSetRepo().add(gid, version="broken_v1")

    def _boom_resolve(slug, **kwargs):
        raise RuntimeError("client exploded")

    monkeypatch.setattr("pf_core.eval._runner.resolve_agent", _boom_resolve)

    cfg = EvalConfig({"agents": {"broken_resolve_agent": {"compare": "structured_diff"}}})
    runner = EvalRunner.__new__(EvalRunner)
    runner._cfg = cfg

    report = runner.run(
        version="broken_v1", agent_type="broken_resolve_agent", target={"model": "x"}
    )
    result = report.results[0]
    assert result.error is not None and "client exploded" in result.error
    assert result.passed is False


def test_array_golden_errors_instead_of_silent_pass(tracking_db, monkeypatch):
    """A golden whose parsed_output isn't a non-empty dict cannot be
    structured-diffed: it must error before spending the replay call —
    never collapse to {} vs {} and score 1.0."""
    from pf_core.eval._golden import GoldenSetRepo
    from pf_core.eval._runner import EvalRunner

    gid = _seed_golden(
        tracking_db,
        slug="array_agent",
        parsed_output=[1, 2, 3],
        raw_response="[1, 2, 3]",
    )
    GoldenSetRepo().add(gid, version="array_v1")

    def _no_resolve(*a, **k):
        raise AssertionError("replay call must not be spent on an uncomparable golden")

    monkeypatch.setattr("pf_core.eval._runner.resolve_agent", _no_resolve)
    monkeypatch.setattr("pf_core.clients.openrouter.get_client", _no_resolve)

    cfg = EvalConfig({"agents": {"array_agent": {"compare": "structured_diff"}}})
    runner = EvalRunner.__new__(EvalRunner)
    runner._cfg = cfg

    report = runner.run(version="array_v1", agent_type="array_agent", target={"model": "x"})
    result = report.results[0]
    assert result.error is not None and "parsed_output" in result.error
    assert result.score == 0.0
    assert result.passed is False


def test_eval_runner_raises_on_empty_golden_set(pf_engine):
    """PreconditionError raised when no golden runs exist."""
    from pf_core.eval._runner import EvalRunner
    from pf_core.exceptions import PreconditionError

    metadata.create_all(pf_engine)
    try:
        runner = EvalRunner.__new__(EvalRunner)
        runner._cfg = EvalConfig({})

        with pytest.raises(PreconditionError, match="No golden runs"):
            runner.run(
                version="empty_version",
                agent_type="nonexistent_agent",
                target={},
            )
    finally:
        metadata.drop_all(pf_engine)


def _run_gated(tracking_db, monkeypatch, *, slug: str, comparator: str, gates: list[dict]):
    """Replay one golden through a comparator + metric gates; return the report."""
    from pf_core.eval._golden import GoldenSetRepo
    from pf_core.eval._runner import EvalRunner

    gid = _seed_golden(tracking_db, slug=slug, parsed_output={"answer": 1})
    GoldenSetRepo().add(gid, version=f"{slug}_v1")

    class _FakeClient:
        def chat(self, *, messages, model="", **kwargs):
            return '{"answer": 1}', {"duration_ms": 1}

    monkeypatch.setattr("pf_core.clients.openrouter.get_client", lambda *a, **k: _FakeClient())

    cfg = EvalConfig({"agents": {slug: {"compare": comparator, "metrics": gates}}})
    runner = EvalRunner.__new__(EvalRunner)
    runner._cfg = cfg
    return runner.run(version=f"{slug}_v1", agent_type=slug, target={"model": "candidate"})


def test_metric_gate_fails_a_replay_when_the_metric_is_out_of_range(tracking_db, monkeypatch):
    """A configured gate must actually gate: a comparator-reported metric below
    ``min`` drives the replay to 0.0 even though the comparison itself scored 1.0."""
    from sqlalchemy import select

    from pf_core.eval._compare import register_comparator
    from pf_core.llm.tracking import llm_run_metrics

    @register_comparator("gate_test_low")
    def _low(golden, replay, *, context):
        return 1.0, {"field_ratio": 0.40}

    report = _run_gated(
        tracking_db,
        monkeypatch,
        slug="gate_low_agent",
        comparator="gate_test_low",
        gates=[{"name": "field_ratio", "min": 0.70}],
    )
    result = report.results[0]
    assert result.error is None
    assert result.score == 0.0
    assert result.passed is False
    assert report.passed is False

    with tracking_db.connect() as conn:
        stored = dict(
            conn.execute(
                select(llm_run_metrics.c.metric_name, llm_run_metrics.c.metric_value).where(
                    llm_run_metrics.c.llm_run_id == result.run_id
                )
            ).fetchall()
        )
    assert stored == {"field_ratio": 0.40}


def test_metric_gate_passes_a_replay_when_the_metric_is_in_range(tracking_db, monkeypatch):
    """The same gate leaves the comparator's score alone when the metric holds."""
    from pf_core.eval._compare import register_comparator

    @register_comparator("gate_test_high")
    def _high(golden, replay, *, context):
        return 1.0, {"field_ratio": 0.95}

    report = _run_gated(
        tracking_db,
        monkeypatch,
        slug="gate_high_agent",
        comparator="gate_test_high",
        gates=[{"name": "field_ratio", "min": 0.70, "max": 1.0}],
    )
    result = report.results[0]
    assert result.error is None
    assert result.score == 1.0
    assert result.passed is True
    assert report.passed is True


def test_metric_gate_with_no_source_fails_closed(tracking_db, monkeypatch):
    """A gate on a metric nothing produces must fail the replay, not be skipped —
    a gate that always passes is worse than no gate."""
    report = _run_gated(
        tracking_db,
        monkeypatch,
        slug="gate_orphan_agent",
        comparator="structured_diff",
        gates=[{"name": "never_written", "min": 0.70}],
    )
    result = report.results[0]
    assert result.score == 0.0
    assert result.passed is False


# ---------------------------------------------------------------------------
# compare_experiments
# ---------------------------------------------------------------------------


def _seed_compare_agent(tracking_db, *, slug: str) -> tuple[int, int]:
    from pf_core.llm.tracking import llm_agent_types, llm_models

    with tracking_db.begin() as conn:
        mid = conn.execute(
            llm_models.insert().values(name=f"cmp-model-{slug}")
        ).inserted_primary_key[0]
        aid = conn.execute(llm_agent_types.insert().values(slug=slug)).inserted_primary_key[0]
    return aid, mid


def _seed_scored_replay(
    tracking_db, *, agent_type_id: int, model_id: int, golden_id: int, tag: str, score: float
) -> int:
    from pf_core.llm.tracking import llm_run_links, llm_run_outcomes, llm_run_tags, llm_runs

    with tracking_db.begin() as conn:
        rid = conn.execute(
            llm_runs.insert().values(
                agent_type_id=agent_type_id, model_id=model_id, status="success"
            )
        ).inserted_primary_key[0]
        conn.execute(
            llm_run_links.insert().values(
                parent_run_id=golden_id, child_run_id=rid, relation="replay"
            )
        )
        conn.execute(llm_run_tags.insert().values(llm_run_id=rid, tag=tag))
        conn.execute(
            llm_run_outcomes.insert().values(llm_run_id=rid, outcome_kind="eval_score", score=score)
        )
    return rid


def _compare_runner():
    from pf_core.eval._runner import EvalRunner

    runner = EvalRunner.__new__(EvalRunner)
    runner._cfg = EvalConfig({})
    return runner


def _seed_golden_run(tracking_db, *, agent_type_id: int, model_id: int) -> int:
    from pf_core.llm.tracking import llm_runs

    with tracking_db.begin() as conn:
        return conn.execute(
            llm_runs.insert().values(
                agent_type_id=agent_type_id, model_id=model_id, status="success"
            )
        ).inserted_primary_key[0]


def test_compare_experiments_raises_when_baseline_tag_matches_nothing(tracking_db):
    """A typo'd baseline tag must raise, never return an empty comparison."""
    from pf_core.exceptions import PreconditionError

    aid, mid = _seed_compare_agent(tracking_db, slug="cmp_empty_base_agent")
    gid = _seed_golden_run(tracking_db, agent_type_id=aid, model_id=mid)
    _seed_scored_replay(
        tracking_db,
        agent_type_id=aid,
        model_id=mid,
        golden_id=gid,
        tag="experiment:cand",
        score=0.9,
    )

    with pytest.raises(PreconditionError, match="experiment:no-such-tag"):
        _compare_runner().compare_experiments(
            baseline="experiment:no-such-tag",
            candidate="experiment:cand",
            agent_type="cmp_empty_base_agent",
        )


def test_compare_experiments_raises_when_candidate_tag_matches_nothing(tracking_db):
    from pf_core.exceptions import PreconditionError

    aid, mid = _seed_compare_agent(tracking_db, slug="cmp_empty_cand_agent")
    gid = _seed_golden_run(tracking_db, agent_type_id=aid, model_id=mid)
    _seed_scored_replay(
        tracking_db,
        agent_type_id=aid,
        model_id=mid,
        golden_id=gid,
        tag="experiment:base",
        score=0.8,
    )

    with pytest.raises(PreconditionError, match="experiment:also-missing"):
        _compare_runner().compare_experiments(
            baseline="experiment:base",
            candidate="experiment:also-missing",
            agent_type="cmp_empty_cand_agent",
        )


def test_compare_experiments_returns_pairs_and_counts(tracking_db):
    aid, mid = _seed_compare_agent(tracking_db, slug="cmp_happy_agent")
    g1 = _seed_golden_run(tracking_db, agent_type_id=aid, model_id=mid)
    g2 = _seed_golden_run(tracking_db, agent_type_id=aid, model_id=mid)
    for gid, score in ((g1, 0.8), (g2, 0.6)):
        _seed_scored_replay(
            tracking_db,
            agent_type_id=aid,
            model_id=mid,
            golden_id=gid,
            tag="experiment:base",
            score=score,
        )
    for gid, score in ((g1, 0.9), (g2, 0.5)):
        _seed_scored_replay(
            tracking_db,
            agent_type_id=aid,
            model_id=mid,
            golden_id=gid,
            tag="experiment:cand",
            score=score,
        )

    result = _compare_runner().compare_experiments(
        baseline="experiment:base", candidate="experiment:cand", agent_type="cmp_happy_agent"
    )
    assert result["baseline_runs"] == 2
    assert result["candidate_runs"] == 2
    assert [p["golden_id"] for p in result["pairs"]] == sorted([g1, g2])
    by_gid = {p["golden_id"]: p for p in result["pairs"]}
    assert by_gid[g1]["baseline_score"] == pytest.approx(0.8)
    assert by_gid[g1]["candidate_score"] == pytest.approx(0.9)
    assert by_gid[g1]["delta"] == pytest.approx(0.1)
    assert by_gid[g2]["delta"] == pytest.approx(-0.1)


def test_compare_experiments_latest_replay_wins_for_reused_tag(tracking_db):
    """Duplicate scored replays under one tag: latest wins; row count exposes it."""
    aid, mid = _seed_compare_agent(tracking_db, slug="cmp_dupe_agent")
    gid = _seed_golden_run(tracking_db, agent_type_id=aid, model_id=mid)
    for score in (0.4, 0.9):
        _seed_scored_replay(
            tracking_db,
            agent_type_id=aid,
            model_id=mid,
            golden_id=gid,
            tag="experiment:base",
            score=score,
        )
    _seed_scored_replay(
        tracking_db,
        agent_type_id=aid,
        model_id=mid,
        golden_id=gid,
        tag="experiment:cand",
        score=0.7,
    )

    result = _compare_runner().compare_experiments(
        baseline="experiment:base", candidate="experiment:cand", agent_type="cmp_dupe_agent"
    )
    assert result["baseline_runs"] == 2
    assert result["candidate_runs"] == 1
    assert len(result["pairs"]) == 1
    assert result["pairs"][0]["baseline_score"] == pytest.approx(0.9)
    assert result["pairs"][0]["delta"] == pytest.approx(-0.2)


def test_compare_experiments_reports_partial_overlap_not_raises(tracking_db):
    """Counts larger than len(pairs) expose a shrunken pairing to the caller."""
    aid, mid = _seed_compare_agent(tracking_db, slug="cmp_partial_agent")
    g1 = _seed_golden_run(tracking_db, agent_type_id=aid, model_id=mid)
    g2 = _seed_golden_run(tracking_db, agent_type_id=aid, model_id=mid)
    g3 = _seed_golden_run(tracking_db, agent_type_id=aid, model_id=mid)
    for gid in (g1, g2):
        _seed_scored_replay(
            tracking_db,
            agent_type_id=aid,
            model_id=mid,
            golden_id=gid,
            tag="experiment:base",
            score=0.7,
        )
    for gid in (g2, g3):
        _seed_scored_replay(
            tracking_db,
            agent_type_id=aid,
            model_id=mid,
            golden_id=gid,
            tag="experiment:cand",
            score=0.8,
        )

    result = _compare_runner().compare_experiments(
        baseline="experiment:base", candidate="experiment:cand", agent_type="cmp_partial_agent"
    )
    assert result["baseline_runs"] == 2
    assert result["candidate_runs"] == 2
    assert len(result["pairs"]) == 1
    assert result["pairs"][0]["golden_id"] == g2


def test_compare_experiments_raises_when_tags_share_no_golden(tracking_db):
    """Two populated tags over different golden sets yield zero pairs, which
    must not read as "no regressions"."""
    from pf_core.exceptions import PreconditionError

    aid, mid = _seed_compare_agent(tracking_db, slug="cmp_disjoint_agent")
    g1 = _seed_golden_run(tracking_db, agent_type_id=aid, model_id=mid)
    g2 = _seed_golden_run(tracking_db, agent_type_id=aid, model_id=mid)
    _seed_scored_replay(
        tracking_db, agent_type_id=aid, model_id=mid, golden_id=g1, tag="base", score=0.9
    )
    _seed_scored_replay(
        tracking_db, agent_type_id=aid, model_id=mid, golden_id=g2, tag="cand", score=0.9
    )

    with pytest.raises(PreconditionError, match="share no golden parent"):
        _compare_runner().compare_experiments(
            baseline="base", candidate="cand", agent_type="cmp_disjoint_agent"
        )


def test_compare_experiments_rejects_a_self_comparison(tracking_db):
    """Comparing a tag with itself reports every delta as 0.0 — a clean gate
    that proves nothing."""
    from pf_core.exceptions import InvalidInputError

    with pytest.raises(InvalidInputError, match="same tag"):
        _compare_runner().compare_experiments(
            baseline="base", candidate="base", agent_type="cmp_self_agent"
        )


def test_compare_experiments_skips_replays_with_a_null_score(tracking_db):
    """A NULL eval_score is not a scored replay; it must be filtered out, not
    crash the float() conversion."""
    aid, mid = _seed_compare_agent(tracking_db, slug="cmp_nullscore_agent")
    g1 = _seed_golden_run(tracking_db, agent_type_id=aid, model_id=mid)
    g2 = _seed_golden_run(tracking_db, agent_type_id=aid, model_id=mid)
    _seed_scored_replay(
        tracking_db, agent_type_id=aid, model_id=mid, golden_id=g1, tag="base", score=0.5
    )
    _seed_scored_replay(
        tracking_db, agent_type_id=aid, model_id=mid, golden_id=g1, tag="cand", score=0.7
    )
    _seed_scored_replay(
        tracking_db, agent_type_id=aid, model_id=mid, golden_id=g2, tag="base", score=None
    )
    _seed_scored_replay(
        tracking_db, agent_type_id=aid, model_id=mid, golden_id=g2, tag="cand", score=None
    )

    result = _compare_runner().compare_experiments(
        baseline="base", candidate="cand", agent_type="cmp_nullscore_agent"
    )
    assert [p["golden_id"] for p in result["pairs"]] == [g1]
    assert result["baseline_runs"] == 1


# ---------------------------------------------------------------------------
# Tag matching is exact regardless of the server's collation
# ---------------------------------------------------------------------------


def test_exact_tag_rows_drops_case_variants():
    """Simulates a case-insensitive collation (MySQL's utf8mb4_0900_ai_ci):
    SQL ``=`` hands back the wrong-case rows, and the filter removes them."""
    from pf_core.eval._experiments import _exact_tag_rows

    rows = [
        {"golden_id": 1, "score": 0.8, "tag": "experiment:v5"},
        {"golden_id": 2, "score": 0.6, "tag": "experiment:V5"},
    ]
    assert [r["golden_id"] for r in _exact_tag_rows(rows, "experiment:v5")] == [1]
    assert [r["golden_id"] for r in _exact_tag_rows(rows, "experiment:V5")] == [2]
    assert _exact_tag_rows(rows, "experiment:V5X") == []


def test_case_variant_candidate_raises_instead_of_reporting_zero_deltas(tracking_db, monkeypatch):
    """On a case-insensitive server the candidate tag selects the baseline's
    own rows, so neither guard fires and an all-zero-delta table reads as
    'no regression' for an experiment that never ran."""
    import pf_core.eval._experiments as ex
    from pf_core.exceptions import PreconditionError

    aid, mid = _seed_compare_agent(tracking_db, slug="cmp_case_agent")
    gid = _seed_golden_run(tracking_db, agent_type_id=aid, model_id=mid)
    _seed_scored_replay(
        tracking_db,
        agent_type_id=aid,
        model_id=mid,
        golden_id=gid,
        tag="experiment:v5",
        score=0.8,
    )

    real = ex._fetch_tag_rows
    monkeypatch.setattr(
        ex, "_fetch_tag_rows", lambda repo, tag, agent: real(repo, tag.lower(), agent)
    )

    with pytest.raises(PreconditionError, match="experiment:V5"):
        ex.compare_experiments(
            baseline="experiment:v5",
            candidate="experiment:V5",
            agent_type="cmp_case_agent",
        )
