"""Claude Code CLI client.

Thin wrapper around the local ``claude --print`` subprocess. Uses the
machine's active Claude Max session; consumes no API credits.

Implements the same ``.chat(messages, model, ...) -> (content, usage)``
interface as :class:`pf_core.clients.openrouter.OpenRouterClient` so a
caller can swap clients transparently. Per-agent routing between the two
backends is the job of the model router — :func:`pf_core.llm.router.resolve_agent`.

Notes:

- ``model`` is passed verbatim as ``--model X`` to
  the CLI. Without it, ``claude --print`` runs against the user's active
  interactive session model, which can silently chew through Claude Max
  quota in batch consumers. Resolution: per-call ``chat(model=...)`` >
  ``ClaudeCodeClient(model=...)`` constructor arg > ``$PF_CORE_CLAUDE_CODE_MODEL``
  env var > no flag (session default). The string is whatever
  ``claude --model`` accepts (``haiku``, ``sonnet``, ``opus``, or full
  IDs like ``claude-haiku-4-5``).
- ``temperature``, ``max_tokens``, ``top_p``, and ``response_format`` are
  accepted but **ignored** — the active Claude Code session controls
  sampling. They're in the signature so callers can use the same kwargs
  they'd use for OpenRouter without code changes.
- Token counts in the returned ``usage`` dict are always 0 (the CLI does
  not expose them). ``duration_ms`` is wall-clock from invocation.
- ``cost_usd`` is always 0.0 — Claude Max sessions don't bill per-call.
- All ``system`` messages are joined (in encountered order) and separated
  from the joined non-system messages with ``\\n\\n---\\n\\n``, then sent
  as a single prompt. See :func:`_flatten_messages`.

Usage::

    from pf_core.clients.claude_code import get_client

    # Pin to haiku for batch work — protects Claude Max quota
    client = get_client(model="haiku")
    content, usage = client.chat(
        messages=[
            {"role": "system", "content": "You are a summarizer."},
            {"role": "user", "content": "Summarize this..."},
        ],
    )
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import Any

from pf_core.exceptions import AppError
from pf_core.log import get_logger

_log = get_logger(__name__)


# Default wall-clock cap for a single ``claude --print`` invocation.
# Long-form synthesis (multi-step summaries) can
# exceed a minute, but anything past 10 minutes is almost certainly a
# hung subprocess. Per-instance override via ``ClaudeCodeClient(timeout=N)``.
DEFAULT_TIMEOUT_SECONDS = 600

# Wall-clock cap for ``preflight()`` — should be much shorter than the
# per-call default. The whole point is to fail fast on a logged-out
# session; if a ``claude --print "ok"`` round-trip takes longer than
# 30 seconds, something is worth knowing about.
DEFAULT_PREFLIGHT_TIMEOUT_SECONDS = 30

# --safe-mode runs `claude --print` with all customizations off (project
# CLAUDE.md, skills, hooks, plugins). On by default via isolate=True; the
# why lives in ClaudeCodeClient's `isolate` docstring.
_SAFE_MODE_FLAG = "--safe-mode"


class ClaudeCodeError(AppError):
    """The ``claude --print`` subprocess call failed (binary missing,
    non-zero exit, or wall-clock timeout)."""


_MODEL_ENV_VAR = "PF_CORE_CLAUDE_CODE_MODEL"


class ClaudeCodeClient:
    """Run chat completions through the local Claude Code CLI.

    Args:
        timeout: Wall-clock cap (seconds) for a single ``claude --print``
            call. Defaults to :data:`DEFAULT_TIMEOUT_SECONDS`.
        binary: Path to the ``claude`` executable. Defaults to whatever
            is on the user's ``PATH``. Override for non-standard installs.
        extra_args: Additional CLI flags inserted after ``--safe-mode``
            (when ``isolate=True``) and before ``--model`` / ``--print``
            (e.g. ``["--allowedTools", "Bash"]``). Empty by default.
        model: Default model passed as ``--model X`` on every call. Falls
            back to ``$PF_CORE_CLAUDE_CODE_MODEL`` if not provided. Set
            to ``None`` (the default) to omit the flag entirely and let
            the active session decide. Per-call ``chat(model=...)``
            overrides this for one call.
        isolate: Run with ``--safe-mode`` so the call ignores the ambient
            project's CLAUDE.md / skills / hooks / plugins (auth, model, and
            explicit flags still apply). ``True`` by default — the secure
            choice for a programmatic call, which almost never wants the
            surrounding repo's instructions. Set ``False`` only when a
            consumer deliberately runs claude inside a project to use that
            project's customizations. See :data:`_SAFE_MODE_FLAG`.
    """

    def __init__(
        self,
        *,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        binary: str = "claude",
        extra_args: list[str] | None = None,
        model: str | None = None,
        retry: int = 0,
        isolate: bool = True,
    ) -> None:
        self.timeout = timeout
        self.binary = binary
        self.extra_args = list(extra_args or [])
        self.model = model if model is not None else os.environ.get(_MODEL_ENV_VAR) or None
        self.retry = retry
        self.isolate = isolate
        _log.info(
            "claude_code_client_init",
            binary=self.binary,
            model=self.model,
            timeout=self.timeout,
            retry=self.retry,
            isolate=self.isolate,
        )

    def chat(
        self,
        messages: list[dict],
        model: str = "",
        temperature: float = 0.2,
        max_tokens: int = 4096,
        top_p: float = 1.0,
        response_format: dict | None = None,
        timeout: int | None = None,
        **kwargs: Any,
    ) -> tuple[str, dict]:
        """Run one chat completion via ``claude --print``.

        ``model`` (when non-empty) is added to the subprocess as
        ``--model X``; an empty string falls through to the instance
        default (which itself falls back to ``$PF_CORE_CLAUDE_CODE_MODEL``,
        and finally to no flag at all).

        ``temperature`` / ``max_tokens`` / ``top_p`` / ``response_format``
        are accepted for API parity with :class:`OpenRouterClient` but
        ignored — the active Claude Code session decides sampling.
        ``timeout`` overrides the per-instance default just for this call.

        Returns:
            ``(content, usage)`` where ``usage`` carries the same keys as
            ``OpenRouterClient.chat`` (token counts and cost are 0).
        """
        binary_path = shutil.which(self.binary)
        if binary_path is None:
            raise ClaudeCodeError(
                f"`{self.binary}` CLI not found on PATH. "
                "Install Claude Code and ensure the binary is accessible.",
                context={"binary": self.binary},
            )

        prompt = _flatten_messages(messages)
        if not prompt:
            raise ClaudeCodeError(
                "messages list contained no usable user content",
                context={"messages_count": len(messages)},
            )

        resolved_model = _translate_model(model or self.model)
        model_flag = ["--model", resolved_model] if resolved_model else []
        # isolate=True prepends --safe-mode (leads the argv). See _SAFE_MODE_FLAG.
        isolation_args = [_SAFE_MODE_FLAG] if self.isolate else []
        # Prompt goes on stdin, not argv. Argv has a hard OS limit
        # (ARG_MAX, ~256 KB on macOS) — large prompts (multi-day reports,
        # bundled context) blow past it and raise E2BIG: "Argument list
        # too long". stdin has no such limit. `claude --print` reads
        # stdin when no positional prompt is supplied.
        cmd = [binary_path, *isolation_args, *self.extra_args, *model_flag, "--print"]
        wall_timeout = timeout if timeout is not None else self.timeout

        # This transport authenticates via the active Claude Max session, NOT
        # an API key (see module docstring: "consumes no API credits"). Strip
        # ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN from the child env so a key
        # present in the parent environment can't hijack `claude --print` into
        # (billable, and possibly invalid) external API-key auth — which
        # silently breaks the documented $0 subscription path.
        sub_env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
        }

        # Up to (self.retry + 1) attempts. Default retry=0 means one shot.
        # Both timeout and non-zero exit are treated as transient — both
        # have legitimate retryable causes (rate-limit windows, momentary
        # auth refresh, model warm-up). Missing binary / empty messages
        # already raised above and aren't reached here.
        result = None
        elapsed_ms = 0
        for attempt in range(self.retry + 1):
            t0 = time.monotonic()
            try:
                result = subprocess.run(  # noqa: S603 — binary resolved above
                    cmd,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    timeout=wall_timeout,
                    env=sub_env,
                )
            except subprocess.TimeoutExpired:
                if attempt < self.retry:
                    _log.warning(
                        "claude_code_retry_timeout",
                        attempt=attempt + 1,
                        of=self.retry + 1,
                        timeout=wall_timeout,
                    )
                    continue
                raise ClaudeCodeError(
                    f"`{self.binary} --print` timed out after {wall_timeout}s "
                    f"(after {attempt + 1} attempt(s))",
                    context={"timeout": wall_timeout, "attempts": attempt + 1},
                )
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            if result.returncode != 0:
                if attempt < self.retry:
                    _log.warning(
                        "claude_code_retry_nonzero",
                        attempt=attempt + 1,
                        of=self.retry + 1,
                        returncode=result.returncode,
                        stderr_head=result.stderr[:200],
                    )
                    continue
                raise ClaudeCodeError(
                    f"`{self.binary} --print` exited {result.returncode} "
                    f"(after {attempt + 1} attempt(s)): {result.stderr[:500]}",
                    context={
                        "returncode": result.returncode,
                        "stderr_head": result.stderr[:200],
                        "attempts": attempt + 1,
                    },
                )
            break  # success — exit retry loop

        # Unreachable: every loop path either breaks (success) or raises.
        # The assert is for the type checker.
        assert result is not None  # noqa: S101

        content = result.stdout.strip()

        # Match the OpenRouterClient.chat usage-dict shape so the two
        # clients are drop-in interchangeable. Fields that don't apply
        # to the CLI return 0 / 0.0 / None.
        usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
            "cost_usd": 0.0,
            "duration_ms": elapsed_ms,
            "system_fingerprint": None,
        }
        return content, usage

    def preflight(self, *, timeout: int = DEFAULT_PREFLIGHT_TIMEOUT_SECONDS) -> None:
        """Smoke-test the local Claude Code session before launching a batch.

        Issues one ``claude --print "ok"`` against the configured binary
        and model. Returns ``None`` on success; raises
        :class:`ClaudeCodeError` with an actionable ``<binary> /login``
        remediation message on any failure (auth lapse, missing binary,
        timeout, non-zero exit).

        Useful before a long fan-out of calls — catches a logged-out
        session in single-digit seconds instead of after N failed
        subprocess invocations. The raised error carries
        ``context["preflight"] = True`` so log filters can distinguish
        preflight failures from per-call failures.

        Args:
            timeout: Wall-clock cap (seconds) for the smoke call.
                Defaults to :data:`DEFAULT_PREFLIGHT_TIMEOUT_SECONDS` —
                preflight should complete in single-digit seconds; if
                it doesn't, something is worth knowing about.
        """
        try:
            content, _ = self.chat(
                messages=[{"role": "user", "content": "ok"}],
                timeout=timeout,
            )
        except ClaudeCodeError as e:
            ctx = dict(e.context or {})
            ctx["preflight"] = True
            raise ClaudeCodeError(
                f"Claude Code preflight failed (binary={self.binary}, "
                f"model={self.model}). Most likely the session needs to "
                f"be re-authenticated:\n\n"
                f"    {self.binary} /login\n\n"
                f"Underlying error: {e}",
                context=ctx,
                cause=e,
            )
        _log.info(
            "claude_code_preflight_ok",
            binary=self.binary,
            model=self.model,
            content_head=content[:80],
        )


def _translate_model(model: str | None) -> str:
    """Strip an OpenRouter-style ``provider/`` prefix from a model id.

    ``pf_core.clients.routing.get_routed_client`` claims backend
    transparency: a single ``model`` string in a consumer's config has
    to work whether the call lands on OpenRouter (which expects
    ``provider/model`` like ``anthropic/claude-3.7-sonnet``) or on
    Claude Code (whose ``--model`` flag wants the bare id like
    ``claude-3.7-sonnet``). We translate at cmd-build time so consumers
    never have to maintain two model strings per agent.

    Strings without a slash pass through unchanged. A string that
    translates to the empty string (e.g. ``"anthropic/"``) is treated
    as "no model override" — the caller gets the active session model
    rather than ``--model`` with an empty value.
    """
    if not model:
        return ""
    if "/" not in model:
        return model
    return model.rsplit("/", 1)[-1]


def _flatten_messages(messages: list[dict]) -> str:
    """Collapse a chat-message list into a single ``claude --print`` prompt.

    System messages (in encountered order) are joined and then separated
    from user content with ``\\n\\n---\\n\\n``. Multiple user / assistant
    messages are joined with blank lines. Returns the empty string when
    the messages list yields no content.
    """
    system_parts: list[str] = []
    body_parts: list[str] = []
    for msg in messages or []:
        role = (msg.get("role") or "").lower()
        content = msg.get("content") or ""
        if not content:
            continue
        if role == "system":
            system_parts.append(content)
        else:
            body_parts.append(content)

    body = "\n\n".join(body_parts).strip()
    if not body:
        return ""

    if not system_parts:
        return body
    system_block = "\n\n".join(system_parts).strip()
    return f"{system_block}\n\n---\n\n{body}"


# ---------------------------------------------------------------------------
# Module-level singletons (one per model)
# ---------------------------------------------------------------------------
#
# A consumer may legitimately want different models for different tasks in
# the same process — e.g. a pipeline using ``sonnet`` for text
# analysis and ``haiku`` for vision. Caching by ``model`` lets each task pin its
# own client without stepping on the others' configuration. Calls with
# the same ``model`` value share an instance; the first call with a given
# ``model`` wins for the other args (timeout / binary / extra_args).

_clients: dict[str | None, ClaudeCodeClient] = {}


def get_client(
    *,
    timeout: int | None = None,
    binary: str | None = None,
    extra_args: list[str] | None = None,
    model: str | None = None,
    retry: int = 0,
    isolate: bool = True,
) -> ClaudeCodeClient:
    """Return the per-model singleton, creating it on first call for that model.

    The cache is keyed on the explicitly-passed ``model`` value (so
    ``get_client(model="haiku")`` and ``get_client(model="sonnet")`` are
    distinct instances; ``get_client()`` is its own slot under the key
    ``None``). For each cache slot, the first call's args win — later
    calls return the cached instance and ignore ``timeout`` / ``binary``
    / ``extra_args`` / ``retry`` / ``isolate``. Because ``isolate`` defaults
    to ``True``, the cached client is safe-mode-isolated by default — a later
    caller cannot accidentally downgrade a model's shared client to a
    non-isolated one. Use :func:`reset_client` to drop all cached instances
    (useful for testing), or :func:`new_client` for a fresh,
    independently-configured instance.
    """
    if model not in _clients:
        _clients[model] = new_client(
            timeout=timeout,
            binary=binary,
            extra_args=extra_args,
            model=model,
            retry=retry,
            isolate=isolate,
        )
    return _clients[model]


def new_client(
    *,
    timeout: int | None = None,
    binary: str | None = None,
    extra_args: list[str] | None = None,
    model: str | None = None,
    retry: int = 0,
    isolate: bool = True,
) -> ClaudeCodeClient:
    """Construct a fresh client with :func:`get_client`'s defaults but no
    caching — every call returns a new instance.

    The escape hatch from the per-model singleton's first-call-wins
    semantics: use it (directly, or via per-backend ``client_kwargs`` in
    the model router) when different agents need differently-tuned clients
    (timeout, retry, isolate) in one process.
    """
    return ClaudeCodeClient(
        timeout=timeout if timeout is not None else DEFAULT_TIMEOUT_SECONDS,
        binary=binary if binary is not None else "claude",
        extra_args=extra_args,
        model=model,
        retry=retry,
        isolate=isolate,
    )


def reset_client() -> None:
    """Drop all cached per-model singletons. Useful for tests."""
    _clients.clear()
