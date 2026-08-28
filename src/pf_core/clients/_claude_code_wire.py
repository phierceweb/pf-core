"""Wire-format translation between the chat interface and the ``claude`` CLI.

Argv-facing model strings, the flattened stdin prompt, and the
``--output-format json`` envelope on stdout. Kept apart from
:mod:`pf_core.clients.claude_code`, which owns the subprocess itself.
"""

from __future__ import annotations

import json

# The CLI's own truncation predicate, normalised to OpenRouter's "length".
TRUNCATION_STOP_REASONS = ("max_tokens", "model_context_window_exceeded")


def unwrap_envelope(stdout: str) -> tuple[str, str | None] | None:
    """Read ``(content, finish_reason)`` out of a ``--output-format json`` envelope.

    Returns ``None`` for anything that is not the documented shape — the caller
    decides what that means. ``stop_reason`` is a nullable free string in the
    CLI's schema, not a closed enum: only the two token-limit values are
    normalised (to OpenRouter's ``"length"``) and the rest pass through. An
    ``is_error`` envelope reports no reason at all — the CLI emits the result
    message after any error message, so a trailing error marks the whole run.
    A transcript array (``--verbose``) unwraps via its final ``result`` entry.
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
    stop_reason = envelope.get("stop_reason")
    if envelope.get("is_error") or not isinstance(stop_reason, str):
        return text.strip(), None
    if stop_reason in TRUNCATION_STOP_REASONS:
        return text.strip(), "length"
    return text.strip(), stop_reason


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


def flatten_messages(messages: list[dict]) -> str:
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
