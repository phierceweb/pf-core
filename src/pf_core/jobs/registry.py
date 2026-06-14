"""
Job kind registry.

Projects declare each job type in code via ``register_kind``. The registry
validates inputs on create, enforces state transitions, and drives admin UIs.

Usage::

    from pf_core.jobs import register_kind

    register_kind(
        kind="export_pass",
        description="Process N records with the current config",
        states=["pending", "running", "succeeded", "failed", "partial", "canceled"],
        transitions={
            "pending":   ["running", "canceled"],
            "running":   ["succeeded", "failed", "partial", "canceled"],
            "failed":    ["pending"],       # manual retry
            "partial":   ["running"],       # resume
        },
        inputs_schema=ExportPassInputs,    # Pydantic model (optional)
        outputs_schema=ExportPassOutputs,
        default_priority=60,
    )

Registrations run at import time — a common pattern is a project module
``app/jobs/_register.py`` imported by ``app/jobs/__init__.py``.

See ``docs/jobs.md`` for the implementation reference.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pf_core.exceptions import ConfigurationError, InvalidInputError


# ---------------------------------------------------------------------------
# Default state set
# ---------------------------------------------------------------------------

#: The canonical status set every job kind starts with unless overridden.
DEFAULT_STATES: tuple[str, ...] = (
    "pending",
    "running",
    "succeeded",
    "failed",
    "canceled",
    "partial",
)

#: Default transitions if a kind doesn't declare its own.
DEFAULT_TRANSITIONS: dict[str, list[str]] = {
    "pending": ["running", "canceled"],
    "running": ["succeeded", "failed", "partial", "canceled"],
    "partial": ["running", "failed", "canceled"],
    "failed": ["pending"],
    "canceled": [],
    "succeeded": [],
}

#: Terminal states — jobs in these states do not transition further except
#: via explicit retry.
TERMINAL_STATES: frozenset[str] = frozenset({"succeeded", "failed", "canceled"})


@dataclass(frozen=True)
class JobKind:
    """A registered job kind descriptor."""

    kind: str
    states: tuple[str, ...]
    transitions: dict[str, tuple[str, ...]]
    description: str | None = None
    inputs_schema: Any | None = None
    outputs_schema: Any | None = None
    default_priority: int = 50
    terminal_states: frozenset[str] = field(default_factory=lambda: TERMINAL_STATES)
    auto_track_progress: bool = False

    def can_transition(self, from_status: str, to_status: str) -> bool:
        """Return True if ``from_status`` → ``to_status`` is allowed."""
        return to_status in self.transitions.get(from_status, ())

    def validate_inputs(self, inputs: Any) -> Any:
        """Validate ``inputs`` against the registered Pydantic model, if any.

        Returns the parsed model (or raw inputs if no schema). Raises
        ``InvalidInputError`` on validation failure.
        """
        return _validate_against_schema(inputs, self.inputs_schema, label="inputs")

    def validate_outputs(self, outputs: Any) -> Any:
        """Validate ``outputs`` against the registered Pydantic model, if any."""
        return _validate_against_schema(outputs, self.outputs_schema, label="outputs")


# ---------------------------------------------------------------------------
# Registry state
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, JobKind] = {}


def register_kind(
    *,
    kind: str,
    states: list[str] | tuple[str, ...] | None = None,
    transitions: dict[str, list[str]] | None = None,
    description: str | None = None,
    inputs_schema: Any | None = None,
    outputs_schema: Any | None = None,
    default_priority: int = 50,
    auto_track_progress: bool = False,
) -> JobKind:
    """Register a job kind. Idempotent — re-registration with the same
    signature is a no-op; re-registration with a different signature raises
    ``ConfigurationError`` so import-order bugs are caught at startup.

    Args:
        kind: Discriminator string written to ``jobs.kind``.
        states: Allowed values for ``jobs.status`` for this kind. Defaults to
            ``DEFAULT_STATES`` if omitted.
        transitions: Mapping ``{from_state: [allowed_next_states]}``. Defaults
            to ``DEFAULT_TRANSITIONS`` if omitted.
        description: Free-form admin-UI text.
        inputs_schema: Optional Pydantic model class. If provided, ``JobRepo.create``
            validates the ``inputs`` dict against it.
        outputs_schema: Optional Pydantic model class for the ``outputs`` dict.
        default_priority: Default priority for new jobs of this kind (0-100).
        auto_track_progress: When True, ``JobRepo.finish_step`` atomically
            increments the parent job's ``progress_current`` by 1 every time
            a step transitions to ``succeeded`` or ``failed`` (skipped steps
            do not count — they represent resumed work that was already
            tallied). Default False for backward compatibility. Callers can
            still override the counter explicitly via ``set_progress``.
    """
    if not kind or not isinstance(kind, str):
        raise ConfigurationError(
            f"register_kind: `kind` must be a non-empty string, got {kind!r}",
        )

    resolved_states: tuple[str, ...] = tuple(states) if states else DEFAULT_STATES
    resolved_transitions_dict = (
        dict(transitions) if transitions is not None else dict(DEFAULT_TRANSITIONS)
    )
    # Filter transitions to known states and normalize values to tuples.
    resolved_transitions: dict[str, tuple[str, ...]] = {}
    for from_state, to_states in resolved_transitions_dict.items():
        if from_state not in resolved_states:
            raise ConfigurationError(
                f"register_kind({kind!r}): transition source {from_state!r} "
                f"is not in states {resolved_states!r}",
            )
        bad = [s for s in to_states if s not in resolved_states]
        if bad:
            raise ConfigurationError(
                f"register_kind({kind!r}): transitions from {from_state!r} reference "
                f"unknown states {bad!r} (known states: {resolved_states!r})",
            )
        resolved_transitions[from_state] = tuple(to_states)

    if not (0 <= default_priority <= 100):
        raise ConfigurationError(
            f"register_kind({kind!r}): default_priority must be 0-100, "
            f"got {default_priority}",
        )

    descriptor = JobKind(
        kind=kind,
        states=resolved_states,
        transitions=resolved_transitions,
        description=description,
        inputs_schema=inputs_schema,
        outputs_schema=outputs_schema,
        default_priority=default_priority,
        auto_track_progress=auto_track_progress,
    )

    existing = _REGISTRY.get(kind)
    if existing is not None and existing != descriptor:
        raise ConfigurationError(
            f"register_kind({kind!r}): already registered with a different "
            "signature. This usually means two modules register the same kind.",
        )
    _REGISTRY[kind] = descriptor
    return descriptor


def get_kind(kind: str) -> JobKind:
    """Return the registered ``JobKind`` for ``kind``.

    Raises ``ConfigurationError`` if ``kind`` was never registered.
    """
    try:
        return _REGISTRY[kind]
    except KeyError:
        known = sorted(_REGISTRY)
        raise ConfigurationError(
            f"Job kind {kind!r} is not registered. Known kinds: {known!r}. "
            "Call register_kind() at import time in your app's jobs package.",
        ) from None


def all_kinds() -> list[JobKind]:
    """Return all registered kinds, sorted by name."""
    return [_REGISTRY[k] for k in sorted(_REGISTRY)]


def clear_registry() -> None:
    """Remove all registered kinds. Intended for tests only."""
    _REGISTRY.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_against_schema(value: Any, schema: Any, *, label: str) -> Any:
    """Validate ``value`` against a Pydantic model if ``schema`` is non-None.

    Accepts either:
      - a Pydantic BaseModel subclass — calls ``model_validate``
      - ``None`` — returns ``value`` unchanged

    Raises ``InvalidInputError`` on failure, preserving the underlying error
    as ``__cause__``.
    """
    if schema is None:
        return value
    # Lazy-import Pydantic so the registry module doesn't require it.
    try:
        from pydantic import BaseModel, ValidationError
    except ImportError as e:  # pragma: no cover - pydantic is a pf-core dep
        raise ConfigurationError(
            "Pydantic is required for job input/output validation but is "
            "not installed",
        ) from e

    if isinstance(schema, type) and issubclass(schema, BaseModel):
        try:
            return schema.model_validate(value)
        except ValidationError as e:
            raise InvalidInputError(
                f"Job {label} failed schema validation: {e}",
            ) from e

    raise ConfigurationError(
        f"Unsupported {label}_schema type: {type(schema).__name__}. "
        "Only Pydantic BaseModel subclasses are supported.",
    )
