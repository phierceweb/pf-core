"""Tracked LLM calls — invoke a client and record exactly one ``llm_runs`` row.

Two shapes over the same recording contract (failure rows on client
exceptions, then re-raise):

- :func:`tracked_call` — render a spec into a single user message, resolve
  its ``system_prompt_id``, invoke, record; with ``expect_json=True``, parse
  with one tracked retry (linked via ``llm_run_links.relation="retry"``),
  raising :class:`LlmJsonError` (raw response on ``.raw``) on exhaustion.
- :func:`tracked_messages_call` — the same contract for a verbatim message
  list.

The client is injected: anything exposing ``chat(messages=..., model=...)
-> (content, usage)``. See ``docs/llm-tracked.md`` for usage and the
comparison with the ``@track_run`` decorator.
"""

from __future__ import annotations

import time
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
    from pf_core.llm.tracking.decorator import _extract_rendered_prompts
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


def tracked_messages_call(
    *,
    client: ChatClient,
    agent_type: str,
    messages: list[dict],
    model: str,
    sampling: dict[str, Any] | None = None,
    chat_kwargs: dict[str, Any] | None = None,
    spec: dict | None = None,
    spec_on_change: str = "keep_first",
    provider: str | None = None,
    input_hash: str | None = None,
    configs: dict[str, int] | None = None,
    tags: list[str] | None = None,
    metrics: dict[str, float] | None = None,
    items_out: int | None = None,
    on_record_error: str = "raise",
    repo: LlmRunRepo | None = None,
) -> tuple[str, dict, int | None]:
    """One tracked call with a verbatim *messages* list.

    The messages-based sibling of :func:`tracked_call` (which renders a spec
    into a single user message). Sends *messages* unchanged, records exactly
    one ``llm_runs`` row — ``status="failed"`` with the error captured when
    the client raises (then re-raises) — and returns
    ``(content, usage, run_id)``.

    ``sampling`` is forwarded to ``chat()`` AND recorded; ``chat_kwargs`` is
    forwarded only (transport options like ``response_format``/``timeout``
    are not sampling). ``spec`` — a :func:`~pf_core.llm.prompts.load_prompt_spec`
    dict, or minimally ``{"version": int, "system": str}`` — registers the
    canonical system (and, when present, user) template in ``llm_prompts``
    and stamps the ids on the run; ``spec_on_change`` is passed through to
    ``resolve_prompt_id``. ``on_record_error="warn"`` makes the tracking sink
    best-effort: a failed ``record()`` logs a warning and yields
    ``run_id=None`` instead of masking the call result.

    Raises:
        InvalidInputError: unknown ``on_record_error`` value.
    """
    if on_record_error not in ("raise", "warn"):
        raise InvalidInputError(
            f"on_record_error must be 'raise' or 'warn', got {on_record_error!r}"
        )

    system_prompt_id: int | None = None
    user_prompt_id: int | None = None
    if spec is not None:
        agent_type_id = resolve_agent_type_id(agent_type)
        version = int(spec["version"])
        system_prompt_id = resolve_prompt_id(
            agent_type_id=agent_type_id,
            part="system",
            version=version,
            content=spec["system"],
            on_change=spec_on_change,
        )
        if spec.get("user"):
            user_prompt_id = resolve_prompt_id(
                agent_type_id=agent_type_id,
                part="user",
                version=version,
                content=spec["user"],
                on_change=spec_on_change,
            )

    rendered_system, rendered_user = _extract_rendered_prompts(messages)
    _repo = repo if repo is not None else LlmRunRepo()

    def _record(**kwargs: Any) -> int | None:
        try:
            return _repo.record(
                agent_type=agent_type,
                model=model,
                provider=provider,
                sampling=sampling or None,
                system_prompt_id=system_prompt_id,
                user_prompt_id=user_prompt_id,
                input_hash=input_hash,
                configs=configs,
                tags=tags,
                metrics=metrics,
                rendered_prompts=(rendered_system, rendered_user),
                **kwargs,
            )
        except Exception:
            if on_record_error == "raise":
                raise
            logger.warning(
                "llm_run_record_failed", agent_type=agent_type, model=model
            )
            return None

    logger.info("llm_call_start", agent_type=agent_type, model=model)
    merged_kwargs = {**(sampling or {}), **(chat_kwargs or {})}
    t0 = time.monotonic()
    try:
        content, usage = client.chat(messages=messages, model=model, **merged_kwargs)
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        ctx = getattr(exc, "context", None) or {}
        http_status = ctx.get("status_code") if isinstance(ctx, dict) else None
        _record(
            usage={"duration_ms": elapsed_ms},
            status="failed",
            error=str(exc)[:_MAX_ERROR_LEN],
            error_class=type(exc).__name__,
            http_status=http_status if isinstance(http_status, int) else None,
        )
        raise

    usage.setdefault("duration_ms", int((time.monotonic() - t0) * 1000))
    logger.info(
        "llm_call_done",
        agent_type=agent_type,
        model=model,
        duration_ms=usage.get("duration_ms"),
        content_len=len(content or ""),
    )
    run_id = _record(
        usage={k: v for k, v in usage.items() if k != "system_fingerprint"},
        model_fingerprint=usage.get("system_fingerprint"),
        items_out=items_out,
        raw_response=content if isinstance(content, str) else None,
    )
    return content, usage, run_id


__all__ = ["ChatClient", "LlmJsonError", "tracked_call", "tracked_messages_call"]
