# Jobs runtime

The execution layer for [jobs](jobs.md): a polling worker pool, a tracked subprocess runner, and a background thread submitter. The jobs *tables* record state; these modules are what actually claims and runs the work. Not to be confused with `pf_core.jobs.runtime` (the `Job` context manager these build on).

---

## Table of Contents

- [Which piece do I need?](#which-piece-do-i-need)
- [Worker pool + subprocess jobs](#worker-pool--subprocess-jobs)
- [Cancellation](#cancellation)
- [Thread submitter](#thread-submitter)
- [Test-suite integration](#test-suite-integration)

## Which piece do I need?

- **A queue consumed by long-lived workers** (web app runs conversions, a daemon drains kinds): `start_workers` + `run_subprocess_job` from `pf_core.jobs.workers`.
- **An HTTP request kicks off background work and the UI polls**: `submit_tracked` / `submit_detached` from `pf_core.jobs.submit`.
- **Operators watching/canceling either**: mount the [jobs dashboard](jobs-admin.md).

## Worker pool + subprocess jobs

```python
from functools import partial
from pf_core.jobs.workers import SubprocessJobSpec, run_subprocess_job, start_workers, stop_workers

spec = SubprocessJobSpec(
    name="convert",
    argv=lambda job: build_command(job["inputs"]),          # domain: inputs → CLI
    log_path=lambda job: out_dir(job) / f"job-{job['id']}.log",
    outputs=lambda job, rc: {"returncode": rc},
    job_id_env="MYAPP_JOB_ID",                               # default: PF_JOB_ID
)
handle = start_workers(kinds=["myapp_convert"],
                       run=partial(run_subprocess_job, spec=spec),
                       concurrency=cfg.concurrency)
...
stop_workers(handle)     # stops claiming; live subprocesses keep running
```

- `start_workers` sweeps `reclaim_stale()` first (disable with `reclaim_on_start=False`) so jobs stranded `running` by a killed worker re-enter the queue. Poll cadence: `poll_seconds=` or the `JOB_POLL_SECONDS` env var (default 1).
- The claim loop never dies on an error — claim and run failures log (`worker_claim_failed` / `worker_run_failed`) and the loop continues.
- `run_subprocess_job` runs the child in its own session, merges stderr into the spec's log file (with a `$ argv` header), exports the job id as `spec.job_id_env` — so tracked LLM calls inside the child attribute their runs — and maps exit 0 → `succeeded` (with `spec.outputs`), nonzero → `failed` naming the code and log path.

## Cancellation

Cancel is two-part, in either order: transition the row (`repo.cancel` or the dashboard's cancel endpoint) and `terminate_job(job_id)` the process. The runner re-checks the row after the child exits and leaves an already-`canceled` job untouched. `terminate_job` signals the process group SIGTERM, then SIGKILL after `escalate_after` (default 5s) if it survives. `tail_log(path, since_byte)` is the byte-offset log reader the dashboard's polling uses; offsets advance by bytes read.

## Thread submitter

```python
from pf_core.jobs.submit import JobAlreadyRunning, submit_detached, submit_tracked

job_id = submit_tracked(
    kind="grading_pass", inputs={"section_folder": str(folder)},
    created_by="web", run=lambda progress: service.run(folder, progress),
    dedup_key=lambda inputs: inputs.get("section_folder", "").endswith(suffix),
)
```

- `submit_tracked` creates the row, returns its id immediately, and runs the work on a daemon thread inside a `Job` window: `running` → your `run(progress_callback)` → `succeeded` (failures are recorded by the `Job` context manager). The callback signature is `(done, total, message=None)`.
- `submit_detached` is for services that create and manage their own job: it runs them on a thread and resolves the new job's id (or `None` when the service had nothing to do).
- Dedup is opt-in and scope-blind: `dedup_key` receives each non-terminal same-kind job's parsed `inputs` and returns whether it occupies your scope; a match raises `JobAlreadyRunning`. What "scope" means (a section, a tenant) stays in the consumer.

## Test-suite integration

Threads from the submitter outlive requests; drain them before the test database is disposed by wiring `wait_all` as the [testing](testing.md) teardown hook:

```python
@pytest.fixture
def pf_engine_teardown():
    from pf_core.jobs.submit import wait_all
    return lambda: wait_all(timeout=10.0)
```
