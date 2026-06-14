# Code Style

## General

- Python 3.11+ — use modern syntax (type unions with `|`, `match` where clearer).
- No star imports (`from x import *`).
- Imports grouped: stdlib → third-party → pf_core → project. One blank line between groups.
- Use `from __future__ import annotations` in files with forward references.

## File size

Under 300 lines/file (soft WARN), 500 hard FAIL — split past 300 by concern. Enforced by the `pf_core.guards` build gate (pre-commit + CI). Per-layer limits: `project-structure.md`. Gate + baseline mechanics: `docs/guards.md`.

## Naming

- Files: `snake_case.py`. Private modules prefixed with `_` (e.g. `_util.py`).
- Classes: `PascalCase`. Exceptions end with `Error` or `Exception`.
- Functions/methods: `snake_case`. Private prefixed with `_`.
- Constants: `UPPER_SNAKE_CASE`.

## Functions

- Prefer pure functions that take inputs and return outputs.
- Service functions return plain dicts, lists, or primitives — not ORM objects or stateful instances.
- Use keyword-only arguments for functions with more than 2 parameters.

## Type hints

- All public function signatures must have type hints.
- Use `dict`, `list`, `tuple` (lowercase) not `Dict`, `List`, `Tuple`.
- Use `X | None` not `Optional[X]`.

## Docstrings

- Required on: modules, public classes, public functions with non-obvious behavior.
- Not required on: private helpers, obvious getters/setters, test functions.
- Use Google-style docstrings (Args/Returns/Raises sections).

## Error handling

- See `error-handling.md` — never raise bare `Exception` from service code.
- Never swallow exceptions silently (`except Exception: pass`).

## Logging

- Use `pf_core.log.get_logger(__name__)` — never raw `print()` for operational output.
- `print()` is acceptable only in CLI entry points for user-facing output.
