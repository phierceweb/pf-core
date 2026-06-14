"""JobRepo: create, transitions, progress, claim, retry, steps, events."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from pf_core.exceptions import InvalidInputError, PreconditionError
from pf_core.jobs import JobRepo, register_kind


# ---------------------------------------------------------------------------
# Create + read
# ---------------------------------------------------------------------------


def test_create_stores_inputs_and_returns_id(jobs_db, simple_kind):
    repo = JobRepo()
    job_id = repo.create(
        kind="simple_pass",
        inputs={"widget_ids": [1, 2], "config_id": 10},
        created_by="cli:test",
    )
    assert isinstance(job_id, int) and job_id > 0

    row = repo.get(job_id)
    assert row["kind"] == "simple_pass"
    assert row["status"] == "pending"
    assert row["inputs"] == {"widget_ids": [1, 2], "config_id": 10}
    assert row["created_by"] == "cli:test"
    assert row["progress_current"] == 0


def test_create_rejects_unknown_kind(jobs_db):
    with pytest.raises(Exception) as excinfo:
        JobRepo().create(kind="ghost")
    assert "ghost" in str(excinfo.value)


def test_create_validates_inputs_against_schema(jobs_db, simple_kind):
    with pytest.raises(InvalidInputError, match="failed schema validation"):
        JobRepo().create(
            kind="simple_pass",
            inputs={"widget_ids": "not a list"},
        )


def test_create_rejects_priority_out_of_range(jobs_db, simple_kind):
    with pytest.raises(InvalidInputError, match="priority"):
        JobRepo().create(kind="simple_pass", priority=500)


def test_find_filters_by_kind_status_creator(jobs_db, simple_kind):
    register_kind(kind="other_pass")
    repo = JobRepo()
    id1 = repo.create(kind="simple_pass", created_by="alice")
    id2 = repo.create(kind="simple_pass", created_by="bob")
    id3 = repo.create(kind="other_pass", created_by="alice")

    got = repo.find(kind="simple_pass")
    assert {r["id"] for r in got} == {id1, id2}

    got = repo.find(created_by="alice")
    assert {r["id"] for r in got} == {id1, id3}


def test_descendants_returns_child_jobs(jobs_db, simple_kind):
    repo = JobRepo()
    parent = repo.create(kind="simple_pass")
    c1 = repo.create(kind="simple_pass", parent_job_id=parent)
    c2 = repo.create(kind="simple_pass", parent_job_id=parent)
    unrelated = repo.create(kind="simple_pass")

    children = repo.descendants(parent)
    assert {r["id"] for r in children} == {c1, c2}
    assert unrelated not in {r["id"] for r in children}


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


def test_transition_follows_registered_rules(jobs_db, strict_kind):
    repo = JobRepo()
    job_id = repo.create(kind="strict_flow")

    repo.transition(job_id, "running")
    assert repo.get(job_id)["status"] == "running"
    assert repo.get(job_id)["started_at"] is not None

    repo.transition(job_id, "succeeded")
    row = repo.get(job_id)
    assert row["status"] == "succeeded"
    assert row["finished_at"] is not None


def test_transition_rejects_invalid_move(jobs_db, strict_kind):
    repo = JobRepo()
    job_id = repo.create(kind="strict_flow")
    # strict_kind does not allow pending → succeeded directly
    with pytest.raises(PreconditionError, match="cannot transition"):
        repo.transition(job_id, "succeeded")


def test_transition_validates_outputs_on_success(jobs_db, simple_kind):
    repo = JobRepo()
    job_id = repo.create(kind="simple_pass")
    repo.transition(job_id, "running")
    with pytest.raises(InvalidInputError, match="failed schema validation"):
        repo.transition(job_id, "succeeded", outputs={"not_the_right_shape": True})


def test_transition_records_outputs_and_error(jobs_db, simple_kind):
    repo = JobRepo()
    job_id = repo.create(kind="simple_pass")
    repo.transition(job_id, "running")
    repo.transition(
        job_id, "failed", error="database unreachable", error_class="OperationalError"
    )
    row = repo.get(job_id)
    assert row["status"] == "failed"
    assert row["error"] == "database unreachable"
    assert row["error_class"] == "OperationalError"
    assert row["finished_at"] is not None


def test_transition_raises_for_missing_job(jobs_db):
    with pytest.raises(PreconditionError, match="not found"):
        JobRepo().transition(99999, "running")


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------


def test_set_progress_updates_fields(jobs_db, simple_kind):
    repo = JobRepo()
    job_id = repo.create(kind="simple_pass")

    repo.set_progress(job_id, total=10)
    repo.set_progress(job_id, current=3, step="processing item 3")

    row = repo.get(job_id)
    assert row["progress_total"] == 10
    assert row["progress_current"] == 3
    assert row["current_step"] == "processing item 3"


def test_set_progress_rejects_negative(jobs_db, simple_kind):
    repo = JobRepo()
    job_id = repo.create(kind="simple_pass")
    with pytest.raises(InvalidInputError):
        repo.set_progress(job_id, current=-1)


# ---------------------------------------------------------------------------
# Worker claim
# ---------------------------------------------------------------------------


def test_claim_next_picks_highest_priority(jobs_db, simple_kind):
    repo = JobRepo()
    low = repo.create(kind="simple_pass", priority=10)
    high = repo.create(kind="simple_pass", priority=90)
    mid = repo.create(kind="simple_pass", priority=50)

    claimed = repo.claim_next(worker_id="w1")
    assert claimed["id"] == high
    assert claimed["claimed_by"] == "w1"

    next_claim = repo.claim_next(worker_id="w2")
    assert next_claim["id"] == mid

    last = repo.claim_next(worker_id="w3")
    assert last["id"] == low

    empty = repo.claim_next(worker_id="w4")
    assert empty is None


def test_claim_next_filters_by_kind(jobs_db, simple_kind):
    register_kind(kind="other_pass")
    repo = JobRepo()
    a = repo.create(kind="simple_pass")
    b = repo.create(kind="other_pass")

    claimed = repo.claim_next(kinds=["other_pass"], worker_id="w1")
    assert claimed["id"] == b

    leftover = repo.claim_next(kinds=["other_pass"], worker_id="w2")
    assert leftover is None

    simple = repo.claim_next(kinds=["simple_pass"], worker_id="w2")
    assert simple["id"] == a


def test_claim_next_skips_claimed_jobs(jobs_db, simple_kind):
    repo = JobRepo()
    repo.create(kind="simple_pass")
    first = repo.claim_next(worker_id="w1")
    assert first is not None

    # Second call finds nothing — the only job is claimed.
    second = repo.claim_next(worker_id="w2")
    assert second is None


def test_reclaim_stale_resets_expired_running_jobs(jobs_db, simple_kind):
    """A running job whose lease expired is reset to pending so another
    worker can pick it up."""
    from datetime import datetime

    from sqlalchemy import update

    from pf_core.db import transaction
    from pf_core.jobs import _schema as s

    repo = JobRepo()
    job_id = repo.create(kind="simple_pass")
    repo.claim_next(worker_id="w1")
    repo.transition(job_id, "running")

    # Simulate a stale claim by back-dating claimed_at far beyond the lease.
    past = datetime(2000, 1, 1, 0, 0, 0)
    with transaction() as conn:
        conn.execute(
            update(s.jobs).where(s.jobs.c.id == job_id).values(claimed_at=past)
        )

    reclaimed = repo.reclaim_stale(lease_seconds=10)
    assert reclaimed == 1

    row = repo.get(job_id)
    assert row["status"] == "pending"
    assert row["claimed_by"] is None

    # New worker can claim it.
    claimed = repo.claim_next(worker_id="w2")
    assert claimed["id"] == job_id


# ---------------------------------------------------------------------------
# Retry / cancel
# ---------------------------------------------------------------------------


def test_retry_resets_failed_job(jobs_db, simple_kind):
    repo = JobRepo()
    job_id = repo.create(kind="simple_pass", priority=40)
    repo.transition(job_id, "running")
    repo.transition(job_id, "failed", error="boom")

    repo.retry(job_id)

    row = repo.get(job_id)
    assert row["status"] == "pending"
    assert row["error"] is None
    assert row["error_class"] is None
    assert row["finished_at"] is None
    assert row["priority"] == 50  # bumped by 10


def test_retry_rejects_succeeded_jobs(jobs_db, simple_kind):
    repo = JobRepo()
    job_id = repo.create(kind="simple_pass")
    repo.transition(job_id, "running")
    repo.transition(job_id, "succeeded", outputs={"n_processed": 0})

    with pytest.raises(PreconditionError, match="only allowed from failed"):
        repo.retry(job_id)


def test_cancel_transitions_and_writes_event(jobs_db, simple_kind):
    repo = JobRepo()
    job_id = repo.create(kind="simple_pass")
    repo.cancel(job_id, reason="user aborted")

    row = repo.get(job_id)
    assert row["status"] == "canceled"

    events = repo.get_events(job_id, event_type="canceled")
    assert len(events) == 1
    assert events[0]["message"] == "user aborted"


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def test_start_and_finish_step(jobs_db, simple_kind):
    repo = JobRepo()
    job_id = repo.create(kind="simple_pass")
    repo.transition(job_id, "running")

    step_id = repo.start_step(job_id, name="grade_1", inputs={"submission_id": 1})
    time.sleep(0.01)
    repo.finish_step(step_id, outputs={"result": 28})

    step = repo.find_step(job_id, name="grade_1")
    assert step["status"] == "succeeded"
    assert step["outputs"] == {"result": 28}
    # Tight upper bound catches TZ drift: a 10 ms sleep should land under
    # 60 s; anything larger means started_at and server-now were compared
    # across time-zone frames (see finish_step).
    assert step["duration_ms"] is not None
    assert 0 <= step["duration_ms"] < 60_000


def test_step_index_auto_increments(jobs_db, simple_kind):
    repo = JobRepo()
    job_id = repo.create(kind="simple_pass")

    repo.start_step(job_id, name="a")
    repo.start_step(job_id, name="b")
    repo.start_step(job_id, name="c")

    # Indices are 0,1,2 — not colliding.
    with_steps = repo.get_with_steps(job_id)
    indices = [s["step_index"] for s in with_steps["steps"]]
    assert indices == [0, 1, 2]


def test_finish_step_rejects_invalid_status(jobs_db, simple_kind):
    repo = JobRepo()
    job_id = repo.create(kind="simple_pass")
    step_id = repo.start_step(job_id, name="a")
    with pytest.raises(InvalidInputError, match="succeeded/failed/skipped"):
        repo.finish_step(step_id, status="bogus")


def test_finish_step_auto_track_progress_succeeded(jobs_db):
    """``auto_track_progress=True`` increments ``progress_current`` by 1 on
    each succeeded step."""
    register_kind(kind="auto_pass", auto_track_progress=True)
    repo = JobRepo()
    job_id = repo.create(kind="auto_pass", progress_total=5)

    for name in ("a", "b", "c"):
        step_id = repo.start_step(job_id, name=name)
        repo.finish_step(step_id, status="succeeded")

    assert repo.get(job_id)["progress_current"] == 3


def test_finish_step_auto_track_progress_counts_failed(jobs_db):
    """Failed steps count too — ``progress_current`` is "work units finished
    this run", not "succeeded units"."""
    register_kind(kind="auto_pass", auto_track_progress=True)
    repo = JobRepo()
    job_id = repo.create(kind="auto_pass", progress_total=3)

    s1 = repo.start_step(job_id, name="a")
    repo.finish_step(s1, status="succeeded")
    s2 = repo.start_step(job_id, name="b")
    repo.finish_step(s2, status="failed", error="oops")

    assert repo.get(job_id)["progress_current"] == 2


def test_finish_step_auto_track_progress_skips_skipped(jobs_db):
    """Skipped steps represent resumed work that was already tallied — they
    must not double-count."""
    register_kind(kind="auto_pass", auto_track_progress=True)
    repo = JobRepo()
    job_id = repo.create(kind="auto_pass")

    s1 = repo.start_step(job_id, name="a")
    repo.finish_step(s1, status="succeeded")
    s2 = repo.start_step(job_id, name="b")
    repo.finish_step(s2, status="skipped")

    assert repo.get(job_id)["progress_current"] == 1


def test_finish_step_auto_track_progress_off_by_default(jobs_db, simple_kind):
    """Default behavior: step transitions don't move ``progress_current``."""
    repo = JobRepo()
    job_id = repo.create(kind="simple_pass", progress_total=2)

    s1 = repo.start_step(job_id, name="a")
    repo.finish_step(s1, status="succeeded")
    s2 = repo.start_step(job_id, name="b")
    repo.finish_step(s2, status="succeeded")

    assert repo.get(job_id)["progress_current"] == 0


def test_finish_step_auto_track_progress_explicit_set_progress_wins(jobs_db):
    """``set_progress(current=N)`` after auto-tracked steps overrides the
    counter — explicit caller intent wins last-write-wins."""
    register_kind(kind="auto_pass", auto_track_progress=True)
    repo = JobRepo()
    job_id = repo.create(kind="auto_pass", progress_total=10)

    s1 = repo.start_step(job_id, name="a")
    repo.finish_step(s1, status="succeeded")
    assert repo.get(job_id)["progress_current"] == 1

    repo.set_progress(job_id, current=42)
    assert repo.get(job_id)["progress_current"] == 42


def test_finish_step_clamps_negative_duration(jobs_db, simple_kind):
    """Regression: when ``started_at`` is later than ``server_now`` (a tiny
    cross-statement clock skew on MySQL), ``duration_ms`` is clamped to 0
    instead of being written as a negative integer that crashes the UPDATE
    on MySQL builds where the column is constrained ``>= 0``."""
    from sqlalchemy import update

    from pf_core.db import transaction
    from pf_core.jobs import _schema as s

    repo = JobRepo()
    job_id = repo.create(kind="simple_pass")
    step_id = repo.start_step(job_id, name="a")

    # Force ``started_at`` one second into the future relative to the
    # server clock — same shape as a 172 ms cross-statement skew, just
    # large enough to be unambiguous.
    future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=1)
    with transaction() as conn:
        conn.execute(
            update(s.job_steps)
            .where(s.job_steps.c.id == step_id)
            .values(started_at=future)
        )

    repo.finish_step(step_id, outputs={"result": 1})

    step = repo.find_step(job_id, name="a")
    assert step["duration_ms"] == 0


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def test_events_ordered_by_creation(jobs_db, simple_kind):
    repo = JobRepo()
    job_id = repo.create(kind="simple_pass")

    repo.add_event(job_id, event_type="info", message="started")
    repo.add_event(job_id, event_type="retry", message="backoff", context={"attempt": 1})
    repo.add_event(job_id, event_type="info", message="done")

    events = repo.get_events(job_id)
    assert [e["event_type"] for e in events] == ["info", "retry", "info"]
    assert events[1]["context"] == {"attempt": 1}

    retries = repo.get_events(job_id, event_type="retry")
    assert len(retries) == 1


# ---------------------------------------------------------------------------
# Timezone handling — datetime inputs, outputs, and server-side cutoffs
# ---------------------------------------------------------------------------


def test_get_returns_aware_utc_datetimes(jobs_db, simple_kind):
    """Read datetimes are stamped with ``tzinfo=timezone.utc`` so callers
    can compare against ``datetime.now(timezone.utc)`` directly without
    wrapping — the naive-datetime comparison foot-gun."""
    repo = JobRepo()
    job_id = repo.create(kind="simple_pass")

    row = repo.get(job_id)
    assert row["created_at"].tzinfo is not None
    assert row["created_at"].tzinfo.utcoffset(row["created_at"]) == timedelta(0)
    assert row["updated_at"].tzinfo is not None


def test_get_with_steps_coerces_all_nested_datetimes(jobs_db, simple_kind):
    """Job, steps, and events nested inside ``get_with_steps`` all get
    aware-UTC treatment so the bundle is safe to JSON-serialize."""
    repo = JobRepo()
    job_id = repo.create(kind="simple_pass")
    repo.transition(job_id, "running")
    step_id = repo.start_step(job_id, name="s1")
    repo.finish_step(step_id, outputs={"result": 1})
    repo.add_event(job_id, event_type="info", message="hi")

    bundle = repo.get_with_steps(job_id)
    assert bundle["created_at"].tzinfo is not None
    for step in bundle["steps"]:
        assert step["started_at"].tzinfo is not None
        if step["finished_at"] is not None:
            assert step["finished_at"].tzinfo is not None
    for event in bundle["events"]:
        assert event["created_at"].tzinfo is not None


def test_find_accepts_aware_and_naive_since(jobs_db, simple_kind):
    """``find(since=...)`` normalizes aware datetimes to naive UTC before
    binding, so aware/naive inputs produce the same result set."""
    repo = JobRepo()
    job_id = repo.create(kind="simple_pass")

    # A cutoff far in the past matches everything regardless of TZ frame.
    aware_utc = datetime(2000, 1, 1, tzinfo=timezone.utc)
    aware_local = datetime(2000, 1, 1, tzinfo=timezone(timedelta(hours=-6)))
    naive = datetime(2000, 1, 1)

    ids_utc = {r["id"] for r in repo.find(since=aware_utc)}
    ids_local = {r["id"] for r in repo.find(since=aware_local)}
    ids_naive = {r["id"] for r in repo.find(since=naive)}

    assert job_id in ids_utc
    assert ids_utc == ids_local == ids_naive


def test_find_since_future_excludes_everything(jobs_db, simple_kind):
    """An aware-UTC ``since`` in the future correctly excludes rows —
    proves the comparison happens in the right TZ frame (before the
    normalization fix this silently passed on UTC-local systems and
    failed with a ~6h window on MDT systems)."""
    repo = JobRepo()
    repo.create(kind="simple_pass")
    future = datetime.now(timezone.utc) + timedelta(days=1)
    assert repo.find(since=future) == []


def test_find_returns_aware_utc_rows(jobs_db, simple_kind):
    """Rows returned by ``find`` are stamped as aware UTC."""
    repo = JobRepo()
    repo.create(kind="simple_pass")
    rows = repo.find(kind="simple_pass")
    assert rows
    assert all(r["created_at"].tzinfo is not None for r in rows)


def test_find_step_returns_aware_utc(jobs_db, simple_kind):
    repo = JobRepo()
    job_id = repo.create(kind="simple_pass")
    repo.transition(job_id, "running")
    repo.start_step(job_id, name="s1")
    step = repo.find_step(job_id, name="s1")
    assert step["started_at"].tzinfo is not None


def test_get_events_returns_aware_utc(jobs_db, simple_kind):
    repo = JobRepo()
    job_id = repo.create(kind="simple_pass")
    repo.add_event(job_id, event_type="info", message="hello")
    events = repo.get_events(job_id)
    assert events
    assert events[0]["created_at"].tzinfo is not None


def test_claim_next_returns_aware_utc_row(jobs_db, simple_kind):
    repo = JobRepo()
    repo.create(kind="simple_pass")
    claimed = repo.claim_next(worker_id="w1")
    assert claimed is not None
    assert claimed["created_at"].tzinfo is not None
    assert claimed["claimed_at"].tzinfo is not None


def test_reclaim_stale_uses_server_side_cutoff(jobs_db, simple_kind):
    """A short positive lease exercises the server-side
    ``CURRENT_TIMESTAMP - INTERVAL N SECOND`` path; a claim older than
    the lease gets reset."""
    from sqlalchemy import update

    from pf_core.db import transaction
    from pf_core.jobs import _schema as s

    repo = JobRepo()
    job_id = repo.create(kind="simple_pass")
    repo.claim_next(worker_id="w1")
    repo.transition(job_id, "running")

    # Back-date claimed_at ~1 hour to make it clearly past a 1-second lease.
    past = datetime(2000, 1, 1, 0, 0, 0)
    with transaction() as conn:
        conn.execute(
            update(s.jobs).where(s.jobs.c.id == job_id).values(claimed_at=past)
        )

    assert repo.reclaim_stale(lease_seconds=1) == 1


def test_purge_uses_server_side_cutoff(jobs_db, simple_kind):
    """Back-dating ``finished_at`` far past the cutoff triggers deletion
    via the server-side ``CURRENT_TIMESTAMP - INTERVAL`` expression."""
    from sqlalchemy import update

    from pf_core.db import transaction
    from pf_core.jobs import _schema as s

    repo = JobRepo()
    job_id = repo.create(kind="simple_pass")
    repo.transition(job_id, "running")
    repo.transition(job_id, "succeeded", outputs={"n_processed": 0})

    # Force finished_at to a value comfortably before any reasonable cutoff
    # so the test doesn't depend on SQLite's seconds-precision CURRENT_TIMESTAMP.
    past = datetime(2000, 1, 1, 0, 0, 0)
    with transaction() as conn:
        conn.execute(
            update(s.jobs).where(s.jobs.c.id == job_id).values(finished_at=past)
        )

    assert repo.purge(older_than=timedelta(seconds=60)) == 1
    assert repo.get(job_id) is None


def test_purge_rejects_negative_interval(jobs_db, simple_kind):
    repo = JobRepo()
    with pytest.raises(InvalidInputError, match="non-negative"):
        repo.purge(older_than=timedelta(seconds=-1))
