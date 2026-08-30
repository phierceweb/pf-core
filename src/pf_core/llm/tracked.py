"""Tracked LLM calls — invoke a client and record exactly one ``llm_runs`` row.

:func:`tracked_call` renders a prompt spec; :func:`tracked_messages_call`
takes a verbatim message list. Both record a failure row on a client
exception and re-raise.

The client is injected: anything exposing ``chat(messages=..., model=...)
-> (content, usage)``. See ``docs/llm-tracked.md``.
"""

from __future__ import annotations

import time
from typing import Any, Protocol

from pf_core.exceptions import AppError, DataError, InvalidInputError
from pf_core.llm.parse import parse_llm_json, truncated_from_usage
from pf_core.llm.prompts import render_spec
from pf_core.llm.recording import call_summary, current_session_metadata, record_call

try:
    from pf_core.llm.tracking import (
        LlmRunRepo,
        compute_input_hash,
        resolve_agent_type_id,
        resolve_prompt_id,
        split_metadata,
    )
    from pf_core.llm.tracking.decorator import _extract_rendered_prompts
except ImportError as e:  # pragma: no cover - exercised by the extra matrix
    from pf_core._extras import extra_import_error

    raise extra_import_error("tracking", "sqlalchemy", feature="pf_core.llm.tracked") from e

from pf_core.log import get_logger, log_exception

logger = get_logger(__name__)

_MAX_ERROR_LEN = 10_000


def _mark_parse_failure(repo: LlmRunRepo, run_id: int | None) -> None:
    """Best-effort: flip the rejected run's row to failed before LlmJsonError.

    A DB error here must never mask the parse failure being raised.
    """
    if run_id is None:
        return
    try:
        repo.mark_failed(
            run_id,
            error="response JSON unparseable after successful chat",
            error_class="LlmJsonError",
        )
    except Exception as exc:
        log_exception(
            DataError("mark_failed write failed", context={"run_id": run_id}, cause=exc),
            message_prepend="could not mark run failed",
        )


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
        super().__init__("LLM returned unparseable JSON after retry", context, cause=cause)
        self.raw = raw


class ChatClient(Protocol):
    """Structural type for an injected chat client.

    Both ``ClaudeCodeClient`` and ``OpenRouterClient`` satisfy this — a
    ``chat`` that takes ``messages`` + ``model`` and returns
    ``(content, usage)``.
    """

    def chat(self, messages: list[dict], model: str = ..., **kwargs: Any) -> tuple[str, dict]: ...


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
    on_truncation: str = "raise",
    on_record_error: str = "raise",
    repo: LlmRunRepo | None = None,
) -> tuple[Any, int | None]:
    """Run one tracked LLM call: render → invoke → record → optional JSON.

    Renders ``spec[part]`` (auto-registering ``agent_type`` and the prompt
    in ``llm_prompts``), sends it as the user message, and records exactly
    one ``llm_runs`` row. Full semantics: ``docs/llm-tracked.md``.

    Args:
        client: object with ``chat(messages=..., model=...) -> (content, usage)``.
        agent_type: ``llm_agent_types`` slug, auto-registered.
        spec: dict from :func:`pf_core.llm.prompts.load_prompt_spec`.
        model: model id/alias — passed to ``client.chat`` and recorded.
        render_kwargs: placeholder values (upper-cased for ``style="@@"``).
        part: spec section to render and record (default ``"system"``).
        style: ``"@@"`` or ``"brace"`` — see :func:`pf_core.llm.prompts.render`.
        provider: optional ``llm_runs.provider`` value.
        expect_json: parse the response and return the object instead of text.
        json_retry: retry a parse failure once (second run row linked via
            ``relation="retry"``); skipped when the provider reported
            truncation — the retry would truncate identically.
        on_truncation: ``"raise"`` (default) treats a provider-reported or
            recovered truncation as a parse failure; ``"warn"`` returns the
            salvaged prefix.
        on_record_error: ``"warn"`` logs a failed ``record()`` and yields
            ``run_id=None``; a record failure never masks a client exception.
        repo: optional :class:`LlmRunRepo` to share a transaction/route tests.

    Returns:
        ``(content_or_parsed, run_id)`` — the retry row's id when the retry
        succeeded; ``None`` when the row wasn't written under ``"warn"``.

    Raises:
        LlmJsonError: parsing failed after the retry budget; the rejected
            row is first marked failed via :meth:`LlmRunRepo.mark_failed`.
        InvalidInputError: unknown ``on_truncation``/``on_record_error``.
    """
    if on_truncation not in ("warn", "raise"):
        raise InvalidInputError(f"on_truncation must be 'warn' or 'raise', got {on_truncation!r}")
    if on_record_error not in ("raise", "warn"):
        raise InvalidInputError(
            f"on_record_error must be 'raise' or 'warn', got {on_record_error!r}"
        )

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

    content, usage, run_id = _invoke_and_record(
        client=client,
        repo=_repo,
        agent_type=agent_type,
        model=model,
        provider=provider,
        rendered=rendered,
        system_prompt_id=system_prompt_id,
        on_record_error=on_record_error,
    )

    if not expect_json:
        return content, run_id

    truncated = truncated_from_usage(usage)
    try:
        parsed = parse_llm_json(
            content,
            recover=True,
            strict=True,
            on_truncation=on_truncation,
            truncated=truncated,
        )
        return parsed, run_id
    except InvalidInputError:
        logger.warning(
            "llm_json_parse_failed",
            agent_type=agent_type,
            model=model,
            preview=(content or "")[:200],
            truncated=truncated,
        )
        # The retry resends the same prompt under the same cap, so a known
        # token-limit cut just truncates again at the caller's expense.
        if not json_retry or truncated:
            _mark_parse_failure(_repo, run_id)
            raise LlmJsonError(content)

    retry_content, retry_usage, retry_run_id = _invoke_and_record(
        client=client,
        repo=_repo,
        agent_type=agent_type,
        model=model,
        provider=provider,
        rendered=rendered,
        system_prompt_id=system_prompt_id,
        parent_run=(run_id, "retry") if run_id is not None else None,
        on_record_error=on_record_error,
    )
    try:
        parsed = parse_llm_json(
            retry_content,
            recover=True,
            strict=True,
            on_truncation=on_truncation,
            truncated=truncated_from_usage(retry_usage),
        )
        return parsed, retry_run_id
    except InvalidInputError as exc:
        _mark_parse_failure(_repo, retry_run_id)
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
    on_record_error: str = "raise",
) -> tuple[str, dict, int | None]:
    """One chat invocation + one ``llm_runs`` row.

    Records ``status="failed"`` on any client exception (timeout,
    non-zero exit, transport error) and re-raises so the caller decides
    whether to retry or abort. The rendered text is recorded in the *user*
    payload slot, matching its wire role — eval replays rebuild message
    roles from the slots, so slot and role must agree.
    """

    def _record(**kwargs: Any) -> int | None:
        try:
            return repo.record(
                agent_type=agent_type,
                model=model,
                provider=provider,
                system_prompt_id=system_prompt_id,
                rendered_prompts=(None, rendered),
                parent_run=parent_run,
                **kwargs,
            )
        except Exception:
            if on_record_error == "raise":
                raise
            logger.warning("llm_run_record_failed", agent_type=agent_type, model=model)
            return None

    logger.info("llm_call_start", agent_type=agent_type, model=model)
    try:
        content, usage = client.chat(
            messages=[{"role": "user", "content": rendered}],
            model=model,
        )
    except Exception as exc:
        try:
            _record(
                usage={"duration_ms": None},
                status="failed",
                error=str(exc)[:_MAX_ERROR_LEN],
                error_class=type(exc).__name__,
            )
        except Exception as rec_exc:
            # A tracking write must never displace the client failure.
            logger.warning(
                "llm_run_record_failed",
                agent_type=agent_type,
                model=model,
                error=str(rec_exc)[:_MAX_ERROR_LEN],
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
    run_id = _record(
        usage={k: v for k, v in usage.items() if k != "system_fingerprint"},
        model_fingerprint=usage.get("system_fingerprint"),
        raw_response=content,
    )
    return content, usage, run_id


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
    metadata: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    metrics: dict[str, float] | None = None,
    items_out: int | None = None,
    job_id: int | None = None,
    on_record_error: str = "raise",
    repo: LlmRunRepo | None = None,
) -> tuple[str, dict, int | None]:
    """One tracked call with a verbatim *messages* list.

    The messages-based sibling of :func:`tracked_call`: sends *messages*
    unchanged, records exactly one ``llm_runs`` row (``status="failed"``
    when the client raises, then re-raises), and returns
    ``(content, usage, run_id)``. Full semantics: ``docs/llm-tracked.md``.

    Non-obvious contracts: ``input_hash`` defaults to
    :func:`~pf_core.llm.tracking.compute_input_hash` over *messages*, so the
    recorded key matches the exact cache's. ``sampling`` is forwarded AND
    recorded; ``chat_kwargs`` is forwarded only (transport, not sampling).
    ``metadata`` splits via :func:`~pf_core.llm.tracking.split_metadata` and
    merges beneath explicit ``tags``/``metrics``; an open
    :mod:`pf_core.llm.recording` window merges beneath ``metadata`` and
    receives a per-call summary. ``on_record_error="warn"`` yields
    ``run_id=None`` on a failed ``record()`` instead of masking the result.

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

    combined_md = {**current_session_metadata(), **(metadata or {})}
    if combined_md:
        md_tags, md_metrics = split_metadata(combined_md)
        tags = list(dict.fromkeys([*md_tags, *(tags or [])]))
        metrics = {**md_metrics, **(metrics or {})}

    rendered_system, rendered_user = _extract_rendered_prompts(messages)
    # record()'s fallback would miss multi-part and assistant/tool content.
    resolved_hash = (
        input_hash
        if input_hash is not None
        else compute_input_hash(model=model, messages=messages, sampling=sampling, configs=configs)
    )
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
                input_hash=resolved_hash,
                configs=configs,
                tags=tags,
                metrics=metrics,
                job_id=job_id,
                rendered_prompts=(rendered_system, rendered_user),
                **kwargs,
            )
        except Exception:
            if on_record_error == "raise":
                raise
            logger.warning("llm_run_record_failed", agent_type=agent_type, model=model)
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
        failed_run_id = _record(
            usage={"duration_ms": elapsed_ms},
            status="failed",
            error=str(exc)[:_MAX_ERROR_LEN],
            error_class=type(exc).__name__,
            http_status=http_status if isinstance(http_status, int) else None,
        )
        record_call(
            call_summary(
                agent_type=agent_type,
                model=model,
                provider=provider,
                spec=spec,
                usage={"duration_ms": elapsed_ms},
                success=False,
                run_id=failed_run_id,
            )
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
    record_call(
        call_summary(
            agent_type=agent_type,
            model=model,
            provider=provider,
            spec=spec,
            usage=usage,
            success=True,
            run_id=run_id,
        )
    )
    return content, usage, run_id


__all__ = ["ChatClient", "LlmJsonError", "tracked_call", "tracked_messages_call"]
