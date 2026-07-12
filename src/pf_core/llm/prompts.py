"""
Prompt template loader and renderer.

Loads prompt templates from YAML files and renders them with variable
substitution. Two placeholder styles are supported:

- **Brace style** (default): ``{variable}`` — uses Python's ``str.format_map``.
  Literal curly braces must be escaped as ``{{`` and ``}}``. Best for simple
  prompts without JSON or code examples.

- **Token style**: ``@@VARIABLE@@`` — uses plain string replacement.
  No escaping needed for curly braces. Best for prompts that contain JSON
  examples, code blocks, or other text heavy with ``{`` and ``}``.

Usage::

    from pf_core.llm.prompts import load_prompts, render

    # Brace style (default)
    prompts = load_prompts("config/prompts.yaml")
    system = render(prompts["summarize"]["system"], max_words=200)

    # Token style
    text = render("You are @@ROLE@@.", style="@@", ROLE="a summarizer")
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from pf_core.exceptions import ConfigurationError, InvalidInputError
from pf_core.utils.config_path import resolve_config_path


def load_prompts(path: str | Path) -> dict[str, Any]:
    """Load prompt templates from a YAML file.

    Args:
        path: Path to the YAML file (absolute or relative to cwd).

    Returns:
        Parsed YAML dict (typically keyed by prompt name, with
        ``system`` and ``user`` sub-keys).

    Raises:
        ConfigurationError: If the file cannot be read or parsed.
    """
    p = Path(path)
    if not p.exists():
        raise ConfigurationError(f"Prompt file not found: {p}")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ConfigurationError(f"Prompt file must be a YAML mapping: {p}")
        return data
    except yaml.YAMLError as e:
        raise ConfigurationError(f"Failed to parse prompt file {p}: {e}") from e


# Matches {name} but not {{ or }}
_BRACE_PLACEHOLDER = re.compile(r"\{(\w+)\}")
# Matches @@NAME@@
_TOKEN_PLACEHOLDER = re.compile(r"@@(\w+)@@")


def render(template: str, *, style: str = "brace", **variables: Any) -> str:
    """Render a prompt template with variable substitution.

    Args:
        template: Template string with placeholders.
        style: Placeholder style — ``"brace"`` for ``{variable}`` syntax
            (default), or ``"@@"`` for ``@@VARIABLE@@`` syntax.
        **variables: Values to substitute. All placeholders in the
            template must have a corresponding variable.

    Returns:
        Rendered prompt string.

    Raises:
        InvalidInputError: If the template references a variable not
            provided in ``variables``, or if ``style`` is invalid.

    Examples::

        # Brace style (default)
        render("Hello {name}", name="world")  # "Hello world"
        render("JSON: {{\"key\": \"{val}\"}}", val="x")  # 'JSON: {"key": "x"}'

        # Token style
        render("Hello @@NAME@@", style="@@", NAME="world")  # "Hello world"
        render('{"role": "@@ROLE@@"}', style="@@", ROLE="summarizer")
        # '{"role": "summarizer"}'  — no escaping needed
    """
    if style == "brace":
        return _render_brace(template, variables)
    if style == "@@":
        return _render_token(template, variables)
    raise InvalidInputError(f"Unknown render style: {style!r} (use 'brace' or '@@')")


def _render_brace(template: str, variables: dict[str, Any]) -> str:
    """Render using {variable} syntax via str.format_map."""
    required = set(_BRACE_PLACEHOLDER.findall(template))
    missing = required - set(variables.keys())
    if missing:
        raise InvalidInputError(
            f"Prompt template references undefined variables: {', '.join(sorted(missing))}"
        )
    try:
        return template.format_map(variables)
    except (KeyError, ValueError) as e:
        raise InvalidInputError(f"Failed to render prompt template: {e}") from e


def _render_token(template: str, variables: dict[str, Any]) -> str:
    """Render using @@VARIABLE@@ syntax via string replacement."""
    required = set(_TOKEN_PLACEHOLDER.findall(template))
    missing = required - set(variables.keys())
    if missing:
        raise InvalidInputError(
            f"Prompt template references undefined variables: {', '.join(sorted(missing))}"
        )
    result = template
    for key, value in variables.items():
        result = result.replace(f"@@{key}@@", str(value))
    return result


# ---------------------------------------------------------------------------
# Per-agent spec loader (single-agent YAML spec files)
# ---------------------------------------------------------------------------
#
# Two YAML shapes are supported across consumer apps:
#
# 1. Flat multi-agent files: one YAML file listing every agent. Load with
#    :func:`load_prompts`; caller looks up by key.
#
# 2. Per-agent spec files: one YAML file per agent, with a required schema
#    that carries version and provenance metadata. Load with
#    :func:`load_prompt_spec`. Recommended for apps with many agents
#    or long prompts.
#
# Use :func:`render_spec` to render a loaded spec + return its version as a
# tuple — pairs cleanly with ``resolve_prompt_id`` for DB registration.


_SPEC_REQUIRED = ("agent", "version", "system")


def load_prompt_spec(
    path: str | Path,
    *,
    expected_agent: str | None = None,
) -> dict[str, Any]:
    """Load and validate a per-agent YAML prompt spec file.

    Expected schema::

        agent: <slug>                # required; must match expected_agent if given
        version: <int>               # required; ≥ 1
        system: <str>                # required; non-empty
        user: <str>                  # optional
        changelog: [str, ...]        # optional
        placeholders: [str, ...]     # optional

    Args:
        path: path to the YAML file.
        expected_agent: if supplied, the file's ``agent`` field must
            match exactly — lets callers detect filename/content drift.

    Returns:
        The parsed spec dict (validated).

    Raises:
        ConfigurationError: file not found, YAML malformed, or schema
            violation (missing required key, wrong type, agent mismatch).
    """
    spec = load_prompts(path)  # raises ConfigurationError on load failure
    p = Path(path)
    missing = [k for k in _SPEC_REQUIRED if k not in spec]
    if missing:
        raise ConfigurationError(
            f"{p}: prompt spec missing required keys {missing}"
        )
    if expected_agent is not None and spec["agent"] != expected_agent:
        raise ConfigurationError(
            f"{p}: agent field {spec['agent']!r} does not match "
            f"expected {expected_agent!r}"
        )
    if not isinstance(spec["version"], int) or spec["version"] < 1:
        raise ConfigurationError(
            f"{p}: version must be a positive integer, got {spec['version']!r}"
        )
    if not isinstance(spec["system"], str) or not spec["system"].strip():
        raise ConfigurationError(f"{p}: system must be a non-empty string")
    return spec


def render_spec(
    spec: dict[str, Any],
    *,
    part: str = "system",
    style: str = "brace",
    **variables: Any,
) -> tuple[str, int]:
    """Render one part of a prompt spec dict and return ``(text, version)``.

    Thin wrapper over :func:`render` that pulls the template from a spec
    loaded by :func:`load_prompt_spec` and returns it with the spec's
    version number — the tuple shape pairs cleanly with callers that
    pass both into a DB logger (e.g., ``db.log_agent_run(
    prompt_version=version, system_prompt=text, ...)``).

    Args:
        spec: spec dict with at least ``version`` and ``<part>`` keys.
        part: which section of the spec to render.
        style: ``"brace"`` or ``"@@"`` — passed through to :func:`render`.
        **variables: substitution context.

    Returns:
        ``(rendered_text, version)``.
    """
    if part not in spec:
        raise InvalidInputError(
            f"spec has no {part!r} section; available: {sorted(spec.keys())}"
        )
    template = spec[part]
    if not isinstance(template, str):
        raise InvalidInputError(
            f"spec {part!r} must be a string, got {type(template).__name__}"
        )
    rendered = render(template, style=style, **variables) if variables else template
    version = int(spec.get("version", 1))
    return rendered, version


# ---------------------------------------------------------------------------
# Slug-based loading (one YAML per agent under a prompts directory)
# ---------------------------------------------------------------------------

_PROMPT_CACHE: dict[tuple[str, str], dict[str, Any]] = {}


def load_prompt(
    slug: str,
    *,
    dir: str | Path | None = None,
    env_dir_var: str | None = None,
    bundled_dir: str | Path | None = None,
    cwd_subdir: str = "config/prompts",
    cache: bool = True,
) -> dict[str, Any]:
    """Load + validate the per-agent spec for *slug* (``<slug>.yaml``).

    The slug convenience over :func:`load_prompt_spec`: maps ``slug`` to
    ``<slug>.yaml``, enforces ``expected_agent=slug`` (filename/content
    drift fails loud), and caches the parsed spec per process —
    :func:`clear_prompt_cache` resets; ``cache=False`` re-reads every call.

    Resolution: ``dir`` names the directory holding the file directly.
    Otherwise the override chain applies — ``$env_dir_var`` directory →
    CWD ``cwd_subdir`` → ``bundled_dir`` when given (via
    :func:`pf_core.utils.config_path.resolve_config_path`).

    Raises:
        ConfigurationError: file missing/malformed, or agent mismatch.
        InvalidInputError: ``dir`` and ``bundled_dir`` both given.
    """
    if dir is not None and bundled_dir is not None:
        raise InvalidInputError("dir and bundled_dir are mutually exclusive")

    filename = f"{slug}.yaml"
    if dir is not None:
        path = Path(dir) / filename
    elif bundled_dir is not None:
        path = resolve_config_path(
            filename,
            env_dir_var=env_dir_var,
            bundled_dir=Path(bundled_dir),
            cwd_subdir=cwd_subdir,
        )
    else:
        path = Path(cwd_subdir) / filename
        if env_dir_var:
            env_dir = os.environ.get(env_dir_var)
            if env_dir and (Path(env_dir) / filename).exists():
                path = Path(env_dir) / filename
    path = Path(path).resolve()

    key = (slug, str(path))
    if cache and key in _PROMPT_CACHE:
        return _PROMPT_CACHE[key]
    spec = load_prompt_spec(path, expected_agent=slug)
    if cache:
        _PROMPT_CACHE[key] = spec
    return spec


def clear_prompt_cache() -> None:
    """Reset the in-process :func:`load_prompt` cache."""
    _PROMPT_CACHE.clear()
