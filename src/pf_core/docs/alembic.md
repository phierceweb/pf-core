# Alembic Migrations

Shared Alembic migration runner that reduces each project's `alembic/env.py` to a few lines.

## Usage

In your project's `alembic/env.py`:

```python
from pf_core.alembic import run_migrations_online

run_migrations_online()
```

That's the entire file. The runner handles engine creation, SQLite batch mode, and transaction management.

## Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `fallback_sqlite` | `str` | `""` | SQLite file path if `DATABASE_URL` is not set |
| `target_metadata` | `MetaData` | `None` | SQLAlchemy MetaData for autogenerate support |
| `compare_type` | `bool` | `False` | Detect column type changes during autogenerate |

## Examples

### MySQL project

```python
from pf_core.alembic import run_migrations_online

run_migrations_online()
```

Reads `DATABASE_URL` from environment. Raises `ConfigurationError` if not set.

### SQLite project

```python
from pf_core.alembic import run_migrations_online

run_migrations_online(fallback_sqlite="app.db")
```

Uses `DATABASE_URL` if set, otherwise falls back to `sqlite:///app.db`.

### PostgreSQL project

```python
from pf_core.alembic import run_migrations_online

run_migrations_online()
```

Identical to the MySQL setup. `render_as_batch` is auto-disabled (only SQLite needs it). Requires `pf-core[postgres]` to be installed for the psycopg driver.

### With autogenerate

```python
from myapp.models import Base
from pf_core.alembic import run_migrations_online

run_migrations_online(target_metadata=Base.metadata, compare_type=True)
```

## What it handles

- **Engine reuse**: Uses `pf_core.db.get_engine()` so migrations share the same engine config as the app
- **SQLite batch mode**: Automatically enables `render_as_batch=True` for SQLite (required for ALTER TABLE operations)
- **Transaction management**: Wraps migration execution in a transaction
- **Offline mode**: Raises `RuntimeError` (not supported — run in online mode with a live database)
