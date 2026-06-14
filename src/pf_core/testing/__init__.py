"""pf_core.testing — Shared test infrastructure for pf-core consumer projects.

This package provides two pytest plugins:

``pf_core.testing.fixtures`` (auto-loaded)
    Base plugin, registered via the ``pytest11`` entry point in pyproject.toml.
    Provides ``pf_app_client`` (httpx AsyncClient against a FastAPI app).
    No DB dependencies — safe for consumers without the ``[db]`` extra.

``pf_core.testing.db_fixtures`` (opt-in)
    DB plugin. Requires sqlalchemy (the ``[db]`` extra). Consumers who want
    DB fixtures must explicitly opt in by adding to their ``conftest.py``::

        pytest_plugins = ["pf_core.testing.db_fixtures"]

    Provides:
        pf_engine     — File-backed SQLite engine (per-test temp file), reset per test.
        pf_connection — Connection with an active transaction, rolled back after each test.
        pf_tables     — Marker-driven DDL: ``@pytest.mark.pf_tables(...)``.
"""
