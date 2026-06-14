# Testing

pf_core ships two pytest plugins:

- **`pf_core.testing.fixtures`** — auto-discovered via the `pytest11` entry point. Provides `pf_app_client` only. No DB dependencies.
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
@pytest.mark.pf_tables(
    "CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"
)
def test_items(pf_tables, pf_connection):
    ...
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

def test_items(pf_tables, pf_connection):
    ...
```

Both can be combined — marker DDL runs after `pf_schema`.

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

    row = pf_connection.execute(
        text("SELECT * FROM entries WHERE id = 'E001'")
    ).fetchone()
    assert row is not None

async def test_api_sections(pf_tables, pf_app_client):
    resp = await pf_app_client.get("/api/sections")
    assert resp.status_code == 200
```

## Reset helpers

For integration tests or fixtures that need a clean singleton:

```python
from pf_core.db.connection import reset_engine
from pf_core.db.models import clear_cache
from pf_core.clients.openrouter import reset_client
from pf_core.cache.redis import reset_cache

reset_engine()     # dispose and reset the DB engine singleton
clear_cache()      # clear the model name → ID cache
reset_client()     # reset the OpenRouter client singleton
reset_cache()      # reset the RedisCache singleton
```
