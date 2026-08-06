# Scope

pf-core is infrastructure: the mechanisms every consumer would otherwise hand-roll. A piece of code earns its place here when **two or more independent projects** need it; until a second consumer wants it, it stays in the project that has it.

## What belongs here

- Reusable mechanisms — logging, config/env resolution, exceptions, DB access, LLM clients and run tracking, jobs, caching, guards.
- **Generic, mountable entry points** — routers a consumer mounts (`pf_core.web.llm_admin`, `pf_core.web.jobs_admin`, `pf_core.web.health`) and framework CLIs it runs (`pf-guards`, `pf-doctor`, `pf-jobs`, `pf-setup`). These operate on framework-owned tables and framework-owned rules only; they are wiring, not application surface.
- Scaffolding and templates for consumer projects.

## What must NEVER be here

- Business logic specific to any project
- Domain models or schemas
- Project-specific configuration values
- An entry point that knows about one consumer's tables, routes, or workflow

The test is ownership, not shape: a route or command is fine when everything it touches is owned by pf-core. If it only makes sense in the context of one consumer project, it belongs in that project.
