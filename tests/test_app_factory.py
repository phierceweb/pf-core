"""Tests for pf_core.web.app_factory exception→HTTP mapping."""

import pytest
from fastapi.testclient import TestClient

from pf_core.exceptions import (
    ActionNotAllowedError,
    AppError,
    ConfigurationError,
    FlowException,
    InvalidInputError,
    NotFoundError,
    PreconditionError,
)
from pf_core.web.app_factory import create_app


@pytest.fixture
def client():
    app = create_app(title="Test", log_requests=False)

    @app.get("/not-found")
    async def raise_not_found():
        raise NotFoundError("Course", 42)

    @app.get("/invalid-input")
    async def raise_invalid_input():
        raise InvalidInputError("name is required")

    @app.get("/precondition")
    async def raise_precondition():
        raise PreconditionError("task already complete")

    @app.get("/not-allowed")
    async def raise_not_allowed():
        raise ActionNotAllowedError("section is locked")

    @app.get("/config-error")
    async def raise_config_error():
        raise ConfigurationError("DATABASE_URL not set")

    @app.get("/flow-base")
    async def raise_flow_base():
        raise FlowException("generic domain failure")

    @app.get("/app-error")
    async def raise_app_error():
        raise AppError("something exploded", context={"task_id": 7})

    @app.get("/unhandled")
    async def raise_unhandled():
        raise RuntimeError("unexpected")

    return TestClient(app, raise_server_exceptions=False)


class TestExceptionToHttpMapping:
    """Each domain exception maps to the correct HTTP status code."""

    def test_not_found_returns_404(self, client):
        r = client.get("/not-found")
        assert r.status_code == 404
        assert "Course not found: 42" in r.json()["detail"]

    def test_invalid_input_returns_422(self, client):
        r = client.get("/invalid-input")
        assert r.status_code == 422
        assert "name is required" in r.json()["detail"]

    def test_precondition_returns_409(self, client):
        r = client.get("/precondition")
        assert r.status_code == 409
        assert "task already complete" in r.json()["detail"]

    def test_action_not_allowed_returns_403(self, client):
        r = client.get("/not-allowed")
        assert r.status_code == 403
        assert "section is locked" in r.json()["detail"]

    def test_configuration_error_returns_500(self, client):
        r = client.get("/config-error")
        assert r.status_code == 500
        # Config errors don't leak details to the client
        assert "DATABASE_URL" not in r.json()["detail"]

    def test_flow_base_returns_400(self, client):
        """Unknown FlowException subclasses fall through to 400."""
        r = client.get("/flow-base")
        assert r.status_code == 400
        assert "generic domain failure" in r.json()["detail"]

    def test_app_error_returns_500(self, client):
        r = client.get("/app-error")
        assert r.status_code == 500
        # AppError doesn't leak internal details
        assert "something exploded" not in r.json()["detail"]

    def test_unhandled_exception_returns_500(self, client):
        r = client.get("/unhandled")
        assert r.status_code == 500


class TestHtmlNegotiation:
    """HTML Accept header gets an HTML error page, JSON gets JSON."""

    def test_html_accept_gets_html_page(self, client):
        r = client.get("/not-found", headers={"accept": "text/html"})
        assert r.status_code == 404
        assert "text/html" in r.headers["content-type"]
        assert "Page not found" in r.text

    def test_json_accept_gets_json(self, client):
        r = client.get("/not-found", headers={"accept": "application/json"})
        assert r.status_code == 404
        assert r.json()["detail"] == "Course not found: 42"

    def test_409_html_shows_conflict(self, client):
        r = client.get("/precondition", headers={"accept": "text/html"})
        assert r.status_code == 409
        assert "Conflict" in r.text

    def test_403_html_shows_forbidden(self, client):
        r = client.get("/not-allowed", headers={"accept": "text/html"})
        assert r.status_code == 403
        assert "Forbidden" in r.text
