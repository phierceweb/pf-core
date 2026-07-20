"""Tests for the polling worker pool + subprocess job runner."""

from __future__ import annotations

import sys
import time

import pytest

from pf_core.jobs import JobRepo
from pf_core.jobs.registry import register_kind
from pf_core.jobs.workers import (
    SubprocessJobSpec,
    run_subprocess_job,
    start_workers,
    stop_workers,
    tail_log,
    terminate_job,
)

KIND = "worker_probe"


@pytest.fixture
def pf_schema():
    from pf_core.testing.db_fixtures import framework_ddl

    return framework_ddl()


@pytest.fixture(autouse=True)
def _kind(pf_tables):
    register_kind(kind=KIND, description="worker test kind")


def _spec(tmp_path, argv):
    return SubprocessJobSpec(
        name="probe",
        argv=lambda row: argv,
        log_path=lambda row: tmp_path / f"job-{row['id']}.log",
        outputs=lambda row, rc: {"returncode": rc},
    )


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


class TestRunSubprocessJob:
    def test_zero_exit_marks_succeeded_with_outputs(self, tmp_path):
        job_id = JobRepo().create(kind=KIND, created_by="test")
        row = JobRepo().get(job_id)
        run_subprocess_job(row, _spec(tmp_path, _py("print('hello')")))
        after = JobRepo().get(job_id)
        assert after["status"] == "succeeded"
        assert after["outputs"] == {"returncode": 0}
        log = (tmp_path / f"job-{job_id}.log").read_text()
        assert log.startswith("$ ")
        assert "hello" in log

    def test_nonzero_exit_marks_failed_with_log_pointer(self, tmp_path):
        job_id = JobRepo().create(kind=KIND, created_by="test")
        row = JobRepo().get(job_id)
        run_subprocess_job(row, _spec(tmp_path, _py("raise SystemExit(3)")))
        after = JobRepo().get(job_id)
        assert after["status"] == "failed"
        assert "probe exited 3" in after["error"]
        assert f"job-{job_id}.log" in after["error"]

    def test_job_id_env_injected(self, tmp_path):
        job_id = JobRepo().create(kind=KIND, created_by="test")
        row = JobRepo().get(job_id)
        out = tmp_path / "envdump"
        code = f"import os, pathlib; pathlib.Path({str(out)!r}).write_text(os.environ.get('PF_JOB_ID', ''))"
        run_subprocess_job(row, _spec(tmp_path, _py(code)))
        assert out.read_text() == str(job_id)

class TestTerminate:
    def test_returns_false_when_not_running(self):
        assert terminate_job(999999) is False

    def test_cancel_midrun_then_terminate_leaves_canceled(self, tmp_path):
        # Cancel can only land mid-run (claim_next takes pending rows only);
        # after the kill, the runner must leave the canceled row untouched.
        import threading

        from pf_core.jobs import workers as workers_mod

        job_id = JobRepo().create(kind=KIND, created_by="test")
        row = JobRepo().get(job_id)
        spec = _spec(tmp_path, _py("import time; time.sleep(30)"))
        t = threading.Thread(target=run_subprocess_job, args=(row, spec), daemon=True)
        t.start()

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with workers_mod._RUNNING_LOCK:
                started = job_id in workers_mod._RUNNING
            if started:
                break
            time.sleep(0.05)
        assert started, "runner never registered the subprocess"

        JobRepo().cancel(job_id, reason="test")
        assert terminate_job(job_id) is True
        t.join(timeout=10)
        assert not t.is_alive()
        assert JobRepo().get(job_id)["status"] == "canceled"


class TestWorkerPool:
    def test_processes_pending_job_then_stops(self):
        done: list[int] = []

        def run(job_row):
            repo = JobRepo()
            repo.transition(int(job_row["id"]), "running")
            repo.transition(int(job_row["id"]), "succeeded")
            done.append(int(job_row["id"]))

        job_id = JobRepo().create(kind=KIND, created_by="test")
        handle = start_workers(
            kinds=[KIND], run=run, concurrency=1, poll_seconds=0.05,
            reclaim_on_start=False,
        )
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and job_id not in done:
                time.sleep(0.05)
        finally:
            stop_workers(handle)
        assert done == [job_id]
        assert JobRepo().get(job_id)["status"] == "succeeded"
        assert all(not t.is_alive() for t in handle.threads)

    def test_reclaim_on_start_invoked(self, monkeypatch):
        calls: list[int] = []
        monkeypatch.setattr(
            JobRepo, "reclaim_stale", lambda self, **kw: calls.append(1) or 0
        )
        handle = start_workers(kinds=[KIND], run=lambda row: None, poll_seconds=0.05)
        stop_workers(handle)
        assert calls == [1]

    def test_run_errors_do_not_kill_the_loop(self):
        seen: list[int] = []

        def run(job_row):
            seen.append(int(job_row["id"]))
            if len(seen) == 1:
                raise RuntimeError("first fails")
            repo = JobRepo()
            repo.transition(int(job_row["id"]), "running")
            repo.transition(int(job_row["id"]), "succeeded")

        first = JobRepo().create(kind=KIND, created_by="test")
        second = JobRepo().create(kind=KIND, created_by="test")
        handle = start_workers(
            kinds=[KIND], run=run, poll_seconds=0.05, reclaim_on_start=False
        )
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and len(seen) < 2:
                time.sleep(0.05)
        finally:
            stop_workers(handle)
        assert set(seen) == {first, second}


class TestTailLog:
    def test_missing_file(self, tmp_path):
        assert tail_log(tmp_path / "nope.log") == ("", 0)

    def test_offset_protocol(self, tmp_path):
        p = tmp_path / "run.log"
        p.write_bytes(b"hello ")
        text, offset = tail_log(p)
        assert text == "hello "
        p.write_bytes(b"hello world")
        text2, offset2 = tail_log(p, since_byte=offset)
        assert text2 == "world"
        assert offset2 == 11
        text3, offset3 = tail_log(p, since_byte=offset2)
        assert text3 == ""
        assert offset3 == 11

    def test_max_bytes_caps_chunk(self, tmp_path):
        p = tmp_path / "run.log"
        p.write_bytes(b"abcdef")
        text, offset = tail_log(p, since_byte=0, max_bytes=4)
        assert text == "abcd"
        assert offset == 4
