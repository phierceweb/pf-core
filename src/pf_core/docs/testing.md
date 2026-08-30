# Testing

pf_core ships two pytest plugins:

- **`pf_core.testing.fixtures`** — auto-discovered via the `pytest11` entry point. Provides `pf_app_client` and `pf_budget_disabled`. No DB dependencies.
- **`pf_core.testing.db_fixtures`** — opt-in. Provides `pf_engine`, `pf_connection`, `pf_tables`. Requires the `[db]` extra (sqlalchemy).

To use the DB fixtures, add this to your `conftest.py`:

```python
pytest_plugins = ["pf_core.testing.db_fixtures"]
```

The split exists so that consumers who don't install `[db]` (e.g. a pipeline consumer using only pf-core's clients) don't pay an unconditional sqlalchemy import on every pytest run.

## Fixtures

### pf_engine

*(Requires `pytest_plugins = ["pf_core.testing.db_fixtures"]` in your conftest.py.)*


File-backed SQLite engine, fresh per test (its own temp file, `NullPool` + WAL). Each test is isolated, and because each connection is independent it is **safe under multi-threaded access** — tests that drive `pf_core.parallel.run_parallel` through the repos work without `SQLITE_MISUSE`. Patches `pf_core.db.connection` so that `get_engine()` and `transaction()` use the test engine.

```python
def test_something(pf_engine):
    # pf_core.db.transaction() now uses the test engine
    with transaction() as conn:
        conn.execute(text("SELECT 1"))
```

Setup also clears the `pf_core.llm.tracking` resolver caches (when the tracking closure is installed), so an id cached against a previous test's database can never leak into this one.

**Backend portability:** set `PF_TEST_DATABASE_URL` to point every test at a disposable Postgres/MySQL server instead of the per-test SQLite file — the same suite then exercises real-dialect SQL (`bin/test-pg`-style gates). Each test still gets a clean slate: `pf_engine` creates a `pf_test_<hex>` schema (Postgres, via `search_path`) or database (MySQL) at setup, points the engine and `DATABASE_URL` at it, and drops it at teardown. The URL's role needs `CREATE` on the database (Postgres) or `CREATE`/`DROP` on databases (MySQL). Server engines get `get_engine()`'s session setup (UTC time zone; MySQL foreign-key checks). Apart from tables an extension installed, a test reads only the tables it creates, through `pf_schema` / `pf_tables`. On Postgres the `search_path` is the test schema then `public`, so extension types and functions installed in `public` (pgvector's `vector`, `uuid_generate_v4()`) still resolve while unqualified `CREATE`s land in the test schema. A table in `public` would resolve too, so setup raises `ConfigurationError` naming each table or view there that no extension owns (pre-run migrations, say); point the URL at an empty database. On MySQL a test never sees the URL's own database. Teardown's `DROP` waits `PF_TEST_LOCK_TIMEOUT_S` seconds (default 10, `0` waits forever) for a lock a test left held, then fails that test's teardown rather than stalling the run. Never point it at real data.

Write DDL that runs on every backend you gate on: prefer `metadata_ddl()` over hand-written SQL, and in raw DDL avoid SQLite-only forms such as `AUTOINCREMENT`, `datetime('now')`, an `INTEGER PRIMARY KEY` inserted without an id, and `TEXT` primary or unique keys (MySQL needs a bounded `VARCHAR`). The raw-DDL examples on this page are SQLite-only. Read inserted ids with a Core `insert(table)` and `result.inserted_primary_key`, not `lastrowid`, which psycopg does not provide.

**Teardown hook:** override `pf_engine_teardown` to run something before `engine.dispose()` while the engine is still alive and patched in — e.g. draining background worker threads (disposing under a live worker can segfault SQLite):

```python
# conftest.py
@pytest.fixture
def pf_engine_teardown():
    from app.jobs import wait_all

    return lambda: wait_all(timeout=10.0)
```

### Framework tables — `framework_ddl()` / `metadata_ddl()`

Tests that exercise pf-core's jobs / tracking / cache / budget subsystems need the framework tables in the test database. **Never hand-copy the CREATE TABLE statements** — they drift when pf-core's schema changes. Splice the generated DDL into `pf_schema`:

```python
# conftest.py
from pf_core.testing.db_fixtures import framework_ddl


@pytest.fixture
def pf_schema():
    return framework_ddl() + PROJECT_DDL  # framework first: project FKs may reference jobs(id)
```

`framework_ddl(dialect=None, if_not_exists=True)` emits every pf-core-owned table (tracking, jobs, cache, budget) in dependency order, then indexes. `dialect=None` compiles for the backend `pf_engine` targets — `PF_TEST_DATABASE_URL`'s, else SQLite — so the same `pf_schema` runs on every gate. MySQL has no `CREATE INDEX IF NOT EXISTS`, so its index statements are unconditional and re-running the DDL against the same database fails there. `metadata_ddl(metadata, ...)` does the same for any SQLAlchemy `MetaData` — use it for a project's own declarative metadata, or with `pf_core.jobs._schema.metadata` in a jobs-only consumer that doesn't install the `[tracking]` closure.

Both take `only={"table", ...}` to restrict output to named tables. Use it when your project's migrations **extend** some framework tables (extra columns) — splice the subset you share verbatim and let your own fixtures create the extended shapes (define the extensions with [`pf_core.db.types`](database.md) so the column types match the framework's on every dialect):

```python
return framework_ddl(only={"llm_models", "jobs", "job_steps", "job_events"}) + PROJECT_DDL
```

### pf_budget_disabled

Neutralizes the cost-budget guard for a test: `check_budget()` no-ops and `project_cost()` returns `0.0` without touching the DB (sets `BUDGET_ENFORCEMENT_DISABLED=1`, read per call — works for every import style). For suites where services are tested with mocked repos, autouse-wrap it:

```python
# conftest.py
@pytest.fixture(autouse=True)
def _no_budget(pf_budget_disabled):
    yield
```

### pf_connection

A `Connection` inside a SAVEPOINT, rolled back after each test. Tests never see each other's data.

```python
def test_insert(pf_tables, pf_connection):
    pf_connection.execute(
        text("INSERT INTO items (name) VALUES (:name)"),
        {"name": "test"},
    )
    row = pf_connection.execute(text("SELECT * FROM items")).fetchone()
    assert row is not None
```

### pf_tables

Creates tables before the test runs. Two ways to provide DDL:

**Via marker** (per-test):

```python
@pytest.mark.pf_tables("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
def test_widgets(pf_tables, pf_connection): ...
```

**Via fixture** (project-wide):

```python
# conftest.py
@pytest.fixture
def pf_schema():
    return [
        "CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)",
        "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT UNIQUE)",
    ]


def test_items(pf_tables, pf_connection): ...
```

A `pf_schema` fixture applies to every test that uses `pf_tables`, whether or not the test names `pf_schema` in its arguments, and whether or not it is `autouse`.

Both can be combined — marker DDL runs after `pf_schema` and **supplements** it. Marker DDL that re-creates a table, index, or view `pf_schema` already creates raises `InvalidInputError` naming the object. To give one module a different shape for a shared table, override `pf_schema` in that module instead:

```python
# test_widgets.py — module-local schema replaces the conftest one
@pytest.fixture
def pf_schema():
    return ["CREATE TABLE items (id INTEGER PRIMARY KEY, other TEXT)"]
```

### pf_app_client

httpx `AsyncClient` bound to your FastAPI app. Requires you to define a `pf_app` fixture:

```python
# conftest.py
@pytest.fixture
def pf_app():
    from app.api import app

    return app


# test_api.py
async def test_health(pf_app_client):
    resp = await pf_app_client.get("/api/health")
    assert resp.status_code == 200
```

## Example: full test setup

```python
# conftest.py
import pytest
from pf_core.db import transaction
from sqlalchemy import text


@pytest.fixture
def pf_schema():
    return [
        "CREATE TABLE sections (id INTEGER PRIMARY KEY, name TEXT NOT NULL)",
        "CREATE TABLE entries (id TEXT PRIMARY KEY, section_id INTEGER REFERENCES sections(id))",
    ]


@pytest.fixture
def pf_app():
    from app import create_app

    return create_app()


# test_entries.py
def test_create_entry(pf_tables, pf_connection):
    pf_connection.execute(text("INSERT INTO sections (id, name) VALUES (1, 'Test')"))
    pf_connection.execute(text("INSERT INTO entries (id, section_id) VALUES ('E001', 1)"))

    row = pf_connection.execute(text("SELECT * FROM entries WHERE id = 'E001'")).fetchone()
    assert row is not None


async def test_api_sections(pf_tables, pf_app_client):
    resp = await pf_app_client.get("/api/sections")
    assert resp.status_code == 200
```

## Hermetic env — `pf_core.testing.env`

Consumer config objects read the environment at **import time**, so pytest fixtures run too late for the base env block. Call these at the top of `conftest.py`, before any app import:

```python
# conftest.py — first lines
from pf_core.testing.env import hermetic_test_env, stub_model_router

hermetic_test_env(extra={"MYAPP_MODE": "1"})
stub_model_router(["summarizer", "classifier"])
```

- `hermetic_test_env()` forces `DATABASE_URL` (default `sqlite://`), blanks `REDIS_URL`, sets `CACHE_CONFIG=off` (+ reload `0`) and `BUDGET_ENFORCEMENT_DISABLED=1`, and deletes provider API keys (`OPENROUTER`/`ANTHROPIC`/`OPENAI`/`BRAVE`) so a misconfigured test can't reach a real provider. Keyword args opt individual pieces out; `extra={...}` sets project-specific vars.
- `stub_model_router(agents, model="test-model", dir=None)` writes a stub router YAML mapping each slug to *model* and points `MODEL_ROUTER_CONFIG` at it (reload TTL `0`) — replaces per-project `model_router_test.yaml` fixture copies and keeps `assert_agents_registered` happy.

## Reset helpers

For integration tests or fixtures that need a clean singleton:

```python
from pf_core.db.connection import reset_engine
from pf_core.db.models import clear_cache
from pf_core.clients.openrouter import reset_client
from pf_core.cache.redis import reset_cache

reset_engine()  # dispose and reset the DB engine singleton
clear_cache()  # clear the model name → ID cache
reset_client()  # reset the OpenRouter client singleton
reset_cache()  # reset the RedisCache singleton
```
