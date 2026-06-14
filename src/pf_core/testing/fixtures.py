"""
Pytest plugin providing pf-core's base testing fixtures.

Auto-discovered by pytest via the ``pytest11`` entry point in pyproject.toml.
Consumer projects get these fixtures by installing pf-core.

This module has no DB dependencies — it's safe for consumers who don't
install the ``[db]`` extra. For DB fixtures, opt in via::

    # In your conftest.py:
    pytest_plugins = ["pf_core.testing.db_fixtures"]

Fixtures
--------
pf_app_client
    httpx ``AsyncClient`` bound to the consumer project's FastAPI app.
    Requires the consumer project to define a ``pf_app`` fixture returning
    their ``FastAPI`` instance.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
async def pf_app_client(request):
    """httpx AsyncClient bound to the consumer's FastAPI app.

    Requires consumer conftest.py to define::

        @pytest.fixture
        def pf_app():
            from app.api import app
            return app

    Then in tests::

        async def test_api(pf_app_client):
            resp = await pf_app_client.get("/api/health")
            assert resp.status_code == 200
    """
    import httpx

    app = request.getfixturevalue("pf_app")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
