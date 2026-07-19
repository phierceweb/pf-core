# Layering

The codebase uses a strict layered architecture. No layer skipping.

```
Entry points:
  CLI (app/cli/)          Web API (app/api/)
       │                        │
       └──────────┬─────────────┘
                  ↓
     Orchestrator layer (app/orchestrators/)   [optional]
                  ↓
          Service layer (app/services/)
               ↓          ↓
    Repo layer (app/repo/)  Clients (app/clients/)
               ↓
         DB (pf_core.db)
```

**Call direction:** Entry points → Orchestrators → Services → Repo / Clients → DB. No layer may import from a layer above it.

---

## Per-layer ownership

| Layer | Owns | Must never |
|-------|------|-----------|
| **CLI** (`app/cli/`) | Argument parsing; call one orchestrator or service function; print result | Query the DB directly; call LLM APIs; contain business logic |
| **Web API** (`app/api/`) | HTTP routing; call one orchestrator or service function; return JSON or HTML | Query the DB directly; call LLM APIs; contain business logic |
| **Orchestrator** (`app/orchestrators/`) | Multi-step coordination; task/state machine; sequence of service calls | Contain single-domain logic (delegate to services); run SQL or access data directly — `transaction()` only to open a shared connection passed into services (`conn=`) for atomicity |
| **Service** (`app/services/`) | Single-domain business logic; LLM calls for one step; return plain values | Call other orchestrators; manage task state; open raw DB connections |
| **Repo** (`app/repo/`) | All SQLAlchemy reads and writes; return plain dicts/lists | Call LLM APIs; read files; import from service or orchestrator modules |
| **Clients** (`app/clients/`) | Wrap external API calls; handle retries, timeouts, transport errors | Import from services, orchestrators, or repo; contain business logic |
| **DB** (`pf_core.db`) | Engine, transaction manager, connection setup | Contain business logic; call LLM APIs |

---

## Entry points — CLI

**Role:** Parse arguments, call one service or orchestrator function, print result. Nothing else.

**Rules:**
- No SQL. No LLM calls. No business logic.
- One command = one service/orchestrator call.
- Errors from service functions bubble up; the CLI catches and prints them.

---

## Entry points — Web API

**Role:** HTTP routing and response shaping. One endpoint = one service or orchestrator call.

**Rules:**
- No SQL. No LLM calls. No business logic.
- Call `cache.bump_generation()` after writes that change cached data.

---

## Service layer

**Role:** Single-domain logic. Each module owns exactly one concern.

**Rules:**
- Use `pf_core.clients.openrouter` for LLM calls — never construct raw HTTP requests.
- Use repo layer for all data access — never open raw DB connections.
- Never import from orchestrators or entry points.
- Never read `os.environ` — use the project's config object.
- Never `print()` — use `pf_core.log.get_logger(__name__)`.
- Return plain dicts, lists, or primitives.

**For new services**, subclass `pf_core.services.Service` for built-in logging, config injection, and repo access:

```python
from pf_core.services import Service

class ReportService(Service):
    def active_reports(self) -> list[dict]:
        repo = self._repo(ReportRepo)
        return repo.list_active()
```

Existing function-based services don't need to be converted — the rules above apply regardless of style.

---

## Repo layer

**Role:** Pure data access. SQLAlchemy queries only.

**Rules:**
- Always use `with transaction() as conn:` from `pf_core.db`.
- No LLM calls. No file I/O. No business logic.
- Return plain dicts, lists, or ORM objects.

---

## Why this matters

- **Testability:** Service functions can be tested by mocking repo calls without a real database.
- **Resumability:** Incremental DB writes in services/orchestrators mean any step can be re-run.
- **Auditability:** All state lives in the DB; nothing is in-memory only.
- **AI clarity:** An AI agent reading `cli/` or `api/` sees only entry points — business logic is in the service layer where it belongs.
