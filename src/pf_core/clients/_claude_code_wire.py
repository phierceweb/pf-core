"""Wire-format translation between the chat interface and the ``claude`` CLI.

Argv-facing model strings and tool flags, the system-prompt file, the stdin
prompt, and the ``--output-format json`` envelope on stdout. Kept apart from
:mod:`pf_core.clients.claude_code`, which owns the subprocess itself.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress

from pf_core.exceptions import ConfigurationError

REPLACE_SYSTEM_PROMPT_FLAGS = ("--system-prompt", "--system-prompt-file")
APPEND_SYSTEM_PROMPT_FLAGS = ("--append-system-prompt", "--append-system-prompt-file")
TOOLS_FLAG = "--tools"
# Resuming needs the transcript the client otherwise tells the CLI not to save.
SESSION_CONTINUATION_FLAGS = ("--resume", "-r", "--continue", "-c", "--session-id")
TOOL_GRANT_FLAGS = ("--allowedTools", "--allowed-tools")
SYSTEM_PROMPT_FILE_FLAG = "--system-prompt-file"
APPEND_SYSTEM_PROMPT_FILE_FLAG = "--append-system-prompt-file"

# The CLI's own truncation predicate, normalised to OpenRouter's "length".
TRUNCATION_STOP_REASONS = ("max_tokens", "model_context_window_exceeded")

_ENVELOPE_TOKEN_KEYS = {
    "input_tokens": "prompt_tokens",
    "output_tokens": "completion_tokens",
    "cache_read_input_tokens": "cache_read_tokens",
    "cache_creation_input_tokens": "cache_write_tokens",
}


def _envelope_tokens(envelope: dict) -> dict[str, int]:
    usage = envelope.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    out: dict[str, int] = {}
    for src, dst in _ENVELOPE_TOKEN_KEYS.items():
        v = usage.get(src)
        out[dst] = v if isinstance(v, int) and not isinstance(v, bool) else 0
    return out


def unwrap_envelope(stdout: str) -> tuple[str, str | None, dict[str, int]] | None:
    """Read ``(content, finish_reason, tokens)`` out of a ``--output-format json`` envelope.

    Returns ``None`` for anything that is not the documented shape — the caller
    decides what that means. ``stop_reason`` is a nullable free string in the
    CLI's schema, not a closed enum: only the two token-limit values are
    normalised (to OpenRouter's ``"length"``) and the rest pass through. An
    ``is_error`` envelope reports no reason at all — the CLI emits the result
    message after any error message, so a trailing error marks the whole run.
    A transcript array (``--verbose``) unwraps via its final ``result`` entry.

    ``tokens`` maps the envelope's ``usage`` counts onto the shared usage-dict
    keys (``prompt_tokens`` / ``completion_tokens`` / ``cache_read_tokens`` /
    ``cache_write_tokens``); fields the envelope omits (older CLIs) are 0.
    """
    try:
        envelope = json.loads(stdout)
    except ValueError:
        return None
    if isinstance(envelope, list):
        results = [e for e in envelope if isinstance(e, dict) and e.get("type") == "result"]
        envelope = results[-1] if results else None
    if not isinstance(envelope, dict):
        return None
    text = envelope.get("result")
    if not isinstance(text, str):
        return None
    tokens = _envelope_tokens(envelope)
    stop_reason = envelope.get("stop_reason")
    if envelope.get("is_error") or not isinstance(stop_reason, str):
        return text.strip(), None, tokens
    if stop_reason in TRUNCATION_STOP_REASONS:
        return text.strip(), "length", tokens
    return text.strip(), stop_reason, tokens


def envelope_error(stdout: str) -> str | None:
    """The error text of an ``is_error`` envelope, or ``None``."""
    try:
        envelope = json.loads(stdout)
    except ValueError:
        return None
    if isinstance(envelope, list):
        results = [e for e in envelope if isinstance(e, dict) and e.get("type") == "result"]
        envelope = results[-1] if results else None
    if not isinstance(envelope, dict) or not envelope.get("is_error"):
        return None
    text = envelope.get("result")
    return text.strip() if isinstance(text, str) else "unknown error"


def translate_model(model: str | None) -> str:
    """Strip an OpenRouter-style ``provider/`` prefix from a model id.

    Strings without a slash pass through; one that strips to ``""`` (``"anthropic/"``)
    means no ``--model`` flag.
    """
    if not model:
        return ""
    if "/" not in model:
        return model
    return model.rsplit("/", 1)[-1]


def has_flag(args: Sequence[str], names: Sequence[str]) -> bool:
    """Whether ``args`` carries any of ``names``, as ``--flag value`` or ``--flag=value``."""
    return any(a == n or a.startswith(f"{n}=") for a in args for n in names)


def tool_args(tools: Sequence[str]) -> list[str]:
    """``--tools`` for the named built-in tools; with none, no built-in or MCP tools at all.

    ``--tools ""`` removes only the built-in set: without ``--safe-mode`` the user's
    MCP servers still load their tools unless ``--strict-mcp-config`` is passed.
    """
    if tools:
        return [TOOLS_FLAG, ",".join(tools)]
    return [TOOLS_FLAG, "", "--strict-mcp-config"]


def checked_tools(tools: Sequence[str] | None, extra_args: Sequence[str]) -> list[str]:
    """``tools`` as a list. Raises ConfigurationError for a combination that cannot work."""
    if isinstance(tools, str):
        raise ConfigurationError(f"tools must be a list of tool names, not the string {tools!r}")
    tools = list(tools or ())
    mcp = [t for t in tools if t.startswith("mcp__")]
    if mcp:
        raise ConfigurationError(
            f"tools takes built-in tool names; MCP tools ({', '.join(mcp)}) load from the "
            "user's MCP config — run with isolate=False and grant them with --allowedTools "
            "in extra_args"
        )
    owns_tools = has_flag(extra_args, (TOOLS_FLAG,))
    if tools and owns_tools:
        raise ConfigurationError(
            "pass built-in tools as tools=[...] or as --tools in extra_args, not both"
        )
    if not tools and not owns_tools and has_flag(extra_args, TOOL_GRANT_FLAGS):
        raise ConfigurationError(
            "extra_args grants tools with --allowedTools, but tools=() gives the model "
            "none to call: pass the built-in tools it may use as tools=[...]"
        )
    return tools


def split_messages(messages: list[dict]) -> tuple[str, str]:
    """``(system, body)`` of a chat-message list, each side joined with blank lines."""
    system_parts: list[str] = []
    body_parts: list[str] = []
    for msg in messages or []:
        content = msg.get("content") or ""
        if not content:
            continue
        if (msg.get("role") or "").lower() == "system":
            system_parts.append(content)
        else:
            body_parts.append(content)
    return "\n\n".join(system_parts).strip(), "\n\n".join(body_parts).strip()


def flatten_messages(messages: list[dict]) -> str:
    """Collapse a chat-message list into a single ``claude --print`` prompt."""
    system, body = split_messages(messages)
    if not body:
        return ""
    return f"{system}\n\n---\n\n{body}" if system else body


def stdin_and_system_flag(
    messages: list[dict],
    system: str,
    body: str,
    *,
    extra_args: Sequence[str],
    agent_prompt: bool,
) -> tuple[str, str | None]:
    """The stdin prompt, and the flag that sends ``system`` in a file (None: no file).

    When ``extra_args`` leaves the client no system-prompt flag to use — it
    replaces the prompt itself, or appends while ``agent_prompt`` needs the one
    append slot — the system messages go ahead of the body on stdin instead.
    """
    if has_flag(extra_args, REPLACE_SYSTEM_PROMPT_FLAGS):
        return flatten_messages(messages), None
    if agent_prompt:
        if has_flag(extra_args, APPEND_SYSTEM_PROMPT_FLAGS):
            return flatten_messages(messages), None
        return body, APPEND_SYSTEM_PROMPT_FILE_FLAG if system else None
    return body, SYSTEM_PROMPT_FILE_FLAG


@contextmanager
def system_prompt_file(text: str) -> Iterator[str]:
    """Path of a private temp file (mode 0600) holding ``text``, removed on exit.

    A file rather than ``--system-prompt <text>``: argv has an ARG_MAX ceiling
    and is visible to other users through ``ps``.
    """
    fd, path = tempfile.mkstemp(prefix="pf-core-claude-system-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        yield path
    finally:
        with suppress(FileNotFoundError):
            os.unlink(path)
