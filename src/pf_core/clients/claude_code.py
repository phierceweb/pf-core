"""Claude Code CLI client.

Thin wrapper around the local ``claude --print`` subprocess. Uses the
machine's active Claude Max session; consumes no API credits.

Implements the same ``.chat(messages, model, ...) -> (content, usage)``
interface as :class:`pf_core.clients.openrouter.OpenRouterClient` so a
caller can swap clients transparently. Per-agent routing between the two
backends is the job of the model router — :func:`pf_core.llm.router.resolve_agent`.

Defaults, flags and the ``usage`` fields are documented in ``docs/claude-code.md``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Sequence
from contextlib import ExitStack
from typing import Any

from pf_core.clients._claude_code_wire import (
    SESSION_CONTINUATION_FLAGS,
    TOOLS_FLAG,
    checked_tools,
    envelope_error,
    has_flag,
    split_messages,
    stdin_and_system_flag,
    system_prompt_file,
    tool_args,
    translate_model,
    unwrap_envelope,
)
from pf_core.exceptions import ClientError
from pf_core.log import get_logger

_log = get_logger(__name__)


# Long-form synthesis can exceed a minute; past 10 minutes the subprocess is
# almost certainly hung.
DEFAULT_TIMEOUT_SECONDS = 600

# preflight() exists to fail fast on a logged-out session.
DEFAULT_PREFLIGHT_TIMEOUT_SECONDS = 30

_SAFE_MODE_FLAG = "--safe-mode"
_NO_PERSIST_FLAG = "--no-session-persistence"

# The JSON envelope carries the model's stop_reason; text-mode stdout has no
# equivalent, so truncation would be unreportable without it.
_OUTPUT_FORMAT_FLAG = "--output-format"

# extra_args that change stdout's shape suppress envelope mode; stdout is read as text.
_SHAPE_FLAGS = (_OUTPUT_FORMAT_FLAG, "--verbose")

# Backoff before retry N (0-based): _RETRY_BACKOFF_BASE * (N + 1) seconds.
_RETRY_BACKOFF_BASE = 0.5


class ClaudeCodeError(ClientError):
    """A ``claude --print`` call could not run, failed, or reported an error."""


_MODEL_ENV_VAR = "PF_CORE_CLAUDE_CODE_MODEL"


class ClaudeCodeClient:
    """Run chat completions through the local Claude Code CLI.

    Args:
        timeout: Wall-clock cap (seconds) for a single ``claude --print``
            call. Defaults to :data:`DEFAULT_TIMEOUT_SECONDS`.
        binary: Path to the ``claude`` executable. Defaults to whatever
            is on the user's ``PATH``. Override for non-standard installs.
        extra_args: CLI flags inserted after the client's own and before
            ``--model`` / ``--print``. A ``--tools`` here replaces the client's
            tool flags. An append system-prompt flag composes with the client's
            ``--system-prompt-file``; a replace flag, or an append flag with
            ``agent_prompt=True``, replaces it and puts the system messages ahead
            of the user content on stdin. ``--output-format`` or ``--verbose``
            suppresses the JSON envelope, so stdout is read as text and no
            ``finish_reason`` is reported.
        model: Default model passed as ``--model X``; falls back to
            ``$PF_CORE_CLAUDE_CODE_MODEL``, and ``None`` omits the flag.
            Per-call ``chat(model=...)`` overrides it.
        isolate: Run with ``--safe-mode`` so the call ignores the ambient
            project's CLAUDE.md / skills / hooks / plugins. ``True`` by default
            — a programmatic call almost never wants the surrounding repo's
            instructions; ``False`` deliberately uses them.
        persist_session: Let the CLI save each call as a session transcript
            under ``~/.claude/projects``. ``False`` by default (passes
            ``--no-session-persistence``); set ``True`` to keep transcripts
            for debugging a specific call. A ``--resume`` / ``--continue`` /
            ``--session-id`` in ``extra_args`` keeps them regardless — a
            resumable session is the point of that flag.
        tools: Built-in tools the model may call, passed as ``--tools``
            (e.g. ``["Read", "Grep"]``, or ``["default"]`` for the CLI's full
            set). Empty by default — a completion needs none — which passes
            ``--tools ""`` and ``--strict-mcp-config``, so no built-in or MCP
            tool loads. A prompt that asks for a tool this leaves out gets an
            answer written without it, not an error. Raises
            ``ConfigurationError`` for MCP names, or for tools granted only
            through ``--allowedTools`` in ``extra_args``.
        agent_prompt: Keep Claude Code's own agent system prompt — its tool-use
            guidance and workspace context — and append the system messages to
            it (``--append-system-prompt-file``). ``False`` by default: the
            system messages replace it (``--system-prompt-file``), as the other
            backends send them.
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
        persist_session: bool = False,
        tools: Sequence[str] | None = (),
        agent_prompt: bool = False,
    ) -> None:
        self.timeout = timeout
        self.binary = binary
        self.extra_args = list(extra_args or [])
        self.model = model if model is not None else os.environ.get(_MODEL_ENV_VAR) or None
        self.retry = retry
        self.isolate = isolate
        self.persist_session = persist_session
        self.tools = checked_tools(tools, self.extra_args)
        self.agent_prompt = agent_prompt
        _log.info(
            "claude_code_client_init",
            binary=self.binary,
            model=self.model,
            timeout=self.timeout,
            retry=self.retry,
            isolate=self.isolate,
            persist_session=self.persist_session,
            tools=self.tools,
            agent_prompt=self.agent_prompt,
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

        ``model`` (when non-empty) is passed as ``--model X``; empty falls through
        to the instance default. ``temperature`` / ``max_tokens`` / ``top_p`` /
        ``response_format`` are accepted for API parity with
        :class:`OpenRouterClient` and ignored. ``timeout`` overrides the
        per-instance default for this call.

        Returns:
            ``(content, usage)``; ``usage`` carries ``OpenRouterClient.chat``'s
            keys, with token counts from the JSON envelope and ``cost_usd`` 0.0.
        """
        binary_path = shutil.which(self.binary)
        if binary_path is None:
            raise ClaudeCodeError(
                f"`{self.binary}` CLI not found on PATH. "
                "Install Claude Code and ensure the binary is accessible.",
                context={"binary": self.binary},
            )

        system, body = split_messages(messages)
        if not body:
            raise ClaudeCodeError(
                "messages list contained no usable user content",
                context={"messages_count": len(messages)},
            )

        resolved_model = translate_model(model or self.model)
        model_flag = ["--model", resolved_model] if resolved_model else []
        isolation_args = [_SAFE_MODE_FLAG] if self.isolate else []
        resuming = has_flag(self.extra_args, SESSION_CONTINUATION_FLAGS)
        persist_args = [] if self.persist_session or resuming else [_NO_PERSIST_FLAG]
        tools_args = [] if has_flag(self.extra_args, (TOOLS_FLAG,)) else tool_args(self.tools)
        envelope_mode = not any(
            a.startswith(flag) for a in self.extra_args for flag in _SHAPE_FLAGS
        )
        format_args = [_OUTPUT_FORMAT_FLAG, "json"] if envelope_mode else []
        prompt, system_flag = stdin_and_system_flag(
            messages, system, body, extra_args=self.extra_args, agent_prompt=self.agent_prompt
        )
        wall_timeout = timeout if timeout is not None else self.timeout

        with ExitStack() as stack:
            system_args = []
            if system_flag:
                system_args = [system_flag, stack.enter_context(system_prompt_file(system))]
            # No positional prompt: it goes on stdin, which has no ARG_MAX ceiling.
            cmd = [
                binary_path,
                *isolation_args,
                *persist_args,
                *tools_args,
                *system_args,
                *self.extra_args,
                *model_flag,
                *format_args,
                "--print",
            ]
            result, elapsed_ms = self._run(cmd, prompt, wall_timeout)

        content = result.stdout.strip()
        finish_reason: str | None = None
        tokens: dict[str, int] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
        if envelope_mode:
            unwrapped = unwrap_envelope(result.stdout)
            if unwrapped is None:
                # The client asked for JSON, so raw stdout is machine output,
                # not an answer — returning it as content would be corruption.
                raise ClaudeCodeError(
                    "claude --output-format json produced an unreadable envelope. "
                    "Upgrade the CLI, or pass extra_args=['--output-format', 'text'] "
                    "to restore text-mode output (truncation then unreportable).",
                    context={"model": resolved_model, "stdout_head": content[:200]},
                )
            content, finish_reason, tokens = unwrapped
            if not any(tokens.values()):
                _log.warning(
                    "claude_code_envelope_without_usage",
                    model=resolved_model,
                    message="no usage counts — a tokens budget cannot gate this call",
                )
            if envelope_error(result.stdout) is not None:
                raise ClaudeCodeError(
                    f"`{self.binary} --print` reported an error: {content[:500]}",
                    context={"model": resolved_model},
                )

        usage: dict[str, Any] = {
            **tokens,
            "reasoning_tokens": 0,
            "cost_usd": 0.0,
            "duration_ms": elapsed_ms,
            "system_fingerprint": None,
        }
        if finish_reason is not None:
            usage["finish_reason"] = finish_reason
        if finish_reason == "length":
            _log.warning(
                "claude_code_truncated",
                model=resolved_model,
                content_len=len(content),
            )
        return content, usage

    def _run(
        self, cmd: list[str], prompt: str, wall_timeout: int
    ) -> tuple[subprocess.CompletedProcess[str], int]:
        """Run ``cmd`` with ``prompt`` on stdin, retrying per ``self.retry`` → (result, elapsed_ms)."""
        # Strip the API-key vars: a key in the parent env would hijack
        # `claude --print` into billable API auth instead of the Max session.
        sub_env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
        }

        # Timeout and non-zero exit are both retryable: rate-limit windows,
        # auth refresh, model warm-up.
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
                    time.sleep(_RETRY_BACKOFF_BASE * (attempt + 1))
                    continue
                raise ClaudeCodeError(
                    f"`{self.binary} --print` timed out after {wall_timeout}s "
                    f"(after {attempt + 1} attempt(s))",
                    context={"timeout": wall_timeout, "attempts": attempt + 1},
                )
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            if result.returncode != 0:
                # In envelope mode the CLI reports errors in the JSON result on
                # stdout, not stderr — quote whichever carries the diagnosis.
                detail = result.stderr.strip() or envelope_error(result.stdout) or ""
                if attempt < self.retry:
                    _log.warning(
                        "claude_code_retry_nonzero",
                        attempt=attempt + 1,
                        of=self.retry + 1,
                        returncode=result.returncode,
                        error_head=detail[:200],
                    )
                    time.sleep(_RETRY_BACKOFF_BASE * (attempt + 1))
                    continue
                raise ClaudeCodeError(
                    f"`{self.binary} --print` exited {result.returncode} "
                    f"(after {attempt + 1} attempt(s)): {detail[:500]}",
                    context={
                        "returncode": result.returncode,
                        "error_head": detail[:200],
                        "attempts": attempt + 1,
                    },
                )
            break  # success — exit retry loop

        # Unreachable: every loop path either breaks (success) or raises.
        # The assert is for the type checker.
        assert result is not None  # noqa: S101
        return result, elapsed_ms

    def preflight(self, *, timeout: int = DEFAULT_PREFLIGHT_TIMEOUT_SECONDS) -> None:
        """Smoke-test the local Claude Code session before launching a batch.

        Issues one ``claude --print "ok"`` against the configured binary and
        model, catching a logged-out session in seconds instead of after N
        failed calls. Returns ``None`` on success; on any failure (auth lapse,
        missing binary, timeout, non-zero exit) raises :class:`ClaudeCodeError`
        naming the ``<binary> /login`` remediation, with
        ``context["preflight"] = True``.

        Args:
            timeout: Wall-clock cap (seconds) for the smoke call. Defaults to
                :data:`DEFAULT_PREFLIGHT_TIMEOUT_SECONDS`.
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


# ---------------------------------------------------------------------------
# Module-level singletons (one per distinct set of arguments)
# ---------------------------------------------------------------------------

_clients: dict[tuple[Any, ...], ClaudeCodeClient] = {}


def get_client(
    *,
    timeout: int | None = None,
    binary: str | None = None,
    extra_args: list[str] | None = None,
    model: str | None = None,
    retry: int = 0,
    isolate: bool = True,
    persist_session: bool = False,
    tools: Sequence[str] | None = (),
    agent_prompt: bool = False,
) -> ClaudeCodeClient:
    """Return the shared client for exactly these arguments, creating it on first use.

    Each distinct set of arguments is its own instance, so one caller's ``tools``,
    ``extra_args`` or ``isolate=False`` never reaches a client another caller asked for.
    """
    # A string stays one, so the constructor refuses it rather than a tuple splitting it.
    tool_names = tools if isinstance(tools, str) else tuple(tools or ())
    flags = tuple(extra_args or ())
    key = (timeout, binary, flags, model, retry, isolate, persist_session, tool_names, agent_prompt)
    if key not in _clients:
        _clients[key] = new_client(
            timeout=timeout,
            binary=binary,
            extra_args=list(flags),
            model=model,
            retry=retry,
            isolate=isolate,
            persist_session=persist_session,
            tools=tool_names,
            agent_prompt=agent_prompt,
        )
    return _clients[key]


def new_client(
    *,
    timeout: int | None = None,
    binary: str | None = None,
    extra_args: list[str] | None = None,
    model: str | None = None,
    retry: int = 0,
    isolate: bool = True,
    persist_session: bool = False,
    tools: Sequence[str] | None = (),
    agent_prompt: bool = False,
) -> ClaudeCodeClient:
    """A fresh, uncached client with :func:`get_client`'s defaults."""
    return ClaudeCodeClient(
        timeout=timeout if timeout is not None else DEFAULT_TIMEOUT_SECONDS,
        binary=binary if binary is not None else "claude",
        extra_args=extra_args,
        model=model,
        retry=retry,
        isolate=isolate,
        persist_session=persist_session,
        tools=tools,
        agent_prompt=agent_prompt,
    )


def reset_client() -> None:
    """Drop all cached singletons. Useful for tests."""
    _clients.clear()
