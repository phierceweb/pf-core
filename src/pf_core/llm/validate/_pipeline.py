"""Validation pipeline — ValidationResult, ValidationSignal, parse_and_validate.

The pipeline runs in three stages:

    parse (JSON)  →  shape  →  semantic  →  cross-field

Each validator produces a :class:`ValidationSignal`. When called with a
``run_id``, signals are persisted to ``llm_run_validations`` and the registry's
``schema_version`` is tagged onto ``llm_run_tags``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pf_core.exceptions import PipelineNotRegisteredError
from pf_core.llm.parse import parse_llm_json
from pf_core.log import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class ValidationSignal:
    """One row's worth of validator output.

    Matches the shape of an ``llm_run_validations`` row. ``severity`` is one
    of ``"info"``, ``"warn"``, ``"error"`` by convention but stored as free
    text to allow project-specific extensions.
    """

    validator: str
    severity: str
    passed: bool
    details: dict | None = None


@dataclass
class ValidationResult:
    """Outcome of running the full pipeline against one raw LLM response.

    ``ok`` is ``True`` iff no ``error``-severity signal fired. ``value`` is
    the parsed-and-coerced object on shape-pass (a Pydantic instance when
    the shape validator is a :class:`PydanticValidator`, otherwise a dict);
    ``None`` on shape failure.
    """

    ok: bool
    value: Any | None
    signals: list[ValidationSignal] = field(default_factory=list)
    schema_version: int = 1

    @property
    def failures(self) -> list[ValidationSignal]:
        """Signals with ``passed=False`` and ``severity='error'``."""
        return [s for s in self.signals if not s.passed and s.severity == "error"]

    @property
    def warnings(self) -> list[ValidationSignal]:
        """Signals with ``passed=False`` and severity in ``{info, warn}``."""
        return [
            s for s in self.signals
            if not s.passed and s.severity in ("info", "warn")
        ]


def _coerce_signals(out: Any) -> list[ValidationSignal]:
    """Normalize a validator return value into a list of signals."""
    if out is None:
        return []
    if isinstance(out, ValidationSignal):
        return [out]
    if isinstance(out, list):
        return [s for s in out if isinstance(s, ValidationSignal)]
    return []


def _write_signals_to_db(
    *, run_id: int, schema_version: int, agent_type: str,
    signals: list[ValidationSignal],
) -> None:
    """Persist signals to ``llm_run_validations`` and tag the schema version."""
    from pf_core.llm.tracking.subrepos import LlmRunValidationRepo
    from pf_core.llm.tracking import schema as s
    from pf_core.db import transaction

    repo = LlmRunValidationRepo()
    for sig in signals:
        try:
            repo.record(
                run_id,
                validator=sig.validator,
                passed=sig.passed,
                severity=sig.severity,
                details=sig.details,
            )
        except Exception:  # noqa: BLE001 — logging must not break the caller
            logger.exception(
                "validation_record_failed",
                run_id=run_id, validator=sig.validator,
            )

    tag = f"schema:{agent_type}_v{schema_version}"
    try:
        with transaction() as conn:
            conn.execute(
                s.llm_run_tags.delete().where(
                    (s.llm_run_tags.c.llm_run_id == run_id)
                    & (s.llm_run_tags.c.tag == tag)
                )
            )
            conn.execute(
                s.llm_run_tags.insert().values(llm_run_id=run_id, tag=tag)
            )
    except Exception:  # noqa: BLE001
        logger.exception("validation_tag_write_failed", run_id=run_id, tag=tag)


def parse_and_validate(
    raw_response: str,
    *,
    agent_type: str,
    run_id: int | None = None,
    validation_context: dict | None = None,
    stages: tuple[str, ...] = ("shape", "semantic", "cross_field"),
    expect: str = "any",
    missing_pipeline: Literal["raise", "fallback"] = "raise",
) -> ValidationResult:
    """Parse a raw LLM response and run the registered validation pipeline.

    Args:
        raw_response: The raw text returned by the LLM (may contain markdown
            fences or trailing prose; :func:`parse_llm_json` handles cleanup).
        agent_type: The agent-type slug used to look up the registered
            pipeline.
        run_id: If set, every signal is written to ``llm_run_validations``
            and the ``schema:<agent>_v<n>`` tag is added to ``llm_run_tags``.
            If ``None``, the pipeline runs in-memory only (useful for tests
            and offline replay).
        validation_context: Passed to cross-field validators via their
            ``context=`` kwarg. Services typically pass domain objects the
            validator needs (e.g. ``{"essay_config": ec}``).
        stages: Which pipeline stages to run. Default runs all three.
            Useful during migration to skip semantic/cross-field with
            ``stages=("shape",)``.
        expect: Forwarded to :func:`parse_llm_json` — ``"any"``, ``"array"``,
            or ``"object"``.
        missing_pipeline: Behavior when no pipeline is registered for
            ``agent_type``. ``"raise"`` (default) raises
            :class:`pf_core.exceptions.PipelineNotRegisteredError` naming
            the missing slug and currently-registered agents.
            ``"fallback"`` preserves pre-0.13 behavior: emit a WARNING log
            and return ``ValidationResult(ok=False,
            signals=[no_pipeline_registered])``.

    Returns:
        A :class:`ValidationResult` describing parse + validation outcome.

    Raises:
        PipelineNotRegisteredError: if ``missing_pipeline="raise"`` and
            ``agent_type`` has no registered pipeline.
    """
    from pf_core.llm.validate._registry import get_pipeline, list_agent_types

    pipeline = get_pipeline(agent_type)
    if pipeline is None:
        if missing_pipeline == "raise":
            raise PipelineNotRegisteredError(
                agent_type=agent_type,
                known_agents=list_agent_types(),
            )
        logger.warning(
            "no_pipeline_registered",
            agent_type=agent_type,
            known_agents=list_agent_types(),
        )
        sig = ValidationSignal(
            validator="no_pipeline_registered", severity="error", passed=False,
            details={
                "agent_type": agent_type,
                "known_agents": list_agent_types(),
            },
        )
        return ValidationResult(ok=False, value=None, signals=[sig])

    # --- Parse ---
    parsed = parse_llm_json(raw_response, expect=expect, strict=False)
    signals: list[ValidationSignal] = []

    if parsed is None:
        sig = ValidationSignal(
            validator=f"{agent_type}_parse",
            severity="error",
            passed=False,
            details={"reason": "could not extract JSON from response"},
        )
        signals.append(sig)
        result = ValidationResult(
            ok=False, value=None, signals=signals,
            schema_version=pipeline.schema_version,
        )
        if run_id is not None:
            _write_signals_to_db(
                run_id=run_id, schema_version=pipeline.schema_version,
                agent_type=agent_type, signals=signals,
            )
        return result

    value: Any | None = parsed

    # --- Shape ---
    if "shape" in stages and pipeline.shape is not None:
        coerced, shape_signal = pipeline.shape.validate_shape(
            parsed, agent_type=agent_type,
        )
        signals.append(shape_signal)
        if shape_signal.passed:
            value = coerced
        else:
            value = None

    # --- Semantic ---
    if "semantic" in stages and value is not None:
        for sem in pipeline.semantic:
            try:
                out = sem(value, context=validation_context or {})
            except Exception as e:  # noqa: BLE001
                out = ValidationSignal(
                    validator=getattr(sem, "name", "semantic_unknown"),
                    severity="error", passed=False,
                    details={"exception": repr(e)},
                )
            signals.extend(_coerce_signals(out))

    # --- Cross-field ---
    if "cross_field" in stages and value is not None:
        for cf in pipeline.cross_field:
            try:
                out = cf(value, context=validation_context or {})
            except Exception as e:  # noqa: BLE001
                out = ValidationSignal(
                    validator=getattr(cf, "name", "cross_field_unknown"),
                    severity="error", passed=False,
                    details={"exception": repr(e)},
                )
            signals.extend(_coerce_signals(out))

    ok = not any(not s.passed and s.severity == "error" for s in signals)
    result = ValidationResult(
        ok=ok,
        value=value if ok else (value if value is not None else None),
        signals=signals,
        schema_version=pipeline.schema_version,
    )

    if run_id is not None:
        _write_signals_to_db(
            run_id=run_id, schema_version=pipeline.schema_version,
            agent_type=agent_type, signals=signals,
        )

    return result
