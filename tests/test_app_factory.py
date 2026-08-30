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
            scope_kind="agent",
            scope_value="drafter",
            period="daily",
            limit_value=10.0,
            spent_value=9.5,
            projected_value=1.0,
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


class TestBuiltinHandlersDoNotWin:
    """InvalidInputError is a ValueError and PreconditionError a RuntimeError; a handler an
    app registers for the builtin must not take their 422 / 409 away."""

    @pytest.fixture
    def builtin_client(self):
        from fastapi.responses import JSONResponse

        app = create_app(title="Test", log_requests=False)

        @app.exception_handler(ValueError)
        async def value_error(request, exc):
            return JSONResponse({"detail": "value"}, status_code=400)

        @app.exception_handler(RuntimeError)
        async def runtime_error(request, exc):
            return JSONResponse({"detail": "runtime"}, status_code=503)

        @app.get("/invalid-input")
        async def raise_invalid_input():
            raise InvalidInputError("name is required")

        @app.get("/precondition")
        async def raise_precondition():
            raise PreconditionError("task already complete")

        @app.get("/value")
        async def raise_value():
            raise ValueError("plain")

        return TestClient(app, raise_server_exceptions=False)

    def test_invalid_input_still_returns_422(self, builtin_client):
        assert builtin_client.get("/invalid-input").status_code == 422

    def test_precondition_still_returns_409(self, builtin_client):
        assert builtin_client.get("/precondition").status_code == 409

    def test_a_plain_value_error_reaches_its_own_handler(self, builtin_client):
        assert builtin_client.get("/value").status_code == 400

    def test_a_flow_exception_handler_beats_a_value_error_handler(self):
        """A bare FastAPI app with only the base FlowException handler, registered last."""
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse

        app = FastAPI()

        @app.exception_handler(ValueError)
        async def value_error(request, exc):
            return JSONResponse({"detail": "value"}, status_code=400)

        @app.exception_handler(FlowException)
        async def flow(request, exc):
            return JSONResponse({"detail": "flow"}, status_code=422)

        @app.get("/invalid-input")
        async def raise_invalid_input():
            raise InvalidInputError("name is required")

        r = TestClient(app, raise_server_exceptions=False).get("/invalid-input")
        assert (r.status_code, r.json()["detail"]) == (422, "flow")


class TestFlowExceptionInsideValidation:
    """A model built in a route whose validator raises InvalidInputError: pydantic wraps it in a
    ValidationError, which must still answer 422, as it did before the builtin bases."""

    @pytest.fixture
    def app(self):
        from datetime import date

        from fastapi.responses import JSONResponse
        from pydantic import BaseModel, field_validator

        from pf_core.utils.dates import parse_date

        class Window(BaseModel):
            start: date

            @field_validator("start", mode="before")
            @classmethod
            def _start(cls, v: object) -> date:
                return parse_date(v)  # raises InvalidInputError on "2026-13-45"

        class SpecError(InvalidInputError):
            pass

        class Spec(BaseModel):
            name: str

            @field_validator("name")
            @classmethod
            def _name(cls, v: str) -> str:
                raise SpecError(f"no spec called {v}")

        class Count(BaseModel):
            n: int

        class Both(Count, Window):
            pass

        app = create_app(title="Test", log_requests=False)

        @app.exception_handler(SpecError)
        async def spec_error(request, exc):
            return JSONResponse({"detail": str(exc)}, status_code=418)

        @app.get("/window")
        async def window(start: str):
            return {"start": str(Window(start=start).start)}

        @app.get("/spec")
        async def spec(name: str):
            return {"name": Spec(name=name).name}

        @app.get("/count")
        async def count(n: str):
            return {"n": Count(n=n).n}

        @app.get("/both")
        async def both(n: str, start: str):
            return {"n": Both(n=n, start=start).n}

        return app

    def test_a_bad_date_answers_422(self, app):
        r = TestClient(app, raise_server_exceptions=False).get(
            "/window", params={"start": "2026-13-45"}, headers={"accept": "application/json"}
        )
        assert (r.status_code, r.json()["detail"]) == (
            422,
            "Invalid calendar date: '2026-13-45'",
        )

    def test_the_handler_for_the_raised_class_answers(self, app):
        r = TestClient(app, raise_server_exceptions=False).get("/spec", params={"name": "x"})
        assert (r.status_code, r.json()["detail"]) == (418, "no spec called x")

    def test_a_bad_date_beside_ordinary_bad_input_still_answers_422(self, app):
        """Before, the InvalidInputError escaped validation whatever else was wrong."""
        r = TestClient(app, raise_server_exceptions=False).get(
            "/both",
            params={"n": "x", "start": "2026-13-45"},
            headers={"accept": "application/json"},
        )
        assert (r.status_code, r.json()["detail"]) == (422, "Invalid calendar date: '2026-13-45'")

    def test_ordinary_bad_input_is_untouched(self, app):
        from pydantic import ValidationError

        assert TestClient(app, raise_server_exceptions=False).get("/count?n=x").status_code == 500
        with pytest.raises(ValidationError):
            TestClient(app).get("/count?n=x")
