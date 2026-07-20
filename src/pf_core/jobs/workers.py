"""Polling worker pool + tracked subprocess execution for pf-core jobs.

``start_workers`` runs N daemon threads over :meth:`JobRepo.claim_next`,
calling ``run(job_row)`` for each claim; pair it with
:func:`run_subprocess_job` (via a :class:`SubprocessJobSpec`) when a job
executes as a child process. ``reclaim_on_start`` sweeps expired leases so
jobs stranded ``running`` by a killed worker return to the queue.

Cancellation is two-part by design: transition the row (``repo.cancel`` /
the jobs-admin route) *and* :func:`terminate_job` the process. The runner
checks for a ``canceled`` row before writing its own terminal transition,
so either order works. ``stop_workers`` stops claiming and joins the
threads; it never kills live subprocesses — cancel those explicitly.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pf_core.jobs.repo import JobRepo
from pf_core.jobs.runtime import Job
from pf_core.log import get_logger
from pf_core.utils.env import resolve_int

logger = get_logger(__name__)

__all__ = [
    "SubprocessJobSpec",
    "WorkerHandle",
    "run_subprocess_job",
    "start_workers",
    "stop_workers",
    "tail_log",
    "terminate_job",
]

_RUNNING: dict[int, subprocess.Popen] = {}
_RUNNING_LOCK = threading.Lock()


@dataclass(frozen=True)
class SubprocessJobSpec:
    """How jobs of one kind-family execute as a subprocess.

    Args:
        name: Short label stamped into logs and failure messages.
        argv: ``job_row -> command list`` (domain: inputs → CLI invocation).
        log_path: ``job_row -> Path`` for the job's combined stdout+stderr log.
        outputs: Optional ``(job_row, returncode) -> outputs`` for the
            succeeded transition.
        job_id_env: Env var carrying the job id into the child, so tracked
            LLM calls in the subprocess attribute their runs.
        cwd: Working directory for the child (``None`` = inherit).
    """

    name: str
    argv: Callable[[dict], list[str]]
    log_path: Callable[[dict], Path]
    outputs: Callable[[dict, int], Any] | None = None
    job_id_env: str = "PF_JOB_ID"
    cwd: Path | None = None


@dataclass
class WorkerHandle:
    """Threads + stop signal returned by :func:`start_workers`."""

    threads: list[threading.Thread] = field(default_factory=list)
    stop_event: threading.Event = field(default_factory=threading.Event)


def _poll_seconds(override: float | None) -> float:
    if override is not None:
        return float(override)
    return float(resolve_int(None, "JOB_POLL_SECONDS", default=1))


def _loop(
    worker_id: str,
    stop_event: threading.Event,
    run: Callable[[dict], None],
    kinds: list[str],
    poll: float,
) -> None:
    repo = JobRepo()
    while not stop_event.is_set():
        try:
            job = repo.claim_next(kinds=kinds, worker_id=worker_id)
        except Exception as exc:
            logger.warning("worker_claim_failed", worker=worker_id, error=repr(exc))
            stop_event.wait(poll)
            continue
        if job is None:
            stop_event.wait(poll)
            continue
        try:
            run(job)
        except Exception as exc:
            logger.warning(
                "worker_run_failed", worker=worker_id, job_id=job.get("id"), error=repr(exc)
            )


def start_workers(
    *,
    kinds: list[str],
    run: Callable[[dict], None],
    concurrency: int = 1,
    worker_id_prefix: str = "worker",
    poll_seconds: float | None = None,
    reclaim_on_start: bool = True,
) -> WorkerHandle:
    """Start ``concurrency`` daemon claim-loop threads for ``kinds``.

    ``reclaim_on_start`` runs :meth:`JobRepo.reclaim_stale` first, so jobs
    left ``running`` with an expired lease (a previously killed worker)
    re-enter the queue instead of hanging forever.
    """
    if reclaim_on_start:
        reclaimed = JobRepo().reclaim_stale()
        if reclaimed:
            logger.info("worker_reclaimed_stale", count=reclaimed)

    handle = WorkerHandle()
    poll = _poll_seconds(poll_seconds)
    for i in range(max(1, concurrency)):
        worker_id = f"{worker_id_prefix}-{os.getpid()}-{i}"
        t = threading.Thread(
            target=_loop,
            args=(worker_id, handle.stop_event, run, list(kinds), poll),
            daemon=True,
            name=worker_id,
        )
        t.start()
        handle.threads.append(t)
    return handle


def stop_workers(handle: WorkerHandle, *, timeout: float = 2.0) -> None:
    """Stop claiming and join the loop threads (live subprocesses keep running)."""
    handle.stop_event.set()
    for t in handle.threads:
        t.join(timeout=timeout)


def run_subprocess_job(job_row: dict, spec: SubprocessJobSpec) -> None:
    """Run one claimed job as a subprocess and transition it terminally.

    The child runs in its own session (so :func:`terminate_job` can signal
    the whole process group) with stdout+stderr merged into the spec's log
    file and the job id exported as ``spec.job_id_env``. Exit 0 →
    ``succeeded`` (with ``spec.outputs`` when given); nonzero → ``failed``
    naming the exit code and log path — unless the row was already
    ``canceled``, in which case the runner leaves it alone.
    """
    job_id = int(job_row["id"])
    lp = spec.log_path(job_row)
    lp.parent.mkdir(parents=True, exist_ok=True)
    cmd = spec.argv(job_row)
    env = dict(os.environ)
    env[spec.job_id_env] = str(job_id)

    repo = JobRepo()
    with Job(job_id, repo=repo) as job:
        job.transition("running")
        job.event("started", " ".join(cmd))
        with open(lp, "wb", buffering=0) as log_fh:
            log_fh.write(f"$ {' '.join(cmd)}\n\n".encode())
            proc = subprocess.Popen(
                cmd,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=str(spec.cwd) if spec.cwd else None,
                start_new_session=True,
            )
            with _RUNNING_LOCK:
                _RUNNING[job_id] = proc
            try:
                rc = proc.wait()
            finally:
                with _RUNNING_LOCK:
                    _RUNNING.pop(job_id, None)

        if (repo.get(job_id) or {}).get("status") == "canceled":
            return
        if rc == 0:
            if spec.outputs is not None:
                job.outputs = spec.outputs(job_row, rc)
            job.transition("succeeded")
        else:
            job.transition("failed", error=f"{spec.name} exited {rc} (see {lp})")


def terminate_job(job_id: int, *, escalate_after: float = 5.0) -> bool:
    """SIGTERM a running job's process group; SIGKILL if it survives.

    Returns True when a process was found (or had already exited), False
    when no subprocess is registered for the id or signaling failed.
    """
    with _RUNNING_LOCK:
        proc = _RUNNING.get(job_id)
    if proc is None:
        return False
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        return False

    def _force_kill() -> None:
        time.sleep(escalate_after)
        if proc.poll() is None:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass

    threading.Thread(target=_force_kill, daemon=True).start()
    return True


def tail_log(
    path: Path, since_byte: int = 0, max_bytes: int = 256 * 1024
) -> tuple[str, int]:
    """Read the log from ``since_byte``; return ``(text, next_since_byte)``.

    The offset advances by *bytes* read (decode is ``errors="replace"``),
    so callers loop with the returned offset. Missing file → ``("", since)``.
    """
    p = Path(path)
    if not p.is_file():
        return "", since_byte
    size = p.stat().st_size
    if size <= since_byte:
        return "", size
    with open(p, "rb") as fh:
        fh.seek(since_byte)
        chunk = fh.read(min(max_bytes, size - since_byte))
    return chunk.decode("utf-8", errors="replace"), since_byte + len(chunk)
