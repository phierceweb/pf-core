"""Tests for pf_core.cli.jobs — the pf-jobs admin CLI sub-app.

Exercises the command layer (list / show / retry / cancel / reclaim / purge)
end-to-end through Typer's CliRunner against a real JobRepo on the test DB,
plus the parse_duration helper. (The repo itself is covered by
tests/test_jobs/; this file covers the CLI wiring on top of it.)
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import typer
from pydantic import BaseModel
from typer.testing import CliRunner

import pf_core.jobs.registry as _jobs_registry
from pf_core.cli.jobs import app, parse_duration
from pf_core.jobs import JobRepo, register_kind
from pf_core.llm.tracking import metadata

runner = CliRunner()


class _Inputs(BaseModel):
    x: int = 0


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Snapshot/restore the global job-kind registry so registering
    ``demo_pass`` here doesn't clobber kinds other modules register at
    import time."""
    saved = dict(_jobs_registry._REGISTRY)
    _jobs_registry._REGISTRY.clear()
    yield
    _jobs_registry._REGISTRY.clear()
    _jobs_registry._REGISTRY.update(saved)


@pytest.fixture()
def jobs_db(pf_engine):
    """Test engine with the jobs + llm_* tables created."""
    metadata.create_all(pf_engine)
    yield pf_engine
    metadata.drop_all(pf_engine)


@pytest.fixture()
def kind():
    register_kind(kind="demo_pass", inputs_schema=_Inputs)


# ---------------------------------------------------------------------------
# parse_duration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("60s", timedelta(seconds=60)),
        ("30m", timedelta(minutes=30)),
        ("24h", timedelta(hours=24)),
        ("90d", timedelta(days=90)),
        ("2w", timedelta(weeks=2)),
        ("  7d ", timedelta(days=7)),
        ("12H", timedelta(hours=12)),
    ],
)
def test_parse_duration_valid(raw, expected):
    assert parse_duration(raw) == expected


@pytest.mark.parametrize("raw", ["abc", "10", "5y", "", "d", "1.5h"])
def test_parse_duration_invalid_raises(raw):
    with pytest.raises(typer.BadParameter):
        parse_duration(raw)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_empty(jobs_db, kind):
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "No jobs match" in result.output


def test_list_shows_jobs(jobs_db, kind):
    repo = JobRepo()
    repo.create(kind="demo_pass", created_by="alice")
    repo.create(kind="demo_pass", created_by="bob")

    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "2 rows" in result.output
    assert "demo_pass" in result.output


def test_list_filters_by_created_by(jobs_db, kind):
    repo = JobRepo()
    repo.create(kind="demo_pass", created_by="alice")
    repo.create(kind="demo_pass", created_by="bob")

    result = runner.invoke(app, ["list", "--created-by", "alice"])
    assert result.exit_code == 0
    assert "1 rows" in result.output


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def test_show_found(jobs_db, kind):
    job_id = JobRepo().create(kind="demo_pass")
    result = runner.invoke(app, ["show", str(job_id)])
    assert result.exit_code == 0
    assert f"Job {job_id}" in result.output
    assert "demo_pass" in result.output


def test_show_missing_exits_1(jobs_db, kind):
    result = runner.invoke(app, ["show", "9999"])
    assert result.exit_code == 1
    assert "not found" in result.output


# ---------------------------------------------------------------------------
# cancel / retry
# ---------------------------------------------------------------------------


def test_cancel_marks_canceled(jobs_db, kind):
    job_id = JobRepo().create(kind="demo_pass")
    result = runner.invoke(app, ["cancel", str(job_id)])
    assert result.exit_code == 0
    assert "canceled" in result.output
    assert JobRepo().get_with_steps(job_id)["status"] == "canceled"


def test_retry_requeues_a_canceled_job(jobs_db, kind):
    job_id = JobRepo().create(kind="demo_pass")
    JobRepo().cancel(job_id)

    result = runner.invoke(app, ["retry", str(job_id)])
    assert result.exit_code == 0
    assert "requeued" in result.output
    assert JobRepo().get_with_steps(job_id)["status"] == "pending"


# ---------------------------------------------------------------------------
# reclaim / purge
# ---------------------------------------------------------------------------


def test_reclaim_reports_count(jobs_db, kind):
    result = runner.invoke(app, ["reclaim"])
    assert result.exit_code == 0
    assert "Reclaimed 0 stale job(s)" in result.output


def test_purge_with_yes_skips_confirmation(jobs_db, kind):
    # A fresh pending job isn't finished, so nothing is purged — but the
    # command runs without prompting.
    JobRepo().create(kind="demo_pass")
    result = runner.invoke(app, ["purge", "--older-than", "90d", "--yes"])
    assert result.exit_code == 0
    assert "Purged 0 job(s)" in result.output


def test_purge_aborts_on_declined_confirmation(jobs_db, kind):
    result = runner.invoke(app, ["purge", "--older-than", "90d"], input="n\n")
    assert result.exit_code == 1
    assert "Aborted" in result.output
