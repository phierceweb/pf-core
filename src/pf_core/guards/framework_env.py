"""The env-read rule's AST half: reads through another name, and the environment handed on.

The rule's regex matches the ``os.environ`` / ``os.getenv`` spelling. A module can also bind
``os`` to another name (``import os as o``) or import the environment itself (``from os import
environ, getenv``); every use of those names is a read too. The one shape that is not a read is
the whole environment unpacked into a dict literal passed as a call's ``env=``: that hands it to
a child process without reading a setting.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass


@dataclass(frozen=True)
class _Names:
    modules: frozenset[str]  # names bound to os, "os" itself included
    environ: frozenset[str]  # names bound to os.environ
    getenv: frozenset[str]  # names bound to os.getenv


def _names(tree: ast.AST) -> _Names:
    modules: set[str] = {"os"}
    environ: set[str] = set()
    getenv: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.asname or a.name for a in node.names if a.name == "os")
        elif isinstance(node, ast.ImportFrom) and node.module == "os" and node.level == 0:
            environ.update(a.asname or a.name for a in node.names if a.name == "environ")
            getenv.update(a.asname or a.name for a in node.names if a.name == "getenv")
    return _Names(frozenset(modules), frozenset(environ), frozenset(getenv))


def _is_environ(node: ast.expr, names: _Names) -> bool:
    if isinstance(node, ast.Name):
        return node.id in names.environ
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id in names.modules
    )


def _handed_on(tree: ast.AST, names: _Names) -> list[ast.expr]:
    """Each ``**environ`` in a dict literal passed as a call's ``env=``."""
    return [
        value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "env" and isinstance(kw.value, ast.Dict)
        for key, value in zip(kw.value.keys, kw.value.values, strict=True)
        if key is None and _is_environ(value, names)
    ]


def handed_on_positions(tree: ast.AST, source: list[str]) -> set[tuple[int, int]]:
    """``(line, column)`` of each ``os.environ`` handed on, for the regex's matches to skip.

    ``col_offset`` counts UTF-8 bytes; a regex match counts characters.
    """
    out: set[tuple[int, int]] = set()
    for value in _handed_on(tree, _names(tree)):
        line = source[value.lineno - 1].encode("utf-8")
        out.add((value.lineno, len(line[: value.col_offset].decode("utf-8", "replace"))))
    return out


def reads_under_other_names(tree: ast.AST) -> set[int]:
    """Lines reading the environment through a name other than ``os``, not handing it on."""
    names = _names(tree)
    skip = {id(v) for v in _handed_on(tree, names)}
    other_modules = names.modules - {"os"}
    out: set[int] = set()
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id in other_modules and node.attr in ("environ", "getenv"):
                out.add(node.lineno)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in names.environ | names.getenv:
                out.add(node.lineno)
    return out
