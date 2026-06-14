"""Pydantic shape-validator adapter.

Wraps a Pydantic ``BaseModel`` subclass as a shape validator. On success,
returns the validated model instance (attribute access + type coercion
preserved); on failure, returns ``(None, error_signal)`` with the Pydantic
``errors()`` list in ``details``.
"""

from __future__ import annotations

from typing import Any

try:
    from pydantic import BaseModel, ValidationError
except ImportError as e:  # pragma: no cover - exercised by bare-install CI
    from pf_core._extras import extra_import_error

    raise extra_import_error("validate", "pydantic", feature="pf_core.llm.validate") from e

from pf_core.llm.validate._pipeline import ValidationSignal


class PydanticValidator:
    """Validates parsed JSON against a Pydantic model."""

    def __init__(self, model: type[BaseModel]) -> None:
        self.model = model

    def validate_shape(
        self, parsed: Any, *, agent_type: str,
    ) -> tuple[Any | None, ValidationSignal]:
        """Run ``model.model_validate(parsed)``.

        Returns:
            ``(instance, passed_signal)`` on success, ``(None, failed_signal)``
            otherwise. The failure signal's ``details`` contains the full
            Pydantic ``errors()`` list (location + type + message per error).
        """
        name = f"{agent_type}_shape"
        try:
            instance = self.model.model_validate(parsed)
        except ValidationError as e:
            return None, ValidationSignal(
                validator=name,
                severity="error",
                passed=False,
                details={"errors": e.errors(include_url=False)},
            )
        return instance, ValidationSignal(
            validator=name, severity="error", passed=True, details=None,
        )
