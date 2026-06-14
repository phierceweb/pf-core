"""Tests for pf_core.web.json — JSON serialization helpers."""

import json
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from pf_core.web.json import json_default, safe_json_response


class TestJsonDefault:
    def test_date(self):
        assert json_default(date(2025, 3, 15)) == "2025-03-15"

    def test_datetime_naive(self):
        dt = datetime(2025, 3, 15, 14, 30, 0)
        assert json_default(dt) == "2025-03-15T14:30:00"

    def test_datetime_utc(self):
        dt = datetime(2025, 3, 15, 14, 30, 0, tzinfo=timezone.utc)
        assert json_default(dt) == "2025-03-15T14:30:00+00:00"

    def test_decimal_integer(self):
        assert json_default(Decimal("42")) == 42
        assert isinstance(json_default(Decimal("42")), int)

    def test_decimal_float(self):
        assert json_default(Decimal("3.14")) == 3.14
        assert isinstance(json_default(Decimal("3.14")), float)

    def test_bytes(self):
        assert json_default(b"hello") == "hello"

    def test_bytes_with_bad_encoding(self):
        result = json_default(b"\xff\xfe")
        assert isinstance(result, str)

    def test_sqlalchemy_row(self):
        """Simulates a SQLAlchemy Row with _mapping attribute."""

        class FakeRow:
            _mapping = {"id": 1, "name": "test"}

        assert json_default(FakeRow()) == {"id": 1, "name": "test"}

    def test_unsupported_type_raises(self):
        with pytest.raises(TypeError, match="set"):
            json_default({1, 2, 3})


class TestSafeJsonResponse:
    def test_simple_dict(self):
        resp = safe_json_response({"key": "value"})
        body = json.loads(resp.body)
        assert body == {"key": "value"}
        assert resp.status_code == 200

    def test_with_dates(self):
        data = {"created": date(2025, 1, 20), "amount": Decimal("99.99")}
        resp = safe_json_response(data)
        body = json.loads(resp.body)
        assert body["created"] == "2025-01-20"
        assert body["amount"] == 99.99

    def test_status_code_kwarg(self):
        resp = safe_json_response({"ok": True}, status_code=201)
        assert resp.status_code == 201

    def test_nested_dates(self):
        data = [{"d": date(2025, 6, 1)}, {"d": date(2025, 7, 1)}]
        resp = safe_json_response(data)
        body = json.loads(resp.body)
        assert body[0]["d"] == "2025-06-01"
        assert body[1]["d"] == "2025-07-01"
