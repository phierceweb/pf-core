"""Tests for ``pf_core.llm.tracked.tracked_call``.

Exercises the orchestration this module adds on top of existing
primitives:

- Success (raw): one row, ``status="success"``, spec-resolved
  ``system_prompt_id``, rendered prompt stored, fingerprint mapped.
- ``expect_json``: parsed object returned.
- Failure: ``status="failed"`` row written, original exception re-raised.
- JSON parse failure → one tracked retry; retry row linked via
  ``llm_run_links.relation="retry"``; parsed value + retry run_id returned.
- Both attempts unparseable → :class:`LlmJsonError` with ``.raw`` set.
- ``json_retry=False`` → raises immediately, only one row.
- ``style="brace"`` renders without upper-casing kwarg keys.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from pf_core.llm import LlmJsonError, tracked_call
from pf_core.llm.tracking import (
    LlmRunRepo,
    clear_resolver_caches,
    metadata,
)
from pf_core.llm.tracking import schema as s


@pytest.fixture(autouse=True)
def _clear_caches_between_tests():
    clear_resolver_caches()
    yield
    clear_resolver_caches()


@pytest.fixture()
def tracking_db(pf_engine):
    metadata.create_all(pf_engine)
    yield pf_engine
    metadata.drop_all(pf_engine)


_SPEC = {"agent": "classifier", "version": 3, "system": "You are @@ROLE@@."}


class _FakeClient:
    """Returns queued (content, usage) results; can raise instead.

    Each ``chat`` call pops the next scripted result. A result that is an
    ``Exception`` instance is raised; otherwise it is a ``(content, usage)``
    tuple returned verbatim.
    """

    def __init__(self, *results):
        self._results = list(results)
        self.calls: list[dict] = []

    def chat(self, *, messages, model, **kwargs):
        self.calls.append({"messages": messages, "model": model, **kwargs})
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _links(engine, child_run_id: int) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(s.llm_run_links).where(
                s.llm_run_links.c.child_run_id == child_run_id
            )
        ).mappings().fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Success
# ---------------------------------------------------------------------------


def test_success_returns_raw_and_records_one_row(tracking_db):
    client = _FakeClient(
        ("hello world", {"duration_ms": 1234, "system_fingerprint": "fp_z"})
    )

    content, run_id = tracked_call(
        client=client,
        agent_type="classifier",
        spec=_SPEC,
        model="haiku",
        render_kwargs={"role": "a triager"},
    )

    assert content == "hello world"
    # @@ROLE@@ replaced; value not upper-cased, only the key.
    assert client.calls[0]["messages"][0]["content"] == "You are a triager."
    assert client.calls[0]["model"] == "haiku"

    row = LlmRunRepo().get(run_id)
    assert row["status"] == "success"
    assert row["duration_ms"] == 1234
    assert row["model_fingerprint"] == "fp_z"
    assert row["system_prompt_id"] is not None

    full = LlmRunRepo().get_with_payload(run_id)
    assert full["payload"]["rendered_system"] == "You are a triager."
    assert full["payload"]["raw_response"] == "hello world"


def test_expect_json_returns_parsed_object(tracking_db):
    client = _FakeClient(('```json\n{"ok": true}\n```', {"duration_ms": 5}))

    parsed, run_id = tracked_call(
        client=client,
        agent_type="classifier",
        spec=_SPEC,
        model="haiku",
        render_kwargs={"role": "x"},
        expect_json=True,
    )

    assert parsed == {"ok": True}
    assert LlmRunRepo().get(run_id)["status"] == "success"


def test_brace_style_does_not_uppercase_keys(tracking_db):
    spec = {"agent": "classifier", "version": 1, "system": "Hi {name}."}
    client = _FakeClient(("done", {"duration_ms": 1}))

    tracked_call(
        client=client,
        agent_type="classifier",
        spec=spec,
        model="haiku",
        render_kwargs={"name": "Sam"},
        style="brace",
    )

    assert client.calls[0]["messages"][0]["content"] == "Hi Sam."


# ---------------------------------------------------------------------------
# Failure path
# ---------------------------------------------------------------------------


def test_client_failure_records_failed_row_and_reraises(tracking_db):
    boom = RuntimeError("subprocess exploded")
    client = _FakeClient(boom)

    with pytest.raises(RuntimeError, match="subprocess exploded"):
        tracked_call(
            client=client,
            agent_type="classifier",
            spec=_SPEC,
            model="haiku",
            render_kwargs={"role": "x"},
        )

    with tracking_db.connect() as conn:
        rows = conn.execute(select(s.llm_runs)).mappings().fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert rows[0]["error_class"] == "RuntimeError"
    assert "subprocess exploded" in rows[0]["error"]


# ---------------------------------------------------------------------------
# JSON retry
# ---------------------------------------------------------------------------


def test_json_parse_failure_retries_and_links_retry_row(tracking_db):
    client = _FakeClient(
        ("not json at all", {"duration_ms": 1}),
        ('{"recovered": 1}', {"duration_ms": 2}),
    )

    parsed, run_id = tracked_call(
        client=client,
        agent_type="classifier",
        spec=_SPEC,
        model="haiku",
        render_kwargs={"role": "x"},
        expect_json=True,
    )

    assert parsed == {"recovered": 1}
    # Two rows written; the returned run_id is the retry's.
    with tracking_db.connect() as conn:
        all_runs = conn.execute(select(s.llm_runs)).mappings().fetchall()
    assert len(all_runs) == 2

    links = _links(tracking_db, run_id)
    assert len(links) == 1
    assert links[0]["relation"] == "retry"
    # Parent is the first (failed-parse) run, distinct from the retry.
    assert links[0]["parent_run_id"] != run_id


def test_both_attempts_unparseable_raises_llmjsonerror(tracking_db):
    client = _FakeClient(
        ("garbage one", {"duration_ms": 1}),
        ("garbage two", {"duration_ms": 2}),
    )

    with pytest.raises(LlmJsonError) as excinfo:
        tracked_call(
            client=client,
            agent_type="classifier",
            spec=_SPEC,
            model="haiku",
            render_kwargs={"role": "x"},
            expect_json=True,
        )

    assert excinfo.value.raw == "garbage two"


def test_json_retry_disabled_raises_without_second_call(tracking_db):
    client = _FakeClient(("garbage", {"duration_ms": 1}))

    with pytest.raises(LlmJsonError) as excinfo:
        tracked_call(
            client=client,
            agent_type="classifier",
            spec=_SPEC,
            model="haiku",
            render_kwargs={"role": "x"},
            expect_json=True,
            json_retry=False,
        )

    assert excinfo.value.raw == "garbage"
    assert len(client.calls) == 1
    with tracking_db.connect() as conn:
        rows = conn.execute(select(s.llm_runs)).mappings().fetchall()
    assert len(rows) == 1
