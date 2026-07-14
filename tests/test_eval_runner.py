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
                llm_runs.insert().values(
                    agent_type_id=aid, model_id=mid, status="success"
                )
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
        mid = conn.execute(
            llm_models.insert().values(name="tagfix-model")
        ).inserted_primary_key[0]
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

    monkeypatch.setattr(
        "pf_core.clients.openrouter.get_client", lambda *a, **k: _FakeClient()
    )

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


def test_golden_with_empty_parsed_output_falls_back_to_raw_response(
    tracking_db, monkeypatch
):
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
        mid = conn.execute(
            llm_models.insert().values(name="fallback-model")
        ).inserted_primary_key[0]
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

    monkeypatch.setattr(
        "pf_core.clients.openrouter.get_client", lambda *a, **k: _FakeClient()
    )

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
