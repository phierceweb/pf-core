"""LLM utilities — response parsing, prompt loading, agent config, validation.

Re-exports are **lazy** (:pep:`562` ``__getattr__``): importing ``pf_core.llm``
does not eagerly pull every submodule. This matters because the members have
different dependency tiers — ``parse``/``validate`` need ``[validate]``
(json-repair/pydantic), the clients need ``[http]``, and ``tracked`` records to
the DB so it needs ``[tracking]``. The stdlib/pyyaml members (``url_check``,
``prompts``, ``router``, ``safe_apply``) need no extra at all.
Eager re-export would have forced the whole stack onto every importer (and made
``import pf_core.llm.parse`` fail without ``[db]`` via ``tracked`` → tracking).
Each name is imported on first access, raising that member's own friendly
``ImportError`` if its extra is missing.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

# Exported name -> (submodule, attribute). Attribute differs from the export
# name where the old eager block used an ``as`` alias.
_LAZY: dict[str, tuple[str, str]] = {
    # parse — [validate] (json-repair)
    "parse_llm_json": ("pf_core.llm.parse", "parse_llm_json"),
    # prompts — base (pyyaml)
    "load_prompt_spec": ("pf_core.llm.prompts", "load_prompt_spec"),
    "load_prompts": ("pf_core.llm.prompts", "load_prompts"),
    "render": ("pf_core.llm.prompts", "render"),
    "render_spec": ("pf_core.llm.prompts", "render_spec"),
    # router — base (pyyaml)
    "assert_agents_registered": ("pf_core.llm.router", "assert_agents_registered"),
    "clear_router_cache": ("pf_core.llm.router", "clear_cache"),
    "get_agent_config": ("pf_core.llm.router", "get_agent_config"),
    "list_agents": ("pf_core.llm.router", "list_agents"),
    # tracked — [tracking] (records to the DB)
    "ChatClient": ("pf_core.llm.tracked", "ChatClient"),
    "LlmJsonError": ("pf_core.llm.tracked", "LlmJsonError"),
    "tracked_call": ("pf_core.llm.tracked", "tracked_call"),
    # url_check — base (stdlib)
    "UrlHallucinationRule": ("pf_core.llm.url_check", "UrlHallucinationRule"),
    "url_looks_hallucinated": ("pf_core.llm.url_check", "url_looks_hallucinated"),
    "validate_urls": ("pf_core.llm.url_check", "validate_urls"),
    # validate — [validate] (pydantic)
    "JsonSchemaValidator": ("pf_core.llm.validate", "JsonSchemaValidator"),
    "PydanticValidator": ("pf_core.llm.validate", "PydanticValidator"),
    "ValidationResult": ("pf_core.llm.validate", "ValidationResult"),
    "ValidationSignal": ("pf_core.llm.validate", "ValidationSignal"),
    "cross_field_validator": ("pf_core.llm.validate", "cross_field_validator"),
    "parse_and_validate": ("pf_core.llm.validate", "parse_and_validate"),
    "register_validator": ("pf_core.llm.validate", "register"),
    "register_tier1_domains": ("pf_core.llm.validate", "register_tier1_domains"),
    "register_url_hallucination_rules": (
        "pf_core.llm.validate",
        "register_url_hallucination_rules",
    ),
}


def __getattr__(name: str) -> object:
    """PEP 562 hook: import the owning submodule on first attribute access."""
    entry = _LAZY.get(name)
    if entry is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_path, attr = entry
    module = importlib.import_module(module_path)  # may raise a friendly ImportError
    return getattr(module, attr)


def __dir__() -> list[str]:
    return sorted([*globals(), *_LAZY])


if TYPE_CHECKING:  # keep static analysers / IDEs aware of the lazy re-exports
    from pf_core.llm.parse import parse_llm_json  # noqa: F401
    from pf_core.llm.prompts import (  # noqa: F401
        load_prompt_spec,
        load_prompts,
        render,
        render_spec,
    )
    from pf_core.llm.router import (  # noqa: F401
        assert_agents_registered,
        clear_cache as clear_router_cache,
        get_agent_config,
        list_agents,
    )
    from pf_core.llm.tracked import (  # noqa: F401
        ChatClient,
        LlmJsonError,
        tracked_call,
    )
    from pf_core.llm.url_check import (  # noqa: F401
        UrlHallucinationRule,
        url_looks_hallucinated,
        validate_urls,
    )
    from pf_core.llm.validate import (  # noqa: F401
        JsonSchemaValidator,
        PydanticValidator,
        ValidationResult,
        ValidationSignal,
        cross_field_validator,
        parse_and_validate,
        register as register_validator,
        register_tier1_domains,
        register_url_hallucination_rules,
    )
