# Anti-Patterns

Concrete examples of what NOT to do, each paired with the focused alternative. Each names the rule it violates; see that rule for the full standard.

---

## No `transaction()` in orchestrators or entry points

Only services and repos own DB access. If an orchestrator needs data, it calls a service, which calls a repo. (See `layering.md`.)

```python
# WRONG — orchestrator touching the DB directly
from pf_core.db import transaction

def run_export():
    with transaction() as conn:
        entries = conn.execute(text("SELECT ..."))

# RIGHT — orchestrator delegates to a service
def run_export():
    entries = entry_service.get_entries_for_export()
```

---

## No god-module `_util.py` files

`_util.py` is for thin shared helpers with a deliberately low size budget (`UTIL_LIMIT`, enforced by the `pf_core.guards` gate). If it grows past that, business logic has leaked in. Split by subdomain.

```
# WRONG — a 480-line _util.py: request parsing, data queries, and domain
# resolvers all piled into one file
app/api/_util.py  (480 lines)

# RIGHT — split by concern
app/api/_util.py        (resolvers, request helpers — 80 lines)
app/api/_search.py      (listing / search queries — 120 lines)
app/api/_status.py      (status lookups — 90 lines)
```

---

## No business logic in utility files

If a function makes decisions, transforms domain data, or coordinates multiple operations, it belongs in a service — not in `_util.py`, `_helpers.py`, or `_common.py`.

---

## No duplicate exception hierarchies

One `errors.py` per project, in `app/errors.py`. Don't create a second one in `services/errors.py`. (See `error-handling.md`.)

---

## No copy-pasting between files

If two services need the same logic, extract it to a shared module in the appropriate layer. Don't duplicate.

---

## No raw HTTP requests for LLM calls

Use `pf_core.clients.openrouter` (or the model router) — it handles timeouts, retries, provider routing, and usage tracking.

---

## No raw SQL for dialect-specific behavior

Use SQLAlchemy expression constructs for database independence. Never write dialect-detection code or raw SQL that only works on one database.

```python
# WRONG — dialect detection, raw SQL timestamp
def _now_expr(conn):
    if conn.dialect.name == "mysql":
        return text("NOW(6)")
    return text("strftime('%Y-%m-%dT%H:%M:%SZ','now')")

# WRONG — Python-side timestamp in SQL context
from pf_core.db.helpers import now_iso
stmt = t.update().values(deleted_at=now_iso())

# RIGHT — SQLAlchemy handles dialect translation
from sqlalchemy import func, table, column
t = table("entries", column("id"), column("deleted_at"))
stmt = t.update().where(t.c.id == id_value).values(deleted_at=func.now())
```

---

## Also forbidden — full standard in the cross-referenced rule

- `os.environ` reads or hardcoded config in services / framework APIs → `config-driven.md` (services receive config; framework functions read env internally).
- `print()` for operational output in services → `logging.md` (use `pf_core.log`).
- Files over the size limits ("monster files") → `project-structure.md` (split by concern; enforced by the `pf_core.guards` gate).
