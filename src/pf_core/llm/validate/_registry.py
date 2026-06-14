"""In-process registry mapping agent-type slugs to ValidatorPipelines.

Registration lives in consumer-project code (typically ``app/validators/``)
and runs at import time — one ``register()`` call per agent type, plus an
``__init__.py`` that imports each module for its side effect.

Last registration wins; re-registering the same ``agent_type`` replaces the
previous entry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from pf_core.log import get_logger

from pf_core.llm.validate._cross_field import get_cross_field_validator
from pf_core.llm.validate._semantic import build_semantic_validator

logger = get_logger(__name__)


class Validator(Protocol):
    """Shape-stage validator protocol.

    Adapter classes (:class:`PydanticValidator`, :class:`JsonSchemaValidator`)
    implement this. Projects can supply their own by matching the signature.
    """

    def validate_shape(
        self, parsed: Any, *, agent_type: str,
    ) -> tuple[Any, "ValidationSignal"]:  # noqa: F821 — forward string
        ...


@dataclass
class ValidatorPipeline:
    """Per-agent validation pipeline resolved from a ``register()`` call."""

    agent_type: str
    shape: Any | None  # Validator | None
    semantic: list[Callable] = field(default_factory=list)
    cross_field: list[Callable] = field(default_factory=list)
    schema_version: int = 1


_REGISTRY: dict[str, ValidatorPipeline] = {}


def register(
    agent_type: str,
    *,
    shape: Any | None = None,
    semantic: list[str] | None = None,
    cross_field: list[str] | None = None,
    schema_version: int = 1,
) -> None:
    """Register a full validation pipeline for an agent type.

    Args:
        agent_type: Agent-type slug (must match the ``llm_agent_types.slug``
            used by tracking and the model router).
        shape: Either a Pydantic model class (auto-wrapped in
            :class:`PydanticValidator`) or a pre-built Validator instance
            (e.g. ``JsonSchemaValidator(schema)``). ``None`` skips shape.
        semantic: List of config strings for built-in semantic validators
            (e.g. ``["url_sanity", "tier1_ratio:0.6", "field_non_empty:headline"]``).
            Severity override syntax: append ``:error`` or ``:warn`` — applied
            at the end of the args list (e.g. ``"url_sanity:error"``).
        cross_field: List of names of registered cross-field validators
            (registered via :func:`cross_field_validator`).
        schema_version: Incrementing integer written as ``schema:<agent>_v<n>``
            into ``llm_run_tags``. Bump when schema changes materially.

    Raises:
        KeyError: if a ``semantic`` or ``cross_field`` name is not found.
    """
    shape_validator = _resolve_shape(shape) if shape is not None else None
    sem_callables = [build_semantic_validator(spec) for spec in (semantic or [])]
    cf_callables = [get_cross_field_validator(name) for name in (cross_field or [])]

    pipeline = ValidatorPipeline(
        agent_type=agent_type,
        shape=shape_validator,
        semantic=sem_callables,
        cross_field=cf_callables,
        schema_version=schema_version,
    )
    if agent_type in _REGISTRY:
        logger.info(
            "validator_pipeline_replaced",
            agent_type=agent_type,
            prev_schema_version=_REGISTRY[agent_type].schema_version,
            new_schema_version=schema_version,
        )
    _REGISTRY[agent_type] = pipeline


def _resolve_shape(shape: Any) -> Any:
    """Auto-wrap a Pydantic model class in ``PydanticValidator``."""
    try:
        from pydantic import BaseModel
    except ImportError:  # pragma: no cover — pydantic is a hard dep
        BaseModel = None  # type: ignore[assignment]

    if BaseModel is not None and isinstance(shape, type) and issubclass(shape, BaseModel):
        from pf_core.llm.validate._pydantic import PydanticValidator
        return PydanticValidator(shape)
    return shape


def get_pipeline(agent_type: str) -> ValidatorPipeline | None:
    """Lookup a pipeline by agent-type slug, or ``None`` if unregistered."""
    return _REGISTRY.get(agent_type)


def has_pipeline(agent_type: str) -> bool:
    """Return whether *agent_type* has a registered pipeline.

    Useful for pre-flight checks at app startup — pair with
    :func:`list_agent_types` to fail loudly if a known-required agent slug
    is missing before the first validation call.
    """
    return agent_type in _REGISTRY


def list_agent_types() -> list[str]:
    """All registered agent-type slugs, sorted."""
    return sorted(_REGISTRY.keys())


def clear_registry() -> None:
    """Drop all registrations. Test helper."""
    _REGISTRY.clear()
