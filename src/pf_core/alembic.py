"""
Alembic migration helper — shared env.py logic for all projects.

Supports SQLite (batch mode), MySQL/MariaDB, and PostgreSQL. Uses pf_core.db
for engine management so migrations share the same connection config as the app.

Usage in a project's ``alembic/env.py``::

    from pf_core.alembic import run_migrations_online

    run_migrations_online()

With SQLite fallback (e.g., a small app on SQLite)::

    from pf_core.alembic import run_migrations_online

    run_migrations_online(fallback_sqlite="app.db")

With explicit metadata (for autogenerate)::

    from myapp.models import Base
    from pf_core.alembic import run_migrations_online

    run_migrations_online(target_metadata=Base.metadata)
"""

from __future__ import annotations

from typing import Any

from alembic import context

from pf_core.db import db_url, get_engine, is_sqlite


def run_migrations_online(
    *,
    fallback_sqlite: str = "",
    target_metadata: Any = None,
    compare_type: bool = False,
) -> None:
    """Run Alembic migrations in online mode.

    Call this from your project's ``alembic/env.py``. Handles:
    - SQLite batch mode (``render_as_batch=True``)
    - MySQL/MariaDB and PostgreSQL standard mode
    - Engine reuse via ``pf_core.db.get_engine()``

    Args:
        fallback_sqlite: Path to SQLite file if DATABASE_URL is not set.
        target_metadata: SQLAlchemy MetaData for autogenerate support.
            Pass ``None`` (default) for raw-SQL migrations.
        compare_type: Whether Alembic should detect column type changes.
    """
    if context.is_offline_mode():
        raise RuntimeError(
            "Offline mode is not supported. Set DATABASE_URL and run in online mode."
        )

    url = db_url(fallback_sqlite=fallback_sqlite)
    sqlite = is_sqlite(url)
    engine = get_engine(url)

    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=compare_type,
            render_as_batch=sqlite,
        )
        with context.begin_transaction():
            context.run_migrations()
