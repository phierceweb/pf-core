"""Job runtime: context manager, step idempotency, llm_runs attribution."""

from __future__ import annotations

import pytest

from pf_core.exceptions import PreconditionError
from pf_core.jobs import (
    Job,
    JobRepo,
    get_current_job_id,
)
from pf_core.llm.tracking import LlmRunRepo


# ---------------------------------------------------------------------------
# Context var
# ---------------------------------------------------------------------------


def test_current_job_id_is_none_outside_job():
    assert get_current_job_id() is None


def test_job_sets_and_resets_context_var(jobs_db, simple_kind):
    repo = JobRepo()
    job_id = repo.create(kind="simple_pass")

    assert get_current_job_id() is None
    with Job(job_id):
        assert get_current_job_id() == job_id
    assert get_current_job_id() is None


def test_nested_jobs_stack_context(jobs_db, simple_kind):
    repo = JobRepo()
    outer = repo.create(kind="simple_pass")
    inner = repo.create(kind="simple_pass", parent_job_id=outer)

    with Job(outer):
        assert get_current_job_id() == outer
        with Job(inner):
            assert get_current_job_id() == inner
        assert get_current_job_id() == outer
    assert get_current_job_id() is None


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_job_enter_loads_row(jobs_db, simple_kind):
    repo = JobRepo()
    job_id = repo.create(
        kind="simple_pass", inputs={"widget_ids": [1, 2, 3]}
    )

    with Job(job_id) as job:
        assert job.id == job_id
        assert job.kind == "simple_pass"
        assert job.status == "pending"
        assert job.inputs == {"widget_ids": [1, 2, 3], "config_id": None}


def test_job_missing_raises(jobs_db):
    with pytest.raises(PreconditionError, match="not found"):
        with Job(99999):
            pass


def test_job_transition_refreshes_status(jobs_db, simple_kind):
    repo = JobRepo()
    job_id = repo.create(kind="simple_pass")

    with Job(job_id) as job:
        assert job.status == "pending"
        job.transition("running")
        assert job.status == "running"


def test_job_transition_succeeded_picks_up_deferred_outputs(jobs_db, simple_kind):
    repo = JobRepo()
    job_id = repo.create(kind="simple_pass")

    with Job(job_id) as job:
        job.transition("running")
        job.outputs = {"n_processed": 7}
        job.transition("succeeded")

    row = repo.get(job_id)
    assert row["status"] == "succeeded"
    assert row["outputs"] == {"n_processed": 7}


def test_job_exception_force_fails(jobs_db, simple_kind):
    repo = JobRepo()
    job_id = repo.create(kind="simple_pass")

    with pytest.raises(RuntimeError, match="boom"):
        with Job(job_id) as job:
            job.transition("running")
            raise RuntimeError("boom")

    row = repo.get(job_id)
    assert row["status"] == "failed"
    assert row["error"] == "boom"
    assert row["error_class"] == "RuntimeError"
    assert row["finished_at"] is not None

    events = repo.get_events(job_id, event_type="exception")
    assert len(events) == 1
    assert events[0]["message"] == "boom"
    assert events[0]["context"] == {"error_class": "RuntimeError"}


def test_job_exception_from_pending_force_fails(jobs_db, simple_kind):
    """Even from 'pending' (not normally allowed → failed), force-fail works."""
    repo = JobRepo()
    job_id = repo.create(kind="simple_pass")

    with pytest.raises(ValueError):
        with Job(job_id):
            raise ValueError("early crash")

    row = repo.get(job_id)
    assert row["status"] == "failed"
    assert row["error_class"] == "ValueError"


def test_job_exception_preserves_terminal_state(jobs_db, simple_kind):
    """If the job is already terminal, an exception afterward doesn't overwrite."""
    repo = JobRepo()
    job_id = repo.create(kind="simple_pass")

    with pytest.raises(RuntimeError):
        with Job(job_id) as job:
            job.transition("running")
            job.outputs = {"n_processed": 1}
            job.transition("succeeded")
            raise RuntimeError("post-success cleanup failed")

    row = repo.get(job_id)
    assert row["status"] == "succeeded"
    # The exception event was still written for forensics.
    events = repo.get_events(job_id, event_type="exception")
    assert len(events) == 1


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def test_step_records_outputs_on_success(jobs_db, simple_kind):
    repo = JobRepo()
    job_id = repo.create(kind="simple_pass")

    with Job(job_id) as job:
        job.transition("running")
        with job.step("grade_1", inputs={"submission_id": 1}) as step:
            assert step.skipped is False
            step.outputs = {"grade": 28}

    s = repo.find_step(job_id, name="grade_1")
    assert s["status"] == "succeeded"
    assert s["outputs"] == {"grade": 28}


def test_step_is_idempotent_when_prior_succeeded(jobs_db, simple_kind):
    """Re-entering a step whose prior run succeeded short-circuits."""
    repo = JobRepo()
    job_id = repo.create(kind="simple_pass")

    call_count = {"n": 0}

    # First run.
    with Job(job_id) as job:
        job.transition("running")
        with job.step("grade_1") as step:
            call_count["n"] += 1
            step.outputs = {"grade": 28}

    # Simulate resume — second run should skip the step.
    with Job(job_id) as job:
        with job.step("grade_1") as step:
            assert step.skipped is True
            call_count["n"] += 1  # this still runs; skipped flag just informs

    assert call_count["n"] == 2  # both bodies ran, but skipped flag was set

    # Only one step row exists.
    with_steps = repo.get_with_steps(job_id)
    step_rows = [s for s in with_steps["steps"] if s["name"] == "grade_1"]
    assert len(step_rows) == 1


def test_step_exception_marks_step_failed_and_reraises(jobs_db, simple_kind):
    repo = JobRepo()
    job_id = repo.create(kind="simple_pass")

    with pytest.raises(RuntimeError, match="step kaboom"):
        with Job(job_id) as job:
            job.transition("running")
            with job.step("grade_1"):
                raise RuntimeError("step kaboom")

    s = repo.find_step(job_id, name="grade_1")
    assert s["status"] == "failed"
    assert s["error"] == "step kaboom"

    # Job is also force-failed.
    row = repo.get(job_id)
    assert row["status"] == "failed"


def test_step_handle_error_marks_failed_without_exception(jobs_db, simple_kind):
    repo = JobRepo()
    job_id = repo.create(kind="simple_pass")

    with Job(job_id) as job:
        job.transition("running")
        with job.step("try_it") as step:
            step.error = "domain rejected"

    s = repo.find_step(job_id, name="try_it")
    assert s["status"] == "failed"
    assert s["error"] == "domain rejected"


# ---------------------------------------------------------------------------
# llm_runs.job_id attribution via contextvar
# ---------------------------------------------------------------------------


def test_llm_runs_auto_attributed_to_active_job(jobs_db, simple_kind):
    """An LLM run recorded inside a Job block gets job_id set automatically."""
    repo = JobRepo()
    llm = LlmRunRepo()

    job_id = repo.create(kind="simple_pass")

    with Job(job_id):
        run_id = llm.record(agent_type="grader", model="gpt-4o-mini")

    run = llm.get(run_id)
    assert run["job_id"] == job_id


def test_llm_runs_outside_job_have_null_job_id(jobs_db, simple_kind):
    llm = LlmRunRepo()
    run_id = llm.record(agent_type="grader", model="gpt-4o-mini")
    run = llm.get(run_id)
    assert run["job_id"] is None


def test_explicit_job_id_overrides_contextvar(jobs_db, simple_kind):
    """If the caller passes job_id explicitly, the contextvar doesn't win."""
    repo = JobRepo()
    llm = LlmRunRepo()

    outer = repo.create(kind="simple_pass")
    explicit = repo.create(kind="simple_pass")

    with Job(outer):
        run_id = llm.record(
            agent_type="grader", model="gpt-4o-mini", job_id=explicit
        )

    run = llm.get(run_id)
    assert run["job_id"] == explicit


def test_llm_run_in_step_gets_job_id(jobs_db, simple_kind):
    """Step nesting still sees the active job via contextvar."""
    repo = JobRepo()
    llm = LlmRunRepo()

    job_id = repo.create(kind="simple_pass")

    with Job(job_id) as job:
        job.transition("running")
        with job.step("grade_1"):
            run_id = llm.record(agent_type="grader", model="gpt-4o-mini")

    run = llm.get(run_id)
    assert run["job_id"] == job_id


# ---------------------------------------------------------------------------
# Event + progress convenience
# ---------------------------------------------------------------------------


def test_job_progress_and_event_helpers(jobs_db, simple_kind):
    repo = JobRepo()
    job_id = repo.create(kind="simple_pass")

    with Job(job_id) as job:
        job.transition("running")
        job.progress(total=10)
        job.progress(current=5, step="halfway")
        job.event("info", "checkpoint", context={"k": 1})

    row = repo.get(job_id)
    assert row["progress_total"] == 10
    assert row["progress_current"] == 5
    assert row["current_step"] == "halfway"

    events = repo.get_events(job_id, event_type="info")
    assert len(events) == 1
    assert events[0]["context"] == {"k": 1}
