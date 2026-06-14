"""Tests for the ``@track_run`` decorator.

Exercises:
- Success path: captures usage, fingerprint, sampling; stamps ``_llm_run_id``.
- Failure path: records ``status='failed'`` with error fields; re-raises.
- Return-shape flexibility: tuple and dict returns both work.
- Contract: ``model=`` kwarg required.
"""

from __future__ import annotations

import pytest

from pf_core.exceptions import ClientError
from pf_core.llm.tracking import (
    LlmRunRepo,
    clear_resolver_caches,
    metadata,
    track_run,
)
from pf_core.llm.tracking import schema as s


@pytest.fixture(autouse=True)
def _clear_caches_between_tests():
    clear_resolver_caches()
    yield
    clear_resolver_caches()


# ---------------------------------------------------------------------------
# provider= label default deprecation
# ---------------------------------------------------------------------------


def test_track_run_without_provider_warns_deprecation():
    """The silent provider="openrouter" label default is deprecated — the
    label should be passed explicitly (e.g. the backend from resolve_agent)
    or None to skip."""
    with pytest.warns(DeprecationWarning, match="provider"):
        track_run(agent_type="drafter")


def test_track_run_with_explicit_provider_does_not_warn(recwarn):
    track_run(agent_type="drafter", provider="anthropic")
    track_run(agent_type="drafter", provider=None)
    assert not [w for w in recwarn if issubclass(w.category, DeprecationWarning)]


@pytest.fixture()
def tracking_db(pf_engine):
    metadata.create_all(pf_engine)
    yield pf_engine
    metadata.drop_all(pf_engine)


# ---------------------------------------------------------------------------
# Fake OpenRouter client that mirrors the real return shape
# ---------------------------------------------------------------------------


class _FakeOpenRouterClient:
    """Stand-in for ``OpenRouterClient`` — returns a fixed (content, usage)."""

    def __init__(self, content: str, usage: dict):
        self._content = content
        self._usage = usage
        self.calls: list[dict] = []

    def chat(self, *, model, messages, **kwargs):
        self.calls.append({"model": model, "messages": messages, **kwargs})
        return self._content, dict(self._usage)


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


def test_decorator_records_run_on_success(tracking_db):
    fake = _FakeOpenRouterClient(
        content="draft body",
        usage={
            "prompt_tokens": 1200,
            "completion_tokens": 800,
            "cache_read_tokens": 900,
            "cache_write_tokens": 0,
            "reasoning_tokens": 100,
            "cost_usd": 0.0052,
            "duration_ms": 3100,
            "system_fingerprint": "fp_abc123",
        },
    )

    @track_run(agent_type="drafter")
    def tracked_chat(*, model, messages, **sampling):
        return fake.chat(model=model, messages=messages, **sampling)

    content, usage = tracked_chat(
        model="claude-opus-4-7",
        messages=[
            {"role": "system", "content": "you are a drafter"},
            {"role": "user", "content": "write something"},
        ],
        temperature=0.2,
        max_tokens=4096,
    )

    assert content == "draft body"
    run_id = usage["_llm_run_id"]
    assert isinstance(run_id, int) and run_id > 0

    row = LlmRunRepo().get(run_id)
    assert row["status"] == "success"
    assert row["prompt_tokens"] == 1200
    assert row["completion_tokens"] == 800
    assert row["cache_read_tokens"] == 900
    assert row["reasoning_tokens"] == 100
    assert float(row["cost_usd"]) == pytest.approx(0.0052)
    assert row["duration_ms"] == 3100
    assert row["model_fingerprint"] == "fp_abc123"
    assert row["provider"] == "openrouter"
    assert row["temperature"] == pytest.approx(0.2)
    assert row["max_tokens"] == 4096


def test_decorator_stamps_run_id_onto_usage_dict(tracking_db):
    fake = _FakeOpenRouterClient(content="x", usage={"prompt_tokens": 1})

    @track_run(agent_type="drafter")
    def tracked_chat(*, model, messages):
        return fake.chat(model=model, messages=messages)

    _, usage = tracked_chat(model="claude-opus-4-7", messages=[])
    assert "_llm_run_id" in usage


def test_decorator_stores_rendered_prompts_from_messages(tracking_db):
    fake = _FakeOpenRouterClient(content="output", usage={})

    @track_run(agent_type="drafter")
    def tracked_chat(*, model, messages):
        return fake.chat(model=model, messages=messages)

    _, usage = tracked_chat(
        model="claude-opus-4-7",
        messages=[
            {"role": "system", "content": "sys A"},
            {"role": "system", "content": "sys B"},
            {"role": "user", "content": "usr body"},
            {"role": "assistant", "content": "ignore me"},
        ],
    )

    full = LlmRunRepo().get_with_payload(usage["_llm_run_id"])
    assert full["payload"]["rendered_system"] == "sys A\nsys B"
    assert full["payload"]["rendered_user"] == "usr body"
    assert full["payload"]["raw_response"] == "output"


def test_decorator_stores_model_name_on_new_row(tracking_db):
    fake = _FakeOpenRouterClient(content="x", usage={})

    @track_run(agent_type="grader")
    def tracked_chat(*, model, messages):
        return fake.chat(model=model, messages=messages)

    _, usage = tracked_chat(model="openai/gpt-4o", messages=[])
    with tracking_db.connect() as conn:
        model_row = conn.execute(
            s.llm_models.select().where(s.llm_models.c.name == "openai/gpt-4o")
        ).mappings().one()
        run = conn.execute(
            s.llm_runs.select().where(s.llm_runs.c.id == usage["_llm_run_id"])
        ).mappings().one()
    assert run["model_id"] == model_row["id"]


def test_decorator_captures_sampling_kwargs(tracking_db):
    fake = _FakeOpenRouterClient(content="x", usage={})

    @track_run(agent_type="drafter")
    def tracked_chat(*, model, messages, **sampling):
        return fake.chat(model=model, messages=messages, **sampling)

    _, usage = tracked_chat(
        model="claude-opus-4-7",
        messages=[],
        temperature=0.7,
        top_p=0.9,
        max_tokens=2048,
        seed=42,
    )
    row = LlmRunRepo().get(usage["_llm_run_id"])
    assert row["temperature"] == pytest.approx(0.7)
    assert row["top_p"] == pytest.approx(0.9)
    assert row["max_tokens"] == 2048
    assert row["seed"] == 42


def test_decorator_accepts_dict_return_shape(tracking_db):
    """Support ``{"content": ..., "usage": {...}}`` in addition to tuple."""

    @track_run(agent_type="drafter")
    def returns_dict(*, model, messages):
        return {"content": "out", "usage": {"prompt_tokens": 99}}

    result = returns_dict(model="claude-opus-4-7", messages=[])
    assert result["content"] == "out"
    run_id = result["usage"]["_llm_run_id"]
    row = LlmRunRepo().get(run_id)
    assert row["prompt_tokens"] == 99


def test_decorator_pass_through_return_value_identity(tracking_db):
    """The decorator must not replace the return tuple — only mutate usage."""
    fake = _FakeOpenRouterClient(content="x", usage={"prompt_tokens": 1})

    @track_run(agent_type="drafter")
    def tracked_chat(*, model, messages):
        return fake.chat(model=model, messages=messages)

    result = tracked_chat(model="claude-opus-4-7", messages=[])
    assert isinstance(result, tuple)
    assert len(result) == 2


def test_decorator_records_custom_provider(tracking_db):
    fake = _FakeOpenRouterClient(content="x", usage={})

    @track_run(agent_type="drafter", provider="claude_cli")
    def tracked_chat(*, model, messages):
        return fake.chat(model=model, messages=messages)

    _, usage = tracked_chat(model="claude-opus-4-7", messages=[])
    row = LlmRunRepo().get(usage["_llm_run_id"])
    assert row["provider"] == "claude_cli"


# ---------------------------------------------------------------------------
# Failure path
# ---------------------------------------------------------------------------


def test_decorator_records_failed_run_and_reraises(tracking_db):
    @track_run(agent_type="drafter")
    def tracked_chat(*, model, messages):
        raise ClientError(
            "OpenRouter 502: Bad Gateway",
            context={"status_code": 502, "model": model},
        )

    with pytest.raises(ClientError, match="Bad Gateway"):
        tracked_chat(model="claude-opus-4-7", messages=[])

    with tracking_db.connect() as conn:
        rows = conn.execute(
            s.llm_runs.select().order_by(s.llm_runs.c.id.desc())
        ).mappings().fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "failed"
    assert row["error_class"] == "ClientError"
    assert "Bad Gateway" in row["error"]
    assert row["http_status"] == 502


def test_decorator_failure_captures_duration_when_no_usage(tracking_db):
    @track_run(agent_type="drafter")
    def tracked_chat(*, model, messages):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        tracked_chat(model="claude-opus-4-7", messages=[])

    with tracking_db.connect() as conn:
        row = conn.execute(
            s.llm_runs.select().order_by(s.llm_runs.c.id.desc())
        ).mappings().first()
    assert row["status"] == "failed"
    assert row["error_class"] == "RuntimeError"
    assert row["duration_ms"] is not None


def test_decorator_failure_stores_rendered_prompts(tracking_db):
    """Even on failure, we want the prompts for replay/forensics."""

    @track_run(agent_type="drafter")
    def tracked_chat(*, model, messages):
        raise RuntimeError("oops")

    with pytest.raises(RuntimeError):
        tracked_chat(
            model="claude-opus-4-7",
            messages=[
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "usr"},
            ],
        )

    with tracking_db.connect() as conn:
        row = conn.execute(
            s.llm_runs.select().order_by(s.llm_runs.c.id.desc())
        ).mappings().one()
        payload = conn.execute(
            s.llm_run_payloads.select().where(
                s.llm_run_payloads.c.llm_run_id == row["id"]
            )
        ).mappings().one()
    assert payload["rendered_system"] == "sys"
    assert payload["rendered_user"] == "usr"


# ---------------------------------------------------------------------------
# Contract enforcement
# ---------------------------------------------------------------------------


def test_decorator_requires_model_kwarg(tracking_db):
    @track_run(agent_type="drafter")
    def tracked_chat(*, messages):
        return "x", {}

    with pytest.raises(TypeError, match="model="):
        tracked_chat(messages=[])


def test_decorator_rejects_unknown_return_shape(tracking_db):
    @track_run(agent_type="drafter")
    def tracked_chat(*, model, messages):
        return 42  # neither tuple nor dict

    with pytest.raises(TypeError, match="expected"):
        tracked_chat(model="claude-opus-4-7", messages=[])
