"""Tests for pf_core.web.app_factory exception→HTTP mapping."""

import pytest
from fastapi.testclient import TestClient

from pf_core.budget.check import CostBudgetExceeded
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


def test_error_page_escapes_reflected_message():
    """A reflected exception message must be HTML-escaped in the built-in page."""
    app = create_app(title="Test", log_requests=False)

    @app.get("/xss")
    async def raise_xss():
        raise InvalidInputError("<script>alert(1)</script>")

    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/xss", headers={"accept": "text/html"})
    assert r.status_code == 422
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;" in r.text


@pytest.fixture
def client():
    app = create_app(title="Test", log_requests=False)

    @app.get("/not-found")
    async def raise_not_found():
        raise NotFoundError("Order", 42)

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

    @app.get("/budget-exceeded")
    async def raise_budget_exceeded():
        raise CostBudgetExceeded(
            scope_kind="agent", scope_value="drafter", period="daily",
            limit_usd=10.0, spent_usd=9.5, projected_usd=1.0,
        )

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
        assert "Order not found: 42" in r.json()["detail"]

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

    def test_cost_budget_exceeded_returns_429(self, client):
        """Budget block is a domain response (429), not a 500 — and the
        dedicated handler beats the FlowException catch-all (400)."""
        r = client.get("/budget-exceeded")
        assert r.status_code == 429
        assert "budget exceeded" in r.json()["detail"]

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
        assert r.json()["detail"] == "Order not found: 42"

    def test_409_html_shows_conflict(self, client):
        r = client.get("/precondition", headers={"accept": "text/html"})
        assert r.status_code == 409
        assert "Conflict" in r.text

    def test_403_html_shows_forbidden(self, client):
        r = client.get("/not-allowed", headers={"accept": "text/html"})
        assert r.status_code == 403
        assert "Forbidden" in r.text

    def test_429_html_shows_too_many_requests(self, client):
        r = client.get("/budget-exceeded", headers={"accept": "text/html"})
        assert r.status_code == 429
        assert "Too many requests" in r.text


class TestCors:
    """Starlette echoes the requesting origin once credentials are on, so a
    wildcard origin list hands every site read access to authenticated
    responses. `create_app` refuses that combination."""

    def _app(self, **kwargs):
        return create_app(title="T", log_requests=False, rate_limit=False, **kwargs)

    def _client(self, **kwargs):
        app = self._app(**kwargs)

        @app.get("/x")
        def _x():
            return {"ok": True}

        return TestClient(app)

    @pytest.mark.parametrize("origins", [["*"], ["*", "http://ok.example"], [" * "]])
    def test_wildcard_with_credentials_raises(self, origins):
        with pytest.raises(ConfigurationError, match=r"\*"):
            self._app(cors_origins=origins)

    def test_wildcard_allowed_without_credentials(self):
        client = self._client(cors_origins=["*"], cors_allow_credentials=False)
        r = client.get("/x", headers={"Origin": "https://evil.example"})
        assert r.headers["access-control-allow-origin"] == "*"
        assert "access-control-allow-credentials" not in r.headers

    def test_explicit_origin_keeps_credentials(self):
        client = self._client(cors_origins=["http://ok.example"])
        r = client.get("/x", headers={"Origin": "http://ok.example"})
        assert r.headers["access-control-allow-origin"] == "http://ok.example"
        assert r.headers["access-control-allow-credentials"] == "true"

    def test_disallowed_origin_gets_no_acao(self):
        client = self._client(cors_origins=["http://ok.example"])
        r = client.get("/x", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in r.headers

    def test_preflight_from_disallowed_origin_is_refused(self):
        client = self._client(cors_origins=["http://ok.example"])
        r = client.options(
            "/x",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert "access-control-allow-origin" not in r.headers

    def test_no_cors_origins_adds_no_headers(self):
        client = self._client(cors_origins=None)
        r = client.get("/x", headers={"Origin": "https://evil.example"})
        assert not [h for h in r.headers if h.startswith("access-control-")]
