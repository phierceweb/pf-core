"""JSON-Schema shape-validator adapter.

Optional: requires the ``jsonschema`` extra. Install with
``pip install pf-core[jsonschema]``. Import fails loud at call time with a
``ConfigurationError`` so the operator gets a clear remediation message.
"""

from __future__ import annotations

from typing import Any

from pf_core.exceptions import ConfigurationError
from pf_core.llm.validate._pipeline import ValidationSignal


class JsonSchemaValidator:
    """Validates parsed JSON against a JSON Schema dict.

    Requires the optional ``jsonschema`` dependency (``pip install
    pf-core[jsonschema]``). The schema is validated once at construction
    via ``Draft202012Validator.check_schema``; bad schemas fail fast rather
    than at first use.
    """

    def __init__(self, schema: dict) -> None:
        try:
            from jsonschema import Draft202012Validator
        except ImportError as e:
            raise ConfigurationError(
                "JsonSchemaValidator requires the 'jsonschema' extra: "
                "pip install pf-core[jsonschema]"
            ) from e
        Draft202012Validator.check_schema(schema)
        self.schema = schema
        self._validator = Draft202012Validator(schema)

    def validate_shape(
        self, parsed: Any, *, agent_type: str,
    ) -> tuple[Any | None, ValidationSignal]:
        """Run the JSON-Schema validator, collecting every error path."""
        name = f"{agent_type}_shape"
        errors = sorted(self._validator.iter_errors(parsed), key=lambda e: e.path)
        if not errors:
            return parsed, ValidationSignal(
                validator=name, severity="error", passed=True, details=None,
            )
        return None, ValidationSignal(
            validator=name,
            severity="error",
            passed=False,
            details={
                "errors": [
                    {
                        "path": list(err.absolute_path),
                        "message": err.message,
                        "validator": err.validator,
                    }
                    for err in errors
                ],
            },
        )
