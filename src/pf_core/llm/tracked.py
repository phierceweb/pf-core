"""One tracked LLM call — render spec → invoke → record → JSON-retry.

This is the orchestration layer consumers kept re-implementing by hand.
pf-core already ships every primitive used here:

- :func:`pf_core.llm.prompts.render_spec` — render a loaded prompt spec.
- :func:`pf_core.llm.tracking.resolve_agent_type_id` /
  :func:`~pf_core.llm.tracking.resolve_prompt_id` — reference-table FKs.
- :class:`pf_core.llm.tracking.LlmRunRepo` — the atomic ``llm_runs`` write.
- :func:`pf_core.llm.parse.parse_llm_json` — tolerant JSON extraction.

What was missing — and what this module adds — is the *composition*:
render the spec, resolve a ``system_prompt_id`` from it, invoke the
client, record exactly one ``llm_runs`` row (success or
``status="failed"``), and — when JSON is expected — parse it with a
single **tracked** retry whose row is linked to the first via
``llm_run_links.relation="retry"``. On exhaustion it raises
:class:`LlmJsonError`, which carries the last raw response on ``.raw``
so callers can persist it for debugging.

It composes :class:`LlmRunRepo` directly rather than reusing the generic
``@track_run`` decorator: ``track_run`` cannot carry a spec-resolved
``system_prompt_id`` nor emit the retry-linked second row, which are the
whole reason this layer exists.

The client is injected, not coded in: any object exposing
``chat(messages=..., model=...) -> (content, usage)`` works — both
:class:`pf_core.clients.claude_code.ClaudeCodeClient` and
:class:`pf_core.clients.openrouter.OpenRouterClient` satisfy it. Stages
that need tools bake ``extra_args=["--allowedTools", ...]`` into the
client they pass in; this orchestrator stays backend-agnostic.

Usage::

    from pf_core.clients.claude_code import get_client
    from pf_core.llm import tracked_call
    from pf_core.llm.prompts import load_prompt_spec

    spec = load_prompt_spec("config/prompts/classifier.yaml",
                            expected_agent="classifier")
    parsed, run_id = tracked_call(
        client=get_client(model="haiku"),
        agent_type="classifier",
        spec=spec,
        model="haiku",
        render_kwargs={"rubric": rubric_text, "events": events_json},
        expect_json=True,
    )
"""

from __future__ import annotations

from typing import Any, Protocol

from pf_core.exceptions import AppError, InvalidInputError
from pf_core.llm.parse import parse_llm_json
from pf_core.llm.prompts import render_spec

try:
    from pf_core.llm.tracking import (
        LlmRunRepo,
        resolve_agent_type_id,
        resolve_prompt_id,
    )
except ImportError as e:  # pragma: no cover - exercised by the extra matrix
    from pf_core._extras import extra_import_error

    raise extra_import_error("tracking", "sqlalchemy", feature="pf_core.llm.tracked") from e

from pf_core.log import get_logger

logger = get_logger(__name__)

_MAX_ERROR_LEN = 10_000


class LlmJsonError(AppError):
    """The model returned unparseable JSON after the retry budget was spent.

    Carries the last raw response on :attr:`raw` so callers can persist it
    for debugging (e.g. write ``<label>.json.error`` next to the output).
    """

    def __init__(
        self,
        raw: str,
        context: dict | None = None,
        *,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(
            "LLM returned unparseable JSON after retry", context, cause=cause
        )
        self.raw = raw


class ChatClient(Protocol):
    """Structural type for an injected chat client.

    Both ``ClaudeCodeClient`` and ``OpenRouterClient`` satisfy this — a
    ``chat`` that takes ``messages`` + ``model`` and returns
    ``(content, usage)``.
    """

    def chat(
        self, messages: list[dict], model: str = ..., **kwargs: Any
    ) -> tuple[str, dict]: ...


def tracked_call(
    *,
    client: ChatClient,
    agent_type: str,
    spec: dict,
    model: str,
    render_kwargs: dict[str, Any] | None = None,
    part: str = "system",
    style: str = "@@",
    provider: str | None = None,
    expect_json: bool = False,
    json_retry: bool = True,
    repo: LlmRunRepo | None = None,
) -> tuple[Any, int]:
    """Run one tracked LLM call: render → invoke → record → optional JSON.

    Renders ``spec[part]`` with the chosen placeholder ``style``, resolves
    a ``system_prompt_id`` against ``llm_prompts`` (auto-registering the
    ``agent_type`` on first use), sends the rendered text as the user
    message, and records exactly one ``llm_runs`` row.

    Args:
        client: any object with ``chat(messages=..., model=...) ->
            (content, usage)``. Construct it with whatever per-stage
            options you need (model pin, ``--allowedTools``, timeout)
            before passing it in.
        agent_type: slug for ``llm_agent_types``. Auto-registered.
        spec: dict from :func:`pf_core.llm.prompts.load_prompt_spec` —
            must carry ``agent``, ``version`` and ``part``.
        model: model id/alias — passed to ``client.chat`` and recorded.
        render_kwargs: placeholder substitution values. For ``style="@@"``
            keys are upper-cased internally (token placeholders are
            ``@@UPPER@@``); for ``style="brace"`` they pass through.
            ``None``/empty for prompts with no placeholders.
        part: which spec section to render and record (default
            ``"system"``).
        style: ``"@@"`` (default) or ``"brace"`` — see
            :func:`pf_core.llm.prompts.render`.
        provider: optional value written to ``llm_runs.provider``.
        expect_json: when ``True``, parse the response via
            ``parse_llm_json(recover=True, strict=True)`` and return the
            parsed object; otherwise return the raw string.
        json_retry: when ``True`` (and ``expect_json``), retry once on a
            parse failure. The retry writes a second ``llm_runs`` row
            linked to the first via ``llm_run_links.relation="retry"``.
        repo: optional :class:`LlmRunRepo` to share a transaction or
            route writes in tests. Defaults to a fresh instance.

    Returns:
        ``(content_or_parsed, run_id)`` — ``content_or_parsed`` is a
        string when ``expect_json`` is ``False``, else the parsed
        dict/list. ``run_id`` is the row of the call whose output is
        returned (the retry row when the retry succeeded).

    Raises:
        LlmJsonError: ``expect_json`` is ``True`` and parsing failed on
            both the initial call and the retry (or ``json_retry`` is
            ``False`` and the initial parse failed).
    """
    if style == "@@":
        spec_kwargs = {k.upper(): v for k, v in (render_kwargs or {}).items()}
    else:
        spec_kwargs = dict(render_kwargs or {})
    rendered, version = render_spec(spec, part=part, style=style, **spec_kwargs)

    agent_type_id = resolve_agent_type_id(agent_type)
    system_prompt_id = resolve_prompt_id(
        agent_type_id=agent_type_id,
        part=part,
        version=version,
        content=spec[part],
    )

    _repo = repo if repo is not None else LlmRunRepo()

    content, run_id = _invoke_and_record(
        client=client,
        repo=_repo,
        agent_type=agent_type,
        model=model,
        provider=provider,
        rendered=rendered,
        system_prompt_id=system_prompt_id,
    )

    if not expect_json:
        return content, run_id

    try:
        return parse_llm_json(content, recover=True, strict=True), run_id
    except InvalidInputError:
        logger.warning(
            "llm_json_parse_failed",
            agent_type=agent_type,
            model=model,
            preview=(content or "")[:200],
        )
        if not json_retry:
            raise LlmJsonError(content)

    retry_content, retry_run_id = _invoke_and_record(
        client=client,
        repo=_repo,
        agent_type=agent_type,
        model=model,
        provider=provider,
        rendered=rendered,
        system_prompt_id=system_prompt_id,
        parent_run=(run_id, "retry"),
    )
    try:
        return parse_llm_json(retry_content, recover=True, strict=True), retry_run_id
    except InvalidInputError as exc:
        raise LlmJsonError(retry_content) from exc


def _invoke_and_record(
    *,
    client: ChatClient,
    repo: LlmRunRepo,
    agent_type: str,
    model: str,
    provider: str | None,
    rendered: str,
    system_prompt_id: int | None,
    parent_run: tuple[int, str] | None = None,
) -> tuple[str, int]:
    """One chat invocation + one ``llm_runs`` row.

    Records ``status="failed"`` on any client exception (timeout,
    non-zero exit, transport error) and re-raises so the caller decides
    whether to retry or abort.
    """
    logger.info("llm_call_start", agent_type=agent_type, model=model)
    try:
        content, usage = client.chat(
            messages=[{"role": "user", "content": rendered}],
            model=model,
        )
    except Exception as exc:
        repo.record(
            agent_type=agent_type,
            model=model,
            provider=provider,
            system_prompt_id=system_prompt_id,
            usage={"duration_ms": None},
            status="failed",
            error=str(exc)[:_MAX_ERROR_LEN],
            error_class=type(exc).__name__,
            rendered_prompts=(rendered, None),
            parent_run=parent_run,
        )
        raise

    duration_ms = usage.get("duration_ms")
    logger.info(
        "llm_call_done",
        agent_type=agent_type,
        model=model,
        duration_ms=duration_ms,
        content_len=len(content or ""),
    )
    run_id = repo.record(
        agent_type=agent_type,
        model=model,
        provider=provider,
        system_prompt_id=system_prompt_id,
        usage={k: v for k, v in usage.items() if k != "system_fingerprint"},
        model_fingerprint=usage.get("system_fingerprint"),
        rendered_prompts=(rendered, None),
        raw_response=content,
        parent_run=parent_run,
    )
    return content, run_id


__all__ = ["ChatClient", "LlmJsonError", "tracked_call"]
