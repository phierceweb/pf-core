"""pf_core.web._validation_errors — a validation error that wraps a validator's InvalidInputError
is answered as that InvalidInputError; any other is answered by whatever answered it before."""

from typing import Annotated

import pytest
from fastapi import Request, WebSocket
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import AfterValidator, BaseModel, ValidationError
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from pf_core.exceptions import InvalidInputError
from pf_core.web.app_factory import create_app

JSON = {"accept": "application/json"}


def _no_bad(value: str) -> str:
    if value == "bad":
        raise InvalidInputError("start must be a date")
    return value


Start = Annotated[str, AfterValidator(_no_bad)]


class Body(BaseModel):
    start: Start


class Out(BaseModel):
    day: Start


class Count(BaseModel):
    n: int


def _app(**kwargs):
    app = create_app(title="Test", log_requests=False, rate_limit=False, **kwargs)

    @app.post("/body")
    async def body(payload: Body, n: int = 0):
        return {"start": payload.start}

    @app.get("/query")
    async def query(start: Start):
        return {"start": start}

    @app.get("/out", response_model=Out)
    async def out(day: str):
        return {"day": day}

    @app.get("/count")
    async def count(n: str):
        return {"n": Count(n=n).n}

    @app.get("/built")
    async def built(start: str):
        return {"start": Body(start=start).start}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket, start: Start):
        await websocket.accept()
        await websocket.send_text(start)
        await websocket.close()

    return app


def _client(app):
    return TestClient(app, raise_server_exceptions=False)


class TestRequestValidation:
    """FastAPI validates params and bodies itself and raises RequestValidationError."""

    def test_a_body_validator_answers_422_with_its_message(self):
        r = _client(_app()).post("/body", json={"start": "bad"}, headers=JSON)
        assert (r.status_code, r.json()) == (422, {"detail": "start must be a date"})

    def test_an_html_request_gets_the_error_page(self):
        r = _client(_app()).post("/body", json={"start": "bad"}, headers={"accept": "text/html"})
        assert r.status_code == 422
        assert r.headers["content-type"].startswith("text/html")
        assert "start must be a date" in r.text

    def test_a_query_param_validator_answers_422(self):
        r = _client(_app()).get("/query", params={"start": "bad"}, headers=JSON)
        assert (r.status_code, r.json()) == (422, {"detail": "start must be a date"})

    def test_beside_ordinary_bad_input_it_is_still_answered(self):
        r = _client(_app()).post("/body?n=x", json={"start": "bad"}, headers=JSON)
        assert (r.status_code, r.json()) == (422, {"detail": "start must be a date"})

    def test_ordinary_bad_input_keeps_fastapis_answer(self):
        r = _client(_app()).post("/body?n=x", json={"start": "ok"}, headers=JSON)
        assert r.status_code == 422
        assert [e["type"] for e in r.json()["detail"]] == ["int_parsing"]

    def test_a_handler_registered_later_sees_only_ordinary_ones(self):
        app = _app()

        @app.exception_handler(RequestValidationError)
        async def mine(request: Request, exc: RequestValidationError):
            return JSONResponse({"detail": "mine"}, status_code=400)

        client = _client(app)
        assert client.post("/body", json={"start": "bad"}, headers=JSON).status_code == 422
        assert client.post("/body?n=x", json={"start": "ok"}).json() == {"detail": "mine"}

    def test_a_handler_passed_to_fastapi_is_kept_for_ordinary_ones(self):
        async def mine(request: Request, exc: RequestValidationError):
            return JSONResponse({"detail": "mine"}, status_code=400)

        client = _client(_app(exception_handlers={RequestValidationError: mine}))
        assert client.post("/body", json={"start": "bad"}, headers=JSON).status_code == 422
        assert client.post("/body?n=x", json={"start": "ok"}).json() == {"detail": "mine"}


class TestValidationErrorInARoute:
    """A model built inside a route raises pydantic's own ValidationError."""

    def test_a_value_error_handler_still_answers_ordinary_bad_input(self):
        """ValidationError is a ValueError: before, the app's ValueError handler answered it."""
        app = _app()

        @app.exception_handler(ValueError)
        async def bad_value(request: Request, exc: ValueError):
            return JSONResponse({"detail": "bad value"}, status_code=400)

        client = _client(app)
        assert client.get("/count", params={"n": "x"}).json() == {"detail": "bad value"}
        r = client.get("/built", params={"start": "bad"}, headers=JSON)
        assert (r.status_code, r.json()) == (422, {"detail": "start must be a date"})

    def test_a_sync_handler_still_answers(self):
        app = _app()

        @app.exception_handler(ValueError)
        def bad_value(request: Request, exc: ValueError):
            return JSONResponse({"detail": "sync"}, status_code=400)

        assert _client(app).get("/count", params={"n": "x"}).json() == {"detail": "sync"}

    def test_a_validation_error_handler_registered_later_sees_only_ordinary_ones(self):
        app = _app()

        @app.exception_handler(ValidationError)
        async def mine(request: Request, exc: ValidationError):
            return JSONResponse({"detail": "mine"}, status_code=400)

        client = _client(app)
        assert client.get("/built", params={"start": "bad"}).status_code == 422
        assert client.get("/count", params={"n": "x"}).json() == {"detail": "mine"}

    def test_without_a_handler_ordinary_bad_input_is_still_a_500(self):
        assert _client(_app()).get("/count", params={"n": "x"}).status_code == 500
        with pytest.raises(ValidationError):
            TestClient(_app()).get("/count", params={"n": "x"})


class TestResponseValidation:
    def test_a_response_validator_answers_422(self):
        r = _client(_app()).get("/out", params={"day": "bad"}, headers=JSON)
        assert (r.status_code, r.json()) == (422, {"detail": "start must be a date"})

    def test_an_ordinary_response_validation_error_is_still_a_500(self):
        app = _app()

        @app.get("/wrong", response_model=Count)
        async def wrong():
            return {"n": "x"}

        assert _client(app).get("/wrong").status_code == 500
        with pytest.raises(ResponseValidationError):
            TestClient(app).get("/wrong")


class TestWebSocketValidation:
    def test_a_param_validator_is_denied_with_422(self):
        with pytest.raises(WebSocketDenialResponse) as denied:
            with _client(_app()).websocket_connect("/ws?start=bad"):
                pass
        assert denied.value.status_code == 422

    def test_ordinary_bad_input_keeps_fastapis_close(self):
        with pytest.raises(WebSocketDisconnect) as closed:
            with _client(_app()).websocket_connect("/ws"):
                pass
        assert closed.value.code == 1008
