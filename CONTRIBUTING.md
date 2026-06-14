# Contributing to pf-core

Thanks for your interest. pf-core is a dependency-light Python foundation with
opt-in extras; contributions that keep it lean, well-tested, and documented are
welcome.

## Scope — read this first

pf-core is **infrastructure only**. It must never contain business logic, domain
models, route handlers, CLI commands, or project-specific configuration —
anything that only makes sense inside one application belongs in that
application, not here.

A feature earns its place in pf-core when **two or more independent projects**
need it. A pattern that lives in a single project should stay there until a
second consumer wants it. See [`.ai/rules/scope.md`](.ai/rules/scope.md).

## Development setup

Python 3.11+ is required.

```bash
git clone https://github.com/phierceweb/pf-core
cd pf-core
python -m venv .venv && source .venv/bin/activate
# Everything, so the full test suite runs:
pip install -e ".[full,articles,anthropic,image-phash,dev]"
pre-commit install
```

For lighter work, install only the extras you're touching — the base
`pip install -e ".[dev]"` is enough for foundation-only changes. See
[`docs/INSTALLATION.md`](src/pf_core/docs/INSTALLATION.md) for the extras matrix.

## Before you open a pull request

These three checks run in CI and as pre-commit hooks — run them locally first:

```bash
pytest                                  # full suite, must be green
ruff check src tests                    # lint
python -m pf_core.guards --root src/pf_core --baseline .ai/guards/file_size_baseline.json
```

And hold the change to these standards:

- **Tests travel with code.** New behavior needs tests; a bug fix needs a
  regression test that fails before your change and passes after.
- **Docs travel with code.** A change to a module's public API is incomplete
  without the matching `docs/*.md` update — see
  [`.ai/rules/docs-sync.md`](.ai/rules/docs-sync.md).
- **File-size gate.** Python files over 500 lines fail the build; over 300 warn.
  Split by concern instead of growing a monolith — see
  [`.ai/rules/code-style.md`](.ai/rules/code-style.md).
- **Layering.** Respect the layered architecture (repository → service →
  orchestrator → entry point); no layer imports from a layer above it. See
  [`.ai/rules/layering.md`](.ai/rules/layering.md).

## Coding conventions

The full set lives in [`.ai/rules/`](.ai/rules/). The essentials:

- Modern Python 3.11+ syntax — `X | None`, lowercase `dict`/`list`/`tuple`.
- Type hints on every public signature; Google-style docstrings on public APIs.
- Structured logging via `pf_core.log.get_logger(__name__)` — never bare `print`
  outside CLI entry points.
- Raise from the `pf_core.exceptions` hierarchy — never a bare `Exception`.

## Versioning & deprecations

Stability lives in **tags**. `main` may contain unreleased work between version tags — pin to a tagged release (or a published version) for production use.

- **Pre-1.0** (now): a minor bump (`0.X.0`) may include breaking changes, always called out in `CHANGELOG.md`; a patch bump (`0.0.X`) is fixes only.
- **From 1.0**: semantic versioning — patch = fixes, minor = additive and backward-compatible, major = breaking. Anything documented under `docs/` is the stable surface that contract covers.

A deprecated API keeps working and emits a `DeprecationWarning` naming its replacement, for at least one minor release (pre-1.0) or until the next major (post-1.0) before removal. Current deprecations:

- `pf_core.clients.routing.get_routed_client(use_claude_code)` → use `pf_core.llm.router.resolve_agent` or `get_client_for_backend`.
- `pf_core.llm.tracking.track_run()` without an explicit `provider=` → pass the backend (e.g. `resolve_agent(...).backend`) or `provider=None`.

## Questions

Open an issue for bugs and feature requests. For anything security-sensitive,
follow [`SECURITY.md`](SECURITY.md) instead of filing a public issue.
