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
    unwrap_flow_exception,
)


class TestFlowExceptionHierarchy:
    """All FlowException subclasses are FlowExceptions but not AppErrors."""

    @pytest.mark.parametrize(
        "cls",
        [
            InvalidInputError,
            PreconditionError,
            ActionNotAllowedError,
            NotFoundError,
            ConfigurationError,
        ],
    )
    def test_is_flow_exception(self, cls):
        assert issubclass(cls, FlowException)

    @pytest.mark.parametrize(
        "cls",
        [
            InvalidInputError,
            PreconditionError,
            ActionNotAllowedError,
            NotFoundError,
            ConfigurationError,
        ],
    )
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


class TestBuiltinBases:
    """InvalidInputError is a ValueError and PreconditionError a RuntimeError, so swapping a
    builtin raise for them keeps every existing catch working."""

    @pytest.mark.parametrize(
        ("cls", "builtin"), [(InvalidInputError, ValueError), (PreconditionError, RuntimeError)]
    )
    def test_subclasses_the_builtin_it_replaces(self, cls, builtin):
        assert issubclass(cls, builtin)
        assert issubclass(cls, FlowException)

    @pytest.mark.parametrize(
        ("cls", "builtin"), [(InvalidInputError, ValueError), (PreconditionError, RuntimeError)]
    )
    def test_flow_exception_precedes_the_builtin_in_the_mro(self, cls, builtin):
        """Handler lookup walks the MRO: a FlowException handler must win over a builtin one."""
        assert cls.__mro__.index(FlowException) < cls.__mro__.index(builtin)

    @pytest.mark.parametrize(
        ("cls", "builtin"), [(InvalidInputError, ValueError), (PreconditionError, RuntimeError)]
    )
    def test_a_subclass_naming_the_builtin_lists_the_pf_core_class_first(self, cls, builtin):
        assert issubclass(type("Ok", (cls, builtin), {}), cls)
        with pytest.raises(TypeError, match="consistent method resolution"):
            type("Refused", (builtin, cls), {})

    def test_an_existing_value_error_catch_still_catches(self):
        with pytest.raises(ValueError, match="bad date"):
            raise InvalidInputError("bad date")

    def test_an_existing_runtime_error_catch_still_catches(self):
        try:
            raise PreconditionError("already done")
        except RuntimeError as e:
            assert isinstance(e, PreconditionError)

    def test_a_pydantic_validator_raising_invalid_input_yields_a_validation_error(self):
        pydantic = pytest.importorskip("pydantic")

        class Model(pydantic.BaseModel):
            name: str

            @pydantic.field_validator("name")
            @classmethod
            def _named(cls, v: str) -> str:
                if not v:
                    raise InvalidInputError("name is required")
                return v

        with pytest.raises(pydantic.ValidationError, match="name is required"):
            Model(name="")

    def test_an_argparse_converter_raising_invalid_input_is_a_usage_error(self, capsys):
        import argparse

        def positive(raw: str) -> int:
            if int(raw) <= 0:
                raise InvalidInputError(f"{raw} is not positive")
            return int(raw)

        parser = argparse.ArgumentParser(prog="t")
        parser.add_argument("--n", type=positive)
        with pytest.raises(SystemExit) as exit_info:
            parser.parse_args(["--n", "-1"])
        assert exit_info.value.code == 2
        assert "invalid positive value" in capsys.readouterr().err


class TestUnwrapFlowException:
    """pydantic wraps an InvalidInputError raised in a validator into a ValidationError, which is
    no FlowException; unwrapping gives back the one that escaped validation before it could."""

    @pytest.fixture
    def window(self):
        pydantic = pytest.importorskip("pydantic")

        class Window(pydantic.BaseModel):
            start: int
            end: int

            @pydantic.field_validator("start", "end")
            @classmethod
            def _non_negative(cls, v: int) -> int:
                if v < 0:
                    raise InvalidInputError(f"{v} is negative")
                return v

        return Window

    def _error(self, model, **fields):
        pydantic = pytest.importorskip("pydantic")
        with pytest.raises(pydantic.ValidationError) as caught:
            model(**fields)
        return caught.value

    def test_a_validation_error_from_one_flow_exception_gives_it_back(self, window):
        flow = unwrap_flow_exception(self._error(window, start=-1, end=2))
        assert isinstance(flow, InvalidInputError)
        assert str(flow) == "-1 is negative"

    def test_several_flow_exceptions_give_the_first_raised(self, window):
        """Before InvalidInputError was a ValueError, the first one raised escaped the model."""
        assert str(unwrap_flow_exception(self._error(window, start=-1, end=-2))) == "-1 is negative"

    def test_ordinary_bad_input_is_left_alone(self, window):
        assert unwrap_flow_exception(self._error(window, start="x", end=2)) is None

    def test_a_mix_gives_back_the_flow_exception(self, window):
        """Before, the InvalidInputError escaped mid-validation whatever else was wrong."""
        assert (
            str(unwrap_flow_exception(self._error(window, start="x", end=-2))) == "-2 is negative"
        )
        assert (
            str(unwrap_flow_exception(self._error(window, start=-1, end="x"))) == "-1 is negative"
        )

    @pytest.mark.parametrize(
        "bases",
        [(FlowException, ValueError), (InvalidInputError, AssertionError), (InvalidInputError,)],
    )
    def test_only_a_class_pydantic_did_not_convert_before_is_given_back(self, bases):
        """A FlowException that is a ValueError or AssertionError on its own was always wrapped."""
        pydantic = pytest.importorskip("pydantic")
        raised = type("Raised", bases, {})

        class Model(pydantic.BaseModel):
            a: int

            @pydantic.field_validator("a")
            @classmethod
            def _a(cls, v: int) -> int:
                raise raised("no")

        flow = unwrap_flow_exception(self._error(Model, a=1))
        assert (flow is not None) is (bases == (InvalidInputError,))

    def test_a_nested_model_and_a_model_built_in_a_validator_give_it_back(self, window):
        pydantic = pytest.importorskip("pydantic")
        inner = window

        class Outer(pydantic.BaseModel):
            n: int
            window: inner

        class Builds(pydantic.BaseModel):
            s: str

            @pydantic.field_validator("s")
            @classmethod
            def _s(cls, v: str) -> str:
                window(start=-3, end=0)
                return v

        nested = self._error(Outer, n="x", window={"start": 1, "end": -4})
        assert str(unwrap_flow_exception(nested)) == "-4 is negative"
        assert str(unwrap_flow_exception(self._error(Builds, s="x"))) == "-3 is negative"

    @pytest.mark.parametrize(
        "name",
        ["RequestValidationError", "WebSocketRequestValidationError", "ResponseValidationError"],
    )
    def test_fastapi_validation_errors_are_unwrapped(self, name):
        exceptions = pytest.importorskip("fastapi.exceptions")
        flow = InvalidInputError("start must be a date")
        errors = [
            {"type": "int_parsing", "loc": ("query", "n"), "msg": "not an int", "input": "x"},
            {"type": "value_error", "loc": ("body", "s"), "msg": "m", "ctx": {"error": flow}},
        ]
        assert unwrap_flow_exception(getattr(exceptions, name)(errors)) is flow
        assert unwrap_flow_exception(getattr(exceptions, name)(errors[:1])) is None

    @pytest.mark.parametrize("exc", [ValueError("x"), InvalidInputError("x"), RuntimeError("x")])
    def test_anything_but_a_validation_error_is_none(self, exc):
        assert unwrap_flow_exception(exc) is None
