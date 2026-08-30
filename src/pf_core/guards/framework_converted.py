"""Which functions' ``ValueError`` pydantic or argparse converts, resolved module by module.

pydantic turns a ``ValueError`` from a validator into a ``ValidationError``, and argparse turns
one from a ``type=`` converter into a usage error. A validator counts when its decorator, or the
``AfterValidator``-style wrapper handed it, is imported from pydantic. A converter counts when the
``type=`` value, or a function its lambda calls, is defined in or imported into the module that
passes it — a method through its class (``self.parse``, ``Level.parse``) — and an import counts
only when it names a module under the scan root, so ``type=json.loads`` carves out no one's
``loads``.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

_VALIDATORS = frozenset({"field_validator", "model_validator", "validator", "root_validator"})
_WRAPPERS = frozenset({"AfterValidator", "BeforeValidator", "PlainValidator", "WrapValidator"})
_Method = tuple[str, str]  # the class's qualified name, and what the method calls its instance
_Context = tuple[tuple[str, ...], _Method | None]  # enclosing classes, enclosing method


@dataclass(frozen=True)
class _Module:
    path: str
    tree: ast.Module
    bindings: dict[str, str]  # local name -> the dotted name it was imported as


def import_prefixes(root: Path) -> tuple[str, ...]:
    """The dotted names the modules under ``root`` are imported by.

    A package is imported through its package chain (``src/mypkg`` -> ``mypkg``); a directory
    that is not one is either on ``sys.path`` or a namespace package, so both.
    """
    chain: list[str] = []
    here = root.resolve()
    while (here / "__init__.py").is_file():
        chain.insert(0, here.name)
        here = here.parent
    return (".".join(chain),) if chain else ("", root.resolve().name)


def _dotted(prefix: str, path: str) -> str:
    parts = path.removesuffix(".py").split("/")
    return ".".join([p for p in [prefix, *parts[: -1 if parts[-1] == "__init__" else None]] if p])


def _from_base(node: ast.ImportFrom, package: list[str]) -> str | None:
    """The module a ``from`` import reads, absolute; None above the root's package."""
    tail = [node.module] if node.module else []
    if node.level == 0:
        return ".".join(tail)
    up = node.level - 1
    return None if up > len(package) else ".".join([*package[: len(package) - up], *tail])


def _bindings(tree: ast.Module, package: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                head = a.name.split(".")[0]
                out[a.asname or head] = a.name if a.asname else head
        elif isinstance(node, ast.ImportFrom):
            base = _from_base(node, package)
            for a in node.names:
                # A name imported from above the root still shadows a local one; it never resolves.
                target = "<outside>" if base is None else ".".join(filter(None, [base, a.name]))
                out[a.asname or a.name] = target
    return out


def _chain(node: ast.expr) -> list[str] | None:
    """``a.b.c`` as ``["a", "b", "c"]``; None for anything but names and attributes."""
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Attribute):
        base = _chain(node.value)
        return None if base is None else [*base, node.attr]
    return None


def _from_pydantic(node: ast.expr, mod: _Module, names: frozenset[str]) -> bool:
    chain = _chain(node)
    if not chain or chain[0] not in mod.bindings:
        return False
    target = ".".join([mod.bindings[chain[0]], *chain[1:]])
    return target.split(".")[0] == "pydantic" and target.rsplit(".", 1)[-1] in names


def _file_for(target: str, by_dotted: dict[str, str]) -> tuple[str, str] | None:
    """The file ``target`` lives in and its name there, by the longest module that is a file."""
    parts = target.split(".")
    for i in range(len(parts) - 1, 0, -1):
        path = by_dotted.get(".".join(parts[:i]))
        if path is not None:
            return path, ".".join(parts[i:])
    return None


def _refs(
    node: ast.expr, mod: _Module, by_dotted: dict[str, str], method: _Method | None
) -> Iterator[tuple[str, str]]:
    """``(path, qualified name)`` for each function ``node`` hands over, a lambda's calls included."""
    if isinstance(node, ast.Lambda):
        for call in ast.walk(node.body):
            if isinstance(call, ast.Call):
                yield from _refs(call.func, mod, by_dotted, method)
        return
    chain = _chain(node)
    if not chain:
        return
    head, rest = chain[0], chain[1:]
    if method is not None and head == method[1] and rest:
        yield mod.path, ".".join([method[0], *rest])
    elif head in mod.bindings:
        found = _file_for(".".join([mod.bindings[head], *rest]), by_dotted)
        if found is not None:
            yield found
    else:
        yield mod.path, ".".join(chain)


def _method(fn: ast.FunctionDef | ast.AsyncFunctionDef, classes: tuple[str, ...]) -> _Method | None:
    params = [*fn.args.posonlyargs, *fn.args.args]
    static = any(_chain(d) == ["staticmethod"] for d in fn.decorator_list)
    return (".".join(classes), params[0].arg) if classes and params and not static else None


def _walk(tree: ast.Module) -> Iterator[tuple[ast.AST, _Context]]:
    """Every node with the classes around it since the nearest function, and the method it is in.

    A function is yielded with the context it is defined in; classes restart inside it.
    """
    stack: list[tuple[ast.AST, _Context]] = [(tree, ((), None))]
    while stack:
        node, (classes, method) = stack.pop()
        yield node, (classes, method)
        inner: _Context = (classes, method)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            inner = ((), _method(node, classes) or method)
        elif isinstance(node, ast.ClassDef):
            inner = ((*classes, node.name), method)
        stack.extend((child, inner) for child in ast.iter_child_nodes(node))


def _functions(tree: ast.Module) -> Iterator[tuple[ast.FunctionDef | ast.AsyncFunctionDef, str]]:
    """Every function, named ``Class.method`` when it is a method and bare otherwise."""
    for node, (classes, _) in _walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            yield node, ".".join([*classes, node.name])


def _handed_over(mod: _Module) -> Iterator[tuple[ast.expr, _Method | None]]:
    """What this module passes as a ``type=`` converter or to a pydantic validator wrapper."""
    for node, (_, method) in _walk(mod.tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name == "add_argument":
            yield from ((kw.value, method) for kw in node.keywords if kw.arg == "type")
        elif _from_pydantic(func, mod, _WRAPPERS):
            yield from ((arg, method) for arg in node.args[:1])
            yield from ((kw.value, method) for kw in node.keywords if kw.arg == "func")


def converted_functions(
    trees: dict[str, ast.Module], prefixes: Sequence[str] = ("",)
) -> dict[str, set[int]]:
    """Per path (relative to the scan root), the ``def`` lines of functions whose ``ValueError``
    pydantic or argparse converts. A raise lexically inside one of them is converted too.

    ``prefixes`` are the dotted names the root's modules are imported by (``import_prefixes``);
    the first is the one relative imports resolve against.
    """
    by_dotted = {_dotted(prefix, path): path for path in trees for prefix in prefixes}
    home = [p for p in prefixes[0].split(".") if p]
    mods = [
        _Module(path, tree, _bindings(tree, [*home, *path.split("/")[:-1]]))
        for path, tree in trees.items()
    ]
    wanted: set[tuple[str, str]] = set()
    out: dict[str, set[int]] = {path: set() for path in trees}
    for mod in mods:
        for handed, method in _handed_over(mod):
            wanted.update(_refs(handed, mod, by_dotted, method))
        for fn, _ in _functions(mod.tree):
            decorators = (d.func if isinstance(d, ast.Call) else d for d in fn.decorator_list)
            if any(_from_pydantic(d, mod, _VALIDATORS) for d in decorators):
                out[mod.path].add(fn.lineno)
    for mod in mods:
        for fn, name in _functions(mod.tree):
            if (mod.path, name) in wanted:
                out[mod.path].add(fn.lineno)
    return out
