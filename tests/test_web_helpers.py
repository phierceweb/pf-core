"""Tests for pf_core.web.helpers."""

import pytest

from pf_core.exceptions import NotFoundError, FlowException
from pf_core.web.helpers import resolve_or_404


class TestResolveOr404:
    def test_returns_value_when_not_none(self):
        result = resolve_or_404({"id": 1, "name": "test"})
        assert result == {"id": 1, "name": "test"}

    def test_returns_truthy_values(self):
        assert resolve_or_404("hello") == "hello"
        assert resolve_or_404(42) == 42
        assert resolve_or_404([1, 2]) == [1, 2]

    def test_returns_falsy_non_none(self):
        assert resolve_or_404(0) == 0
        assert resolve_or_404("") == ""
        assert resolve_or_404([]) == []
        assert resolve_or_404(False) is False

    def test_raises_on_none(self):
        with pytest.raises(NotFoundError, match="record not found"):
            resolve_or_404(None)

    def test_raises_with_custom_entity(self):
        with pytest.raises(NotFoundError, match="Entry not found"):
            resolve_or_404(None, "Entry")

    def test_exception_is_flow_exception(self):
        """NotFoundError is a FlowException, so app_factory maps it to 404."""
        with pytest.raises(FlowException):
            resolve_or_404(None)

    def test_exception_carries_entity(self):
        with pytest.raises(NotFoundError) as exc_info:
            resolve_or_404(None, "Course")
        assert exc_info.value.entity == "Course"
        assert exc_info.value.identifier is None


class TestNotFoundError:
    def test_with_entity_only(self):
        exc = NotFoundError("Course")
        assert str(exc) == "Course not found"
        assert exc.entity == "Course"
        assert exc.identifier is None

    def test_with_entity_and_id(self):
        exc = NotFoundError("Course", 42)
        assert str(exc) == "Course not found: 42"
        assert exc.entity == "Course"
        assert exc.identifier == 42

    def test_is_flow_exception(self):
        assert issubclass(NotFoundError, FlowException)
