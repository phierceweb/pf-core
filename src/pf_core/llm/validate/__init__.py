"""
Declarative schema validation for LLM responses.

Each agent type registers a shape validator (Pydantic model or JSON Schema)
plus optional semantic and cross-field validators. Services call
:func:`parse_and_validate` with the raw LLM response and the agent-type slug;
every signal is returned structured and optionally persisted to
``llm_run_validations``.

Public surface::

    from pf_core.llm.validate import (
        # Registration
        register,
        get_pipeline,
        list_agent_types,
        cross_field_validator,
        register_tier1_domains,

        # Pipeline
        parse_and_validate,
        ValidationResult,
        ValidationSignal,

        # Shape validators
        PydanticValidator,
        JsonSchemaValidator,  # requires pf-core[jsonschema]
    )

See ``docs/llm-schema-validation.md`` for the implementation reference.
"""

from pf_core.llm.validate._cross_field import (  # noqa: F401
    clear_cross_field_validators,
    cross_field_validator,
    get_cross_field_validator,
    list_cross_field_validators,
)
from pf_core.llm.validate._pipeline import (  # noqa: F401
    ValidationResult,
    ValidationSignal,
    parse_and_validate,
)
from pf_core.llm.validate._jsonschema import JsonSchemaValidator  # noqa: F401
from pf_core.llm.validate._pydantic import PydanticValidator  # noqa: F401
from pf_core.exceptions import PipelineNotRegisteredError  # noqa: F401
from pf_core.llm.validate._registry import (  # noqa: F401
    ValidatorPipeline,
    clear_registry,
    get_pipeline,
    has_pipeline,
    list_agent_types,
    register,
)
from pf_core.llm.validate._semantic import (  # noqa: F401
    register_tier1_domains,
    register_url_hallucination_rules,
)


__all__ = [
    "JsonSchemaValidator",
    "PipelineNotRegisteredError",
    "PydanticValidator",
    "ValidationResult",
    "ValidationSignal",
    "ValidatorPipeline",
    "clear_cross_field_validators",
    "clear_registry",
    "cross_field_validator",
    "get_cross_field_validator",
    "get_pipeline",
    "has_pipeline",
    "list_agent_types",
    "list_cross_field_validators",
    "parse_and_validate",
    "register",
    "register_tier1_domains",
    "register_url_hallucination_rules",
]
