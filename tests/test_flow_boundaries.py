"""Every pf-core function that looks at an exception class pydantic can wrap also unwraps it.

pydantic wraps a validator's InvalidInputError in a ValidationError, so a function that catches,
tests for, or collects ``FlowException`` / ``InvalidInputError`` / a validation error must call
``unwrap_flow_exception`` or be listed in ``EXEMPT`` with why it need not. Raising one, or naming
one in an annotation, is not looking at one.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pf_core

_WRAPPABLE = frozenset(
    {
        "FlowException",
        "InvalidInputError",
        "ValidationError",
        "ValidationException",
        "RequestValidationError",
        "WebSocketRequestValidationError",
        "ResponseValidationError",
    }
)
_NO_MODEL = "its try block builds no pydantic model, so nothing it catches can be wrapped"
EXEMPT = {
    ("exceptions.py", "unwrap_flow_exception"): "it is the unwrap",
    ("exceptions.py", "_converted_before"): "part of the unwrap",
    ("llm/tracked.py", "tracked_call"): _NO_MODEL,
    ("budget/snapshot_job.py", "refresh_snapshots"): _NO_MODEL,
    ("utils/dates.py", "try_parse_date"): _NO_MODEL,
}
_SKIPPED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef, ast.Raise)


def _own_nodes(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> Iterator[ast.AST]:
    """``fn``'s body, not a nested function's, a raise, or an annotation."""
    stack: list[ast.AST] = list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(node, _SKIPPED):
            continue
        yield node
        if isinstance(node, ast.AnnAssign):
            stack.extend(n for n in (node.target, node.value) if n is not None)
        else:
            stack.extend(ast.iter_child_nodes(node))


def _sites() -> dict[tuple[str, str], bool]:
    """``(path, function) -> whether it unwraps`` for each function that looks at a class."""
    root = Path(pf_core.__file__).parent
    out: dict[tuple[str, str], bool] = {}
    for path in sorted(root.rglob("*.py")):
        for fn in ast.walk(ast.parse(path.read_bytes())):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            names = {n.id for n in _own_nodes(fn) if isinstance(n, ast.Name)}
            if names & _WRAPPABLE:
                key = (path.relative_to(root).as_posix(), fn.name)
                out[key] = "unwrap_flow_exception" in names
    return out


def test_every_function_looking_at_one_unwraps_or_says_why_not() -> None:
    sites = _sites()
    missing = sorted(key for key, unwraps in sites.items() if not unwraps and key not in EXEMPT)
    assert missing == [], f"call unwrap_flow_exception, or add to EXEMPT with a reason: {missing}"


def test_every_exemption_names_a_function_that_still_looks() -> None:
    assert sorted(set(EXEMPT) - set(_sites())) == []


def test_the_boundaries_that_answer_one_are_found_and_unwrap() -> None:
    sites = _sites()
    for key in [
        ("cli/__init__.py", "run_cli"),
        ("log.py", "log_exception"),
        ("parallel.py", "wrapper"),
        ("llm/router.py", "call_with_fallback"),
        ("jobs/registry.py", "_validate_against_schema"),
        ("llm/validate/_pydantic.py", "validate_shape"),
    ]:
        assert sites.get(key) is True, key
