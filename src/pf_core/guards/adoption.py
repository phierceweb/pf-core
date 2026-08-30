"""Paste-ready ``.pf-guards.toml`` blocks for adopting the gate on a tree that already breaks it."""

from __future__ import annotations

from pf_core.guards.layering import LayeringViolation


def baseline_block(over: dict[str, int]) -> str:
    """``[tool.pf_guards.baseline]`` grandfathering each file over its hard limit at its size."""
    return "\n".join(
        ["[tool.pf_guards.baseline]", *(f'"{p}" = {n}' for p, n in sorted(over.items()))]
    )


def allowlist_block(violations: list[LayeringViolation]) -> str:
    """``[tool.pf_guards.layering_allowlist]`` naming every current layering violation."""
    by_path: dict[str, set[str]] = {}
    for v in violations:
        by_path.setdefault(v.path, set()).add(v.imported)
    rows = ["[tool.pf_guards.layering_allowlist]"]
    for path, modules in sorted(by_path.items()):
        quoted = ", ".join(f'"{m}"' for m in sorted(modules))
        rows.append(f'"{path}" = [{quoted}]')
    return "\n".join(rows)
