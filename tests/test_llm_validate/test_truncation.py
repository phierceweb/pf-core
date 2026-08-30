"""parse_and_validate truncation handling: usage= derivation + warn/fail paths."""

from __future__ import annotations

import json

from pf_core.llm.tracking import LlmRunRepo
from pf_core.llm.tracking import schema as ts
from pf_core.llm.validate import parse_and_validate, register

from .conftest import PydOk

GOOD = json.dumps({"headline": "hi", "score": 1})


def test_usage_derives_truncation_and_emits_warn_signal():
    register(agent_type="tr_warn", shape=PydOk)
    res = parse_and_validate(
        GOOD,
        agent_type="tr_warn",
        usage={"finish_reason": "length"},
    )
    sig = next(s for s in res.signals if s.validator == "tr_warn_truncated")
    assert sig.severity == "warn"
    assert sig.passed is False
    assert "token limit" in sig.details["reason"]
    assert res.ok is True  # warn severity never flips ok
    assert isinstance(res.value, PydOk)


def test_warn_signal_is_persisted_with_run_id(tracking_db):
    register(agent_type="tr_db", shape=PydOk)
    run_id = LlmRunRepo().record(agent_type="tr_db", model="claude-opus-4-7")
    res = parse_and_validate(
        GOOD,
        agent_type="tr_db",
        run_id=run_id,
        usage={"finish_reason": "max_tokens"},
    )
    assert res.ok is True

    with tracking_db.connect() as conn:
        rows = (
            conn.execute(
                ts.llm_run_validations.select().where(ts.llm_run_validations.c.llm_run_id == run_id)
            )
            .mappings()
            .fetchall()
        )
    by_validator = {r["validator"]: r for r in rows}
    assert "tr_db_truncated" in by_validator
    assert by_validator["tr_db_truncated"]["passed"] is False
    assert by_validator["tr_db_truncated"]["severity"] == "warn"


def test_complete_usage_emits_no_truncation_signal():
    register(agent_type="tr_ok", shape=PydOk)
    res = parse_and_validate(GOOD, agent_type="tr_ok", usage={"finish_reason": "stop"})
    assert res.ok is True
    assert not any(s.validator == "tr_ok_truncated" for s in res.signals)


def test_unknown_usage_emits_no_truncation_signal():
    register(agent_type="tr_unk", shape=PydOk)
    res = parse_and_validate(GOOD, agent_type="tr_unk", usage={})
    assert not any(s.validator == "tr_unk_truncated" for s in res.signals)


def test_explicit_truncated_false_beats_truncating_usage():
    register(agent_type="tr_override", shape=PydOk)
    res = parse_and_validate(
        GOOD,
        agent_type="tr_override",
        truncated=False,
        usage={"finish_reason": "length"},
    )
    assert res.ok is True
    assert not any(s.validator == "tr_override_truncated" for s in res.signals)


def test_explicit_truncated_true_needs_no_usage():
    register(agent_type="tr_explicit", shape=PydOk)
    res = parse_and_validate(GOOD, agent_type="tr_explicit", truncated=True)
    assert any(s.validator == "tr_explicit_truncated" and s.severity == "warn" for s in res.signals)


def test_on_truncation_fail_still_short_circuits_from_usage():
    register(agent_type="tr_fail", shape=PydOk)
    res = parse_and_validate(
        GOOD,
        agent_type="tr_fail",
        usage={"finish_reason": "length"},
        on_truncation="fail",
    )
    assert res.ok is False
    assert res.value is None
    assert [s.validator for s in res.signals] == ["tr_fail_truncated"]
    assert res.signals[0].severity == "error"
