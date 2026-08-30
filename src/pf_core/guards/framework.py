"""Framework check: refuse code that hand-rolls what pf-core already provides.

Every breach names its replacement. Imports, raises and the JSON-write shape are read from the
AST; the ``call`` rules are regexes over source with strings and comments blanked, because a
check that fires on prose gets skipped and then protects nothing. Exemptions are per rule and
per whole relative path, and one that no longer suppresses anything is stale.
"""

from __future__ import annotations

import ast
import dataclasses
import io
import re
import tokenize
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from pf_core.exceptions import ConfigurationError
from pf_core.guards.framework_config import FrameworkConfig, FrameworkExemption
from pf_core.guards.framework_env import handed_on_positions, reads_under_other_names
from pf_core.guards.framework_converted import converted_functions, import_prefixes
from pf_core.guards.framework_rules import RULES, FrameworkRule
from pf_core.guards.sources import python_files, source_lines, unparsed

_PROSE_TOKENS = frozenset({tokenize.STRING, tokenize.COMMENT, tokenize.FSTRING_MIDDLE})
_DEFINITION = re.compile(r"\b(?:def|class)\s+(\w+)")
# pydantic turns a ValueError from a validator into a ValidationError, and argparse turns one
# from a type= converter into a usage error; any other class escapes both as a crash.
_CONVERTED = "ValueError"

_Hit = tuple[FrameworkRule, int, str]  # rule, line, what the line does


@dataclass(frozen=True)
class FrameworkBreach:
    path: str  # relative to the scan root; the key an exemption names
    line: int
    rule: str
    detail: str
    use: str
    why: str


def active_rules(config: FrameworkConfig) -> list[FrameworkRule]:
    """The rules left after ``disable``, carrying any ``replace`` wording."""
    out: list[FrameworkRule] = []
    for rule in RULES:
        if rule.name in config.disable:
            continue
        if rule.name in config.replace:
            use, why = config.replace[rule.name]
            rule = dataclasses.replace(rule, use=use, why=why)
        out.append(rule)
    return out


def code_only(text: str) -> dict[int, str]:
    """Each line with comments and string literals blanked; f-string expressions stay code."""
    lines = dict(enumerate(source_lines(text), start=1))
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError):
        return lines
    for token in tokens:
        if token.type not in _PROSE_TOKENS:
            continue
        (r1, c1), (r2, c2) = token.start, token.end
        for row in range(r1, r2 + 1):
            line = lines.get(row, "")
            start = c1 if row == r1 else 0
            end = c2 if row == r2 else len(line)
            lines[row] = line[:start] + " " * max(0, end - start) + line[end:]
    return lines


def _imported(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [a.name for a in node.names]
    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        return [node.module, *(f"{node.module}.{a.name}" for a in node.names)]
    return []


def _import_hits(tree: ast.AST, rules: list[FrameworkRule]) -> Iterator[_Hit]:
    for node in ast.walk(tree):
        names = _imported(node)
        for rule in rules:
            hit = next((n for n in names if n == rule.name or n.startswith(rule.name + ".")), None)
            if hit is not None:
                yield rule, getattr(node, "lineno", 0), f"imports {hit!r}"


def _raise_hits(
    tree: ast.AST, rules: list[FrameworkRule], converted_defs: set[int]
) -> Iterator[_Hit]:
    """Builtin raises, skipping ``ValueError`` lexically inside a function on ``converted_defs``."""
    by_name = {r.name: r for r in rules}
    stack: list[tuple[ast.AST, bool]] = [(tree, False)]
    while stack:
        node, converted = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            converted = converted or node.lineno in converted_defs
        elif isinstance(node, ast.Raise) and node.exc is not None:
            exc = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
            name = exc.id if isinstance(exc, ast.Name) else None
            if name in by_name and not (converted and name == _CONVERTED):
                yield by_name[name], node.lineno, f"raises {name}"
        stack.extend((child, converted) for child in ast.iter_child_nodes(node))


def _writes_json(node: ast.AST) -> bool:
    """``.write_text``/``.write_bytes`` whose payload came from ``json.dumps``."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr not in ("write_text", "write_bytes"):
        return False
    return any(
        isinstance(inner, ast.Call)
        and isinstance(inner.func, ast.Attribute)
        and inner.func.attr in ("dumps", "dump")
        and isinstance(inner.func.value, ast.Name)
        and inner.func.value.id == "json"
        for arg in node.args
        for inner in ast.walk(arg)
    )


def _json_hits(tree: ast.AST, rules: list[FrameworkRule]) -> Iterator[_Hit]:
    for node in ast.walk(tree):
        if _writes_json(node):
            for rule in rules:
                yield rule, getattr(node, "lineno", 0), "writes JSON with write_text"


def _call_hits(text: str, tree: ast.AST, rules: list[FrameworkRule]) -> Iterator[_Hit]:
    source = source_lines(text)
    code = code_only(text)
    defined = {(n, m.start(1)) for n, line in code.items() for m in _DEFINITION.finditer(line)}
    skip = handed_on_positions(tree, source) | defined
    for rule in rules:
        pattern = rule.pattern
        if pattern is None:
            continue
        lines = {
            n
            for n, line in code.items()
            if any((n, m.start()) not in skip for m in pattern.finditer(line))
        }
        if rule.name == "env-read":
            lines |= reads_under_other_names(tree)
        for n in sorted(lines):
            yield rule, n, source[n - 1].strip()[:60]


def _file_hits(
    text: str, tree: ast.AST, rules: list[FrameworkRule], converted_defs: set[int]
) -> Iterator[_Hit]:
    kind = {k: [r for r in rules if r.kind == k] for k in ("import", "raise", "call", "json-write")}
    yield from _import_hits(tree, kind["import"])
    yield from _raise_hits(tree, kind["raise"], converted_defs)
    yield from _json_hits(tree, kind["json-write"])
    yield from _call_hits(text, tree, kind["call"])


def scan_framework(
    root: str | Path,
    config: FrameworkConfig | None = None,
    *,
    path_prefix: str = "",
    skip_unparsed: bool = False,
) -> list[FrameworkBreach]:
    """Every breach under ``root`` (``*.py``, recursively) — exemptions NOT applied.

    ``path_prefix`` (multi-root scans) is prepended to reported paths. A file that does not
    parse raises its ``SyntaxError``, unless ``skip_unparsed``. Validators and ``type=``
    converters are resolved through each module's imports, across the files under ``root``.
    """
    root = Path(root)
    rules = active_rules(config or FrameworkConfig())
    texts: dict[str, str] = {}
    trees: dict[str, ast.Module] = {}
    for p, text, tree in python_files(root, skip_unparsed=skip_unparsed):
        rel = p.relative_to(root).as_posix()
        texts[rel], trees[rel] = text, tree
    converted = converted_functions(trees, import_prefixes(root))
    out = [
        FrameworkBreach(
            path=f"{path_prefix}{rel}",
            line=line,
            rule=rule.name,
            detail=d,
            use=rule.use,
            why=rule.why,
        )
        for rel, tree in trees.items()
        for rule, line, d in _file_hits(texts[rel], tree, rules, converted[rel])
    ]
    return sorted(out, key=lambda b: (b.path, b.line, b.rule))


def filter_exempt(
    breaches: list[FrameworkBreach], exemptions: tuple[FrameworkExemption, ...]
) -> list[FrameworkBreach]:
    """Drop breaches an exemption names by exact rule and path."""
    keys = {(e.rule, e.path) for e in exemptions}
    return [b for b in breaches if (b.rule, b.path) not in keys]


def stale_exemptions(
    breaches: list[FrameworkBreach], exemptions: tuple[FrameworkExemption, ...]
) -> list[FrameworkExemption]:
    """Exemptions that suppress nothing — the file was fixed, moved, or the rule is off."""
    hit = {(b.rule, b.path) for b in breaches}
    return [e for e in exemptions if (e.rule, e.path) not in hit]


def check_framework(
    root: str | Path, config: FrameworkConfig | None = None
) -> list[FrameworkBreach]:
    """Framework breaches under ``root`` with the config's exemptions applied (public API).

    Raises:
        ConfigurationError: an exemption suppresses nothing — stale, as the gate fails on one.
        SyntaxError: a file under ``root`` does not parse.
    """
    config = config or FrameworkConfig()
    raw = scan_framework(root, config)
    stale = stale_exemptions(raw, config.exempt)
    if stale:
        named = ", ".join(f"{e.rule} in {e.path}" for e in stale)
        raise ConfigurationError(f"stale framework exemption(s), nothing to exempt: {named}")
    return filter_exempt(raw, config.exempt)


def report_framework(roots: list[str], config: FrameworkConfig, *, report: bool) -> bool:
    """Print breaches and stale exemptions; True when they fail the gate (never in report mode)."""
    multi = len(roots) > 1
    raw: list[FrameworkBreach] = []
    unread: set[str] = set()  # an exemption for a file that was not read is not stale
    for r in roots:
        prefix = f"{r.rstrip('/')}/" if multi else ""
        raw += scan_framework(r, config, path_prefix=prefix, skip_unparsed=True)
        unread |= {
            f"{prefix}{Path(e.filename or '').relative_to(r).as_posix()}" for e in unparsed([r])
        }
    breaches = filter_exempt(raw, config.exempt)
    stale = [e for e in stale_exemptions(raw, config.exempt) if e.path not in unread]
    for b in breaches:
        print(f"FRAMEWORK {b.path}:{b.line}: {b.detail} — use {b.use} ({b.why}) [{b.rule}]")
    for e in stale:
        print(f"STALE framework exemption: {e.rule} in {e.path} (nothing to exempt — remove it)")
    if breaches or report:
        counts = ", ".join(f"{k} {n}" for k, n in sorted(Counter(b.rule for b in breaches).items()))
        head = (
            f"{len(breaches)} framework breach(es): {counts}"
            if breaches
            else "0 framework breaches"
        )
        tail = (
            "report only, not failing."
            if report
            else "Fix them, or exempt one with a reason in [[tool.pf_guards.framework.exempt]]."
        )
        sep = "\n" if breaches or stale else ""
        print(f"{sep}{head}. {tail}")
    return bool(breaches or stale) and not report
