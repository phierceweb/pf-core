"""The framework check's rules: hand-rolled code pf-core already replaces, each naming its fix.

A rule's ``name`` is what ``disable``, ``replace`` and ``exempt`` refer to. ``kind`` picks the
mechanism: ``import`` and ``raise`` match the AST, ``call`` is a regex over code with strings
and comments blanked, and ``json-write`` is its own AST shape.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class FrameworkRule:
    name: str
    kind: str  # "import" | "raise" | "call" | "json-write"
    use: str
    why: str
    pattern: re.Pattern[str] | None = None  # "call" rules only


_HTTP_USE = "pf_core.fetch for downloads, pf_core.clients for LLM and search APIs"
_HTTP_WHY = "timeouts, retries, pacing and an SSRF guard, already written and tested"


def _import(name: str, use: str, why: str) -> FrameworkRule:
    return FrameworkRule(name=name, kind="import", use=use, why=why)


def _raise(name: str, use: str, why: str) -> FrameworkRule:
    return FrameworkRule(name=name, kind="raise", use=use, why=why)


def _call(name: str, pattern: str, use: str, why: str) -> FrameworkRule:
    return FrameworkRule(name=name, kind="call", use=use, why=why, pattern=re.compile(pattern))


RULES: tuple[FrameworkRule, ...] = (
    _import(
        "logging",
        "pf_core.log.get_logger(__name__)",
        "structured events, and the project's log level and file wiring",
    ),
    _import(
        "dotenv",
        "pf_core.config.AppConfig",
        "entry points already load .env; a second loader fights the first",
    ),
    _import("requests", _HTTP_USE, _HTTP_WHY),
    _import("httpx", _HTTP_USE, _HTTP_WHY),
    _import("aiohttp", _HTTP_USE, _HTTP_WHY),
    _import("urllib3", _HTTP_USE, _HTTP_WHY),
    _import(
        "concurrent.futures",
        "pf_core.parallel.run_parallel",
        "a reporter, resilient failure collection, one cap on width",
    ),
    _import(
        "multiprocessing",
        "pf_core.parallel.run_parallel",
        "a reporter, resilient failure collection, without the process cost",
    ),
    _import(
        "hashlib",
        "pf_core.utils.hashing.content_hash, or pf_core.pipeline.run_record.file_sha256 for a file",
        "one spelling of a digest, and the file variant streams instead of slurping",
    ),
    _raise("Exception", "a pf_core.exceptions class", "an unsearchable log key and no context"),
    _raise("RuntimeError", "pf_core.exceptions.PreconditionError", "'required state not met'"),
    _raise(
        "ValueError", "pf_core.exceptions.InvalidInputError", "'the caller passed something bad'"
    ),
    _call(
        "env-read",
        r"\bos\.(?:environ|getenv)\b",
        "pf_core.utils.env.resolve_int / resolve_str / resolve_bool",
        "malformed values warn and fall back instead of crashing",
    ),
    _call(
        "print",
        r"(?<![\w.])print\s*\(",
        "pf_core.log.get_logger(__name__), or pf_core.output.ConsoleReporter at the CLI boundary",
        "print() in a library is output nobody can level, filter or redirect",
    ),
    _call(
        "logger-exception",
        r"(?i)(?<!\w)(?:\w+_|_+)?log(?:ger|ging)?\.exception\s*\(",
        "pf_core.log.log_exception",
        "it carries the structured context and the project's event prefix",
    ),
    _call(
        "atomic-write",
        r"\bos\.replace\s*\(",
        "pf_core.utils.io.atomic_write_text / _json / _bytes",
        "the temp-file-then-rename dance is already written and tested",
    ),
    FrameworkRule(
        name="json-write",
        kind="json-write",
        use="pf_core.utils.io.atomic_write_json",
        why="a torn write leaves a file that parses as neither record",
    ),
)

RULE_NAMES: frozenset[str] = frozenset(r.name for r in RULES)
