"""Tests for the background thread submitter (jobs.submit)."""

from __future__ import annotations

import threading

import pytest

from pf_core.jobs import JobRepo
from pf_core.jobs.registry import register_kind
from pf_core.jobs.submit import (
    JobAlreadyRunning,
    submit_detached,
    submit_tracked,
    wait_all,
)

KIND = "submit_probe"


@pytest.fixture
def pf_schema():
    from pf_core.testing.db_fixtures import framework_ddl

    return framework_ddl()


@pytest.fixture(autouse=True)
def _kind(pf_tables):
    register_kind(kind=KIND, description="submitter test kind")
    yield
    wait_all(timeout=10.0)


def _scope(value: str):
    return lambda inputs: inputs.get("scope") == value


class TestSubmitTracked:
    def test_runs_and_succeeds_with_progress(self):
        seen: list[tuple] = []

        def work(progress):
            progress(1, 2, "half")
            seen.append(("ran",))

        job_id = submit_tracked(
            kind=KIND, inputs={"scope": "a"}, created_by="test", run=work
        )
        wait_all()
        row = JobRepo().get(job_id)
        assert row["status"] == "succeeded"
        assert seen == [("ran",)]
        assert row["progress_current"] == 1
        assert row["progress_total"] == 2

    def test_failure_marks_failed(self):
        def work(progress):
            raise RuntimeError("boom")

        job_id = submit_tracked(
            kind=KIND, inputs={"scope": "a"}, created_by="test", run=work
        )
        wait_all()
        row = JobRepo().get(job_id)
        assert row["status"] == "failed"
        assert "boom" in (row["error"] or "")

    def test_dedup_rejects_second(self):
        release = threading.Event()

        def blocked(progress):
            release.wait(timeout=10)

        first = submit_tracked(
            kind=KIND,
            inputs={"scope": "a"},
            created_by="test",
            run=blocked,
            dedup_key=_scope("a"),
        )
        try:
            with pytest.raises(JobAlreadyRunning):
                submit_tracked(
                    kind=KIND,
                    inputs={"scope": "a"},
                    created_by="test",
                    run=blocked,
                    dedup_key=_scope("a"),
                )
            # A different scope is not deduped.
            other = submit_tracked(
                kind=KIND,
                inputs={"scope": "b"},
                created_by="test",
                run=lambda p: None,
                dedup_key=_scope("b"),
            )
            assert other != first
        finally:
            release.set()
            wait_all()


class TestSubmitDetached:
    def test_returns_service_created_job_id(self):
        def service():
            job_id = JobRepo().create(
                kind=KIND, inputs={"scope": "c"}, created_by="svc"
            )
            repo = JobRepo()
            repo.transition(job_id, "running")
            repo.transition(job_id, "succeeded")

        resolved = submit_detached(kind=KIND, run=service, dedup_key=_scope("c"))
        wait_all()
        assert resolved is not None
        assert JobRepo().get(resolved)["kind"] == KIND

    def test_none_when_service_creates_no_job(self):
        resolved = submit_detached(
            kind=KIND, run=lambda: None, dedup_key=_scope("nope")
        )
        wait_all()
        assert resolved is None
