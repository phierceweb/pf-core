"""Tests for tracked_messages_call — the messages-based tracked call.

Unlike tracked_call (spec-render → single user message), this variant takes
a verbatim message list and records it with optional spec-based prompt ids,
input_hash, configs, tags/metrics, and failure rows.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text


SPEC = {
    "agent": "probe",
    "version": 4,
    "system": "You are @@ROLE@@.",
    "user": "Grade: @@THING@@",
}

MESSAGES = [
    {"role": "system", "content": "You are a summarizer."},
    {"role": "user", "content": "Summarize this text."},
]


class FakeChat:
    def __init__(self, *, raise_exc: Exception | None = None):
        self.raise_exc = raise_exc
        self.calls: list[dict[str, Any]] = []

    def chat(self, *, messages, model="", **kwargs):
        self.calls.append({"messages": messages, "model": model, **kwargs})
        if self.raise_exc:
            raise self.raise_exc
        return "summarized!", {
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "cost_usd": 0.001,
            "duration_ms": 321,
            "system_fingerprint": "fp_x",
        }


@pytest.fixture
def pf_schema():
    from pf_core.testing.db_fixtures import framework_ddl

    return framework_ddl()


def _one_run(conn) -> dict:
    rows = [
        dict(r)
        for r in conn.execute(
            text(
                "SELECT id, status, provider, temperature, max_tokens, items_out, "
                "input_hash, system_prompt_id, user_prompt_id, model_fingerprint, "
                "error_class, duration_ms FROM llm_runs"
            )
        ).mappings()
    ]
    assert len(rows) == 1, rows
    return rows[0]


class TestSuccessPath:
    def test_records_row_and_returns_triple(self, pf_tables, pf_connection):
        from pf_core.llm.tracked import tracked_messages_call

        client = FakeChat()
        content, usage, run_id = tracked_messages_call(
            client=client,
            agent_type="probe",
            messages=MESSAGES,
            model="test-model",
            sampling={"temperature": 0.2, "max_tokens": 64},
            provider="openrouter",
        )
        assert content == "summarized!"
        assert usage["cost_usd"] == 0.001
        assert isinstance(run_id, int)

        run = _one_run(pf_connection)
        assert run["status"] == "success"
        assert run["provider"] == "openrouter"
        assert run["temperature"] == 0.2
        assert run["max_tokens"] == 64
        assert run["model_fingerprint"] == "fp_x"
        assert run["duration_ms"] == 321

        payload = pf_connection.execute(
            text("SELECT rendered_system, rendered_user, raw_response FROM llm_run_payloads")
        ).fetchone()
        assert payload[0] == "You are a summarizer."
        assert payload[1] == "Summarize this text."
        assert payload[2] == "summarized!"

    def test_chat_kwargs_forwarded_but_not_recorded_as_sampling(
        self, pf_tables, pf_connection
    ):
        from pf_core.llm.tracked import tracked_messages_call

        client = FakeChat()
        tracked_messages_call(
            client=client,
            agent_type="probe",
            messages=MESSAGES,
            model="test-model",
            sampling={"temperature": 0.1},
            chat_kwargs={"response_format": {"type": "json_object"}, "timeout": 5},
        )
        call = client.calls[0]
        assert call["response_format"] == {"type": "json_object"}
        assert call["timeout"] == 5
        assert call["temperature"] == 0.1
        assert _one_run(pf_connection)["temperature"] == 0.1

    def test_spec_registers_system_and_user_prompt_ids(self, pf_tables, pf_connection):
        from pf_core.llm.tracked import tracked_messages_call

        tracked_messages_call(
            client=FakeChat(),
            agent_type="probe",
            messages=MESSAGES,
            model="test-model",
            spec=SPEC,
        )
        run = _one_run(pf_connection)
        prompts = {
            r[0]: (r[1], r[2])
            for r in pf_connection.execute(
                text("SELECT part, id, version FROM llm_prompts")
            )
        }
        assert prompts["system"] == (run["system_prompt_id"], 4)
        assert prompts["user"] == (run["user_prompt_id"], 4)

    def test_minimal_spec_without_user_part(self, pf_tables, pf_connection):
        from pf_core.llm.tracked import tracked_messages_call

        tracked_messages_call(
            client=FakeChat(),
            agent_type="probe",
            messages=MESSAGES,
            model="test-model",
            spec={"version": 2, "system": "canonical system text"},
        )
        run = _one_run(pf_connection)
        assert run["system_prompt_id"] is not None
        assert run["user_prompt_id"] is None
        parts = [r[0] for r in pf_connection.execute(text("SELECT part FROM llm_prompts"))]
        assert parts == ["system"]

    def test_input_hash_configs_tags_metrics_items_out(self, pf_tables, pf_connection):
        from pf_core.llm.tracked import tracked_messages_call

        tracked_messages_call(
            client=FakeChat(),
            agent_type="probe",
            messages=MESSAGES,
            model="test-model",
            input_hash="a" * 64,
            configs={"report_config": 7},
            tags=["env:test"],
            metrics={"pages": 3.0},
            items_out=1,
        )
        run = _one_run(pf_connection)
        assert run["input_hash"] == "a" * 64
        assert run["items_out"] == 1
        cfg = pf_connection.execute(
            text("SELECT config_kind, config_id FROM llm_run_configs")
        ).fetchone()
        assert tuple(cfg) == ("report_config", 7)
        assert pf_connection.execute(
            text("SELECT tag FROM llm_run_tags")
        ).fetchone()[0] == "env:test"
        assert pf_connection.execute(
            text("SELECT metric_name, metric_value FROM llm_run_metrics")
        ).fetchone()[1] == 3.0


class TestFailurePath:
    def test_client_error_records_failed_row_and_reraises(
        self, pf_tables, pf_connection
    ):
        from pf_core.llm.tracked import tracked_messages_call

        with pytest.raises(RuntimeError, match="boom"):
            tracked_messages_call(
                client=FakeChat(raise_exc=RuntimeError("boom")),
                agent_type="probe",
                messages=MESSAGES,
                model="test-model",
                provider="openrouter",
            )
        run = _one_run(pf_connection)
        assert run["status"] == "failed"
        assert run["error_class"] == "RuntimeError"
        assert run["duration_ms"] is not None

    def test_on_record_error_warn_returns_none_run_id(self, pf_tables):
        from pf_core.llm.tracked import tracked_messages_call

        class BrokenRepo:
            def record(self, **kwargs):
                raise ConnectionError("sink down")

        content, usage, run_id = tracked_messages_call(
            client=FakeChat(),
            agent_type="probe",
            messages=MESSAGES,
            model="test-model",
            repo=BrokenRepo(),
            on_record_error="warn",
        )
        assert content == "summarized!"
        assert run_id is None

    def test_on_record_error_raise_is_default(self, pf_tables):
        from pf_core.llm.tracked import tracked_messages_call

        class BrokenRepo:
            def record(self, **kwargs):
                raise ConnectionError("sink down")

        with pytest.raises(ConnectionError):
            tracked_messages_call(
                client=FakeChat(),
                agent_type="probe",
                messages=MESSAGES,
                model="test-model",
                repo=BrokenRepo(),
            )


class TestMetadataAndWindow:
    def test_metadata_splits_into_tags_and_metrics(self, pf_tables, pf_connection):
        from pf_core.llm.tracked import tracked_messages_call

        tracked_messages_call(
            client=FakeChat(),
            agent_type="probe",
            messages=MESSAGES,
            model="test-model",
            metadata={"source_name": "report.pdf", "verified": True, "pages": 3},
        )
        tags = {r[0] for r in pf_connection.execute(text("SELECT tag FROM llm_run_tags"))}
        assert tags == {"source_name:report.pdf", "verified:true"}
        metrics = {
            r[0]: r[1]
            for r in pf_connection.execute(
                text("SELECT metric_name, metric_value FROM llm_run_metrics")
            )
        }
        assert metrics == {"pages": 3.0}

    def test_session_metadata_merges_call_wins(self, pf_tables, pf_connection):
        from pf_core.llm.recording import begin_call_recording, end_call_recording
        from pf_core.llm.tracked import tracked_messages_call

        begin_call_recording(
            session_metadata={"source_name": "session.pdf", "batch": "b1"}
        )
        try:
            tracked_messages_call(
                client=FakeChat(),
                agent_type="probe",
                messages=MESSAGES,
                model="test-model",
                metadata={"source_name": "call.pdf"},
            )
        finally:
            end_call_recording()
        tags = {r[0] for r in pf_connection.execute(text("SELECT tag FROM llm_run_tags"))}
        assert tags == {"source_name:call.pdf", "batch:b1"}

    def test_explicit_tags_metrics_merge_with_metadata(self, pf_tables, pf_connection):
        from pf_core.llm.tracked import tracked_messages_call

        tracked_messages_call(
            client=FakeChat(),
            agent_type="probe",
            messages=MESSAGES,
            model="test-model",
            metadata={"env": "test", "pages": 3},
            tags=["env:test", "extra:tag"],
            metrics={"pages": 9.0},
        )
        tags = [r[0] for r in pf_connection.execute(text("SELECT tag FROM llm_run_tags"))]
        assert sorted(tags) == ["env:test", "extra:tag"]
        metrics = {
            r[0]: r[1]
            for r in pf_connection.execute(
                text("SELECT metric_name, metric_value FROM llm_run_metrics")
            )
        }
        assert metrics == {"pages": 9.0}

    def test_job_id_explicit_beats_ambient(self, pf_tables, pf_connection):
        from pf_core.jobs import Job, JobRepo, register_kind
        from pf_core.llm.tracked import tracked_messages_call

        register_kind(kind="batch")
        ambient_id = JobRepo().create(kind="batch", inputs={}, created_by="test")
        explicit_id = JobRepo().create(kind="batch", inputs={}, created_by="test")
        with Job(ambient_id):
            tracked_messages_call(
                client=FakeChat(),
                agent_type="probe",
                messages=MESSAGES,
                model="test-model",
                job_id=explicit_id,
            )
        row = pf_connection.execute(text("SELECT job_id FROM llm_runs")).fetchone()
        assert row[0] == explicit_id

    def test_summary_appended_on_success(self, pf_tables, pf_connection):
        from pf_core.llm.recording import begin_call_recording, end_call_recording
        from pf_core.llm.tracked import tracked_messages_call

        begin_call_recording()
        try:
            content, usage, run_id = tracked_messages_call(
                client=FakeChat(),
                agent_type="probe",
                messages=MESSAGES,
                model="test-model",
                provider="openrouter",
                spec=SPEC,
            )
        finally:
            records = end_call_recording()
        assert records == [
            {
                "agent_type": "probe",
                "model": "test-model",
                "provider": "openrouter",
                "prompt_version": 4,
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "cost_usd": 0.001,
                "duration_ms": 321,
                "success": True,
                "run_id": run_id,
            }
        ]

    def test_summary_appended_on_failure(self, pf_tables, pf_connection):
        from pf_core.llm.recording import begin_call_recording, end_call_recording
        from pf_core.llm.tracked import tracked_messages_call

        begin_call_recording()
        try:
            with pytest.raises(RuntimeError, match="boom"):
                tracked_messages_call(
                    client=FakeChat(raise_exc=RuntimeError("boom")),
                    agent_type="probe",
                    messages=MESSAGES,
                    model="test-model",
                )
        finally:
            records = end_call_recording()
        assert len(records) == 1
        rec = records[0]
        assert rec["success"] is False
        assert rec["prompt_tokens"] == 0
        assert rec["cost_usd"] == 0.0
        assert rec["prompt_version"] is None
        assert isinstance(rec["run_id"], int)

    def test_failed_row_carries_tags_and_metrics(self, pf_tables, pf_connection):
        from pf_core.llm.tracked import tracked_messages_call

        with pytest.raises(RuntimeError):
            tracked_messages_call(
                client=FakeChat(raise_exc=RuntimeError("boom")),
                agent_type="probe",
                messages=MESSAGES,
                model="test-model",
                metadata={"source_name": "report.pdf", "pages": 3},
            )
        tag = pf_connection.execute(text("SELECT tag FROM llm_run_tags")).fetchone()
        assert tag[0] == "source_name:report.pdf"
        metric = pf_connection.execute(
            text("SELECT metric_name FROM llm_run_metrics")
        ).fetchone()
        assert metric[0] == "pages"

    def test_no_window_no_metadata_appends_nothing(self, pf_tables, pf_connection):
        from pf_core.llm.recording import end_call_recording
        from pf_core.llm.tracked import tracked_messages_call

        tracked_messages_call(
            client=FakeChat(),
            agent_type="probe",
            messages=MESSAGES,
            model="test-model",
        )
        assert end_call_recording() == []
