# Recipe: Bridging pf-core jobs to project entities

Pattern for linking `pf_core.jobs.jobs.id` to a project-owned domain entity (e.g. `summary_pass.id`) when the domain entity is conceptually one job-worth of work.

## When to use

- You have a domain entity that represents "one batch of work the user triggered" (`summary_pass`, `backfill_run`, `import_pass`)
- That entity predates pf-core jobs, or has its own lifecycle fields you don't want to move into `jobs`
- You want pf-core's job machinery (claim/reclaim, progress, events, admin visibility) without rewriting the domain entity

## When NOT to use

- If the domain entity only exists to track *a job* and has no other purpose: just use `jobs` directly and put a `kind="my_domain"` tag on it
- If you can fit `job_id` FK directly on a domain row — always prefer a column over a bridge table

## The bridge table

```sql
CREATE TABLE summary_job_refs (
    summary_pass_id  INT NOT NULL,
    job_id           INT NOT NULL,     -- FK to jobs(id)
    created_at       TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (summary_pass_id, job_id),
    INDEX idx_job (job_id)
);
```

Composite PK allows historical retry chains (one `summary_pass` → N jobs over its lifetime).

## The repo module

```python
# app/repo/job_refs.py
from functools import lru_cache
from pf_core.db.connection import transaction
from pf_core.jobs.repo import JobRepo

_DOMAIN_CACHE: dict[int, int] = {}  # summary_pass_id → current job_id


def create_summary_job(*, summary_pass_id: int, kind: str = "summary_pass") -> int:
    """Create a pf-core job and bridge it to an existing summary_pass."""
    job_id = JobRepo().create(kind=kind, inputs={"summary_pass_id": summary_pass_id})
    with transaction() as conn:
        conn.execute(
            "INSERT INTO summary_job_refs (summary_pass_id, job_id) VALUES (:p, :j)",
            {"p": summary_pass_id, "j": job_id},
        )
    _DOMAIN_CACHE[summary_pass_id] = job_id
    return job_id


def current_job_id(summary_pass_id: int) -> int | None:
    """Return the most recent job_id for a summary_pass, or None."""
    if summary_pass_id in _DOMAIN_CACHE:
        return _DOMAIN_CACHE[summary_pass_id]
    with transaction() as conn:
        row = conn.execute(
            "SELECT job_id FROM summary_job_refs "
            "WHERE summary_pass_id=:p ORDER BY created_at DESC LIMIT 1",
            {"p": summary_pass_id},
        ).fetchone()
    if row:
        _DOMAIN_CACHE[summary_pass_id] = int(row[0])
        return int(row[0])
    return None


def all_job_ids(summary_pass_id: int) -> list[int]:
    """Full chain of job_ids (includes retries) — chronological."""
    with transaction() as conn:
        return [
            int(r[0]) for r in conn.execute(
                "SELECT job_id FROM summary_job_refs "
                "WHERE summary_pass_id=:p ORDER BY created_at",
                {"p": summary_pass_id},
            ).fetchall()
        ]
```

## Cache discipline

- The module-level dict is a single-process write-through cache. Safe because *creation* is the only write; reads are read-through.
- Never invalidate on job completion — the mapping is historical, not lifecycle-sensitive
- Pop from the cache only on explicit retry (`create_summary_job` with same `summary_pass_id` overwrites), which is fine since writes go through the same function

## Coordinating the lifecycle

```python
# app/services/summarizer.py
from pf_core.jobs.runtime import Job
from app.repo.job_refs import create_summary_job

def run_summary(summary_pass_id: int) -> None:
    job_id = create_summary_job(summary_pass_id=summary_pass_id, kind="summary_pass")
    with Job(job_id):
        # All LLM calls inside this block get llm_runs.job_id = job_id
        # The admin's /admin/llm/job/{id} page shows per-call detail
        _do_batch(summary_pass_id)
```

## Chain traversal

When a `summary_pass` retries, create a new job + bridge row. Staleness queries can then ask "is the latest job for this pass still running?":

```python
latest = current_job_id(summary_pass_id)
if latest and JobRepo().get(latest)["status"] == "running":
    ...
```

Historical chain: `all_job_ids()` walks every retry.

## Don't shed the bridge

When the domain entity has no other fields besides job-ish ones, you might be tempted to drop the bridge and key everything by `job_id`. Resist unless the domain columns truly have no non-job consumers — migrating away from a domain-entity PK is painful. The bridge is cheap (two columns + index); keep it until the domain entity has genuinely nothing left to own.

## Alternative: direct FK

If you're designing the domain entity fresh, put `job_id` on it directly:

```sql
ALTER TABLE summary_pass ADD COLUMN job_id INT NULL REFERENCES jobs(id);
CREATE INDEX idx_summary_pass_job ON summary_pass(job_id);
```

Retries become new rows in `summary_pass` (which is probably what you want anyway). No bridge table needed. Use the bridge pattern only when retrofitting.
