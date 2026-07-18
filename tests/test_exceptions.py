"""Tests for pf_core.exceptions hierarchy."""

import pytest

from pf_core.exceptions import (
    ActionNotAllowedError,
    AppError,
    ClientError,
    ConfigurationError,
    DataError,
    FlowException,
    InvalidInputError,
    NotFoundError,
    PreconditionError,
    TaskError,
)


class TestFlowExceptionHierarchy:
    """All FlowException subclasses are FlowExceptions but not AppErrors."""

    @pytest.mark.parametrize("cls", [
        InvalidInputError,
        PreconditionError,
        ActionNotAllowedError,
        NotFoundError,
        ConfigurationError,
    ])
    def test_is_flow_exception(self, cls):
        assert issubclass(cls, FlowException)

    @pytest.mark.parametrize("cls", [
        InvalidInputError,
        PreconditionError,
        ActionNotAllowedError,
        NotFoundError,
        ConfigurationError,
    ])
    def test_is_not_app_error(self, cls):
        assert not issubclass(cls, AppError)


class TestAppErrorHierarchy:
    """All AppError subclasses are AppErrors but not FlowExceptions."""

    @pytest.mark.parametrize("cls", [ClientError, DataError, TaskError])
    def test_is_app_error(self, cls):
        assert issubclass(cls, AppError)

    @pytest.mark.parametrize("cls", [ClientError, DataError, TaskError])
    def test_is_not_flow_exception(self, cls):
        assert not issubclass(cls, FlowException)


class TestNotFoundError:
    def test_entity_only(self):
        exc = NotFoundError("Order")
        assert str(exc) == "Order not found"
        assert exc.entity == "Order"
        assert exc.identifier is None

    def test_entity_and_identifier(self):
        exc = NotFoundError("Order", 42)
        assert str(exc) == "Order not found: 42"
        assert exc.entity == "Order"
        assert exc.identifier == 42

    def test_default_entity(self):
        exc = NotFoundError()
        assert str(exc) == "record not found"


class TestAppErrorContext:
    def test_carries_context(self):
        exc = AppError("boom", context={"task_id": 7})
        assert exc.context == {"task_id": 7}

    def test_chains_cause(self):
        original = ValueError("bad")
        exc = AppError("wrapped", cause=original)
        assert exc.__cause__ is original

    def test_default_context(self):
        exc = AppError("simple")
        assert exc.context == {}


class TestTaskError:
    def test_carries_running_log(self):
        exc = TaskError("failed", context={"task_id": 1}, running_log="step 1 ok\nstep 2 fail")
        assert exc.running_log == "step 1 ok\nstep 2 fail"
        assert exc.context == {"task_id": 1}


class TestActionNotAllowedError:
    def test_message(self):
        exc = ActionNotAllowedError("Invoice is locked for editing")
        assert str(exc) == "Invoice is locked for editing"

    def test_is_flow_exception(self):
        assert issubclass(ActionNotAllowedError, FlowException)
