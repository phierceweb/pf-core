# Structural Guards (Build Gate)

`pf_core.guards` turns documented structural rules into a **build gate** — a check that fails pre-commit and CI when a rule is broken, so neither a human nor an AI agent can land a violation. Foundation module, stdlib-only (`ast`, `pathlib`, `json`, `argparse`); no extra required.

Phase A ships the **file-size gate** (proven on pf-core itself) and a **layering checker** (built for consumer four-layer apps; pf-core has no such layers, so it isn't wired here).

## File-size gate

Flags Python files over a line limit, matching `code-style.md` (300 soft / 500 hard):

- **Hard limit (default 500)** → `FAIL`, exit 1. Blocks the commit / CI.
- **Soft target (default 300)** → `WARN`, exit 0. Self-reports without blocking — soft targets stay soft.

```bash
python -m pf_core.guards --root src/pf_core --baseline .ai/guards/file_size_baseline.json
# WARN  budget/check.py: 369 lines (soft target 300)
# (exit 0 — no hard violations outside the baseline)
```

CLI flags: `--root` (default `src`), `--hard` (500), `--soft` (300), `--baseline` (path to JSON). Installed as the `pf-guards` console script.

## Baseline (adopting the gate on a dirty tree)

A gate that requires a green tree on day one can't be adopted by a repo that already has violations. The **baseline** grandfathers known offenders: a JSON map of `path → recorded line count`.

```json
{ "jobs/repo.py": 775, "utils/urls.py": 518 }
```

- A baselined file at or **below** its recorded count → suppressed (no failure).
- A baselined file **grown beyond** its recorded count → reported as a hard FAIL. The ratchet only tightens.
- A **new** file over the hard limit (not in the baseline) → reported.

**Updating the baseline:**
- When you *split* a baselined file below 500, **remove** its entry (don't leave dead grandfathering).
- When a baselined file legitimately must grow, **split it** — do not bump the number. Bumping defeats the ratchet.

pf-core's own baseline (`.ai/guards/file_size_baseline.json`) holds the 4 files that were over 500 when the gate was adopted; burning them down is tracked separately.

## Layering checker (consumer apps)

`check_layering(root)` flags imports that violate the four-layer call direction from `layering.md`:

```
cli / api → orchestrators → services → repo / clients → db
```

A violation is an import that targets a **higher** layer (e.g. `app/repo/x.py` importing `app.services.*`), or an **orchestrator importing `pf_core.db`** (opening `transaction()`) directly. Layer is inferred from the `app/<layer>/` path segment; files outside that structure are ignored — which is why pf-core itself (a library, not a four-layer app) isn't checked. It is wired into consumers during the gates rollout.

## How it's wired

- **pre-commit** (`.pre-commit-config.yaml`) — runs the file-size gate + ruff on every commit. Run `pre-commit install` once per clone (from the project venv). Entries point at `.venv/bin/...` because pre-commit's `system` hooks don't inherit the venv PATH.
- **CI** (`.github/workflows/guards.yml`) — the unskippable backstop: `pip install -e .` then the same gate + ruff on push/PR.

## Consumer distribution (rollout, not Phase A)

Consumers will inherit the gate via the gates rollout. The intended mechanism — a shared `.pre-commit-hooks.yaml` published by pf-core so a consumer references `repo: <pf-core> / id: pf-guards-file-size` — is finalized in that phase; for now each repo keeps a local config pointing at its own `.venv` and `src` root.
