"""Tests for pf_core.cli — CLI framework."""

from unittest.mock import patch

import click
import pytest
import typer
from typer.testing import CliRunner

from pf_core.cli import _ABORT, _USAGE, _exc, _merge, create_cli, run_cli
from pf_core.exceptions import (
    ClientError,
    ConfigurationError,
    InvalidInputError,
)

runner = CliRunner()


class TestCreateCli:
    def test_returns_typer_app(self):
        app = create_cli("test")
        assert isinstance(app, typer.Typer)

    def test_name_and_help(self):
        app = create_cli("myapp", help="My help text")
        assert app.info.name == "myapp"
        assert app.info.help == "My help text"

    def test_verbose_flag_exists(self):
        app = create_cli("test")

        @app.command()
        def hello():
            print("hello")

        result = runner.invoke(app, ["--help"])
        assert "--verbose" in result.output or "-v" in result.output

    @patch("pf_core.cli.setup_logging")
    def test_verbose_calls_setup_logging_debug(self, mock_setup):
        app = create_cli("test")

        @app.command()
        def hello():
            print("hello")

        runner.invoke(app, ["--verbose", "hello"])
        mock_setup.assert_called_with(level="DEBUG")

    @patch("pf_core.cli.setup_logging")
    def test_normal_calls_setup_logging_default(self, mock_setup):
        app = create_cli("test")

        @app.command()
        def hello():
            print("hello")

        runner.invoke(app, ["hello"])
        mock_setup.assert_called_with(level=None)


class TestRunCli:
    def _make_app(self, command_fn):
        """Create a test app with a single command."""
        app = create_cli("test")
        app.command()(command_fn)
        return app

    def test_flow_exception_exits_1(self):
        def fail():
            raise InvalidInputError("bad input")

        app = self._make_app(fail)
        with pytest.raises(SystemExit) as exc_info:
            run_cli(app, args=["fail"])
        assert exc_info.value.code == 1

    def test_app_error_exits_1(self):
        def fail():
            raise ClientError("API failed", context={"model": "gpt-4"})

        app = self._make_app(fail)
        with pytest.raises(SystemExit) as exc_info:
            run_cli(app, args=["fail"])
        assert exc_info.value.code == 1

    def test_configuration_error_exits_1(self):
        def fail():
            raise ConfigurationError("DATABASE_URL not set")

        app = self._make_app(fail)
        with pytest.raises(SystemExit) as exc_info:
            run_cli(app, args=["fail"])
        assert exc_info.value.code == 1

    def test_normal_command_runs(self):
        app = create_cli("test")

        @app.command()
        def hello():
            print("it works")

        result = runner.invoke(app, ["hello"])
        assert "it works" in result.output

    def test_typer_exit_code_propagates(self):
        """typer.Exit(N) must become a real process exit code. With
        standalone_mode=False click RETURNS the code instead of raising, so
        run_cli has to convert the return value — dropping it means every
        consumer error path exits 0 (found live in a consumer project)."""

        def fail():
            raise typer.Exit(4)

        app = self._make_app(fail)
        with pytest.raises(SystemExit) as exc_info:
            run_cli(app, args=["fail"])
        assert exc_info.value.code == 4

    def test_typer_exit_zero_is_success(self):
        def ok():
            raise typer.Exit()  # code 0

        app = self._make_app(ok)
        run_cli(app, args=["ok"])  # must not raise

    def test_truthy_bool_return_is_not_an_exit_code(self):
        """bool subclasses int — a command returning True must not exit 1."""

        def ok():
            return True

        app = self._make_app(ok)
        run_cli(app, args=["ok"])  # must not raise


class TestRunCliUsageErrors:
    """Usage errors must print a short message, not a traceback.

    ``CliRunner`` invokes in standalone mode and never reaches ``run_cli``, so
    these drive ``run_cli`` directly.
    """

    def _make_app(self, command_fn):
        app = create_cli("test")
        app.command()(command_fn)
        return app

    def _run(self, app, args, capsys):
        with pytest.raises(SystemExit) as exc_info:
            run_cli(app, args=args)
        captured = capsys.readouterr()
        return exc_info.value.code, captured.err

    def test_unknown_option_exits_2_without_traceback(self, capsys):
        def run():
            print("should not run")

        code, err = self._run(self._make_app(run), ["--bogus"], capsys)
        assert code == 2
        assert "No such option" in err
        assert "Traceback" not in err
        assert len(err.strip().splitlines()) <= 5

    def test_missing_required_argument_exits_2_without_traceback(self, capsys):
        def run(name: str = typer.Argument(...)):
            print(name)

        code, err = self._run(self._make_app(run), ["run"], capsys)
        assert code == 2
        assert "Traceback" not in err
        assert len(err.strip().splitlines()) <= 5

    def test_bad_parameter_from_command_body_exits_2(self, capsys):
        def run():
            raise typer.BadParameter("count must be non-zero")

        code, err = self._run(self._make_app(run), ["run"], capsys)
        assert code == 2
        assert "count must be non-zero" in err
        assert "Traceback" not in err

    def test_plain_click_exception_uses_its_own_exit_code(self, capsys):
        def run():
            raise click.ClickException("plain failure")

        code, err = self._run(self._make_app(run), ["run"], capsys)
        assert code == 1
        assert "plain failure" in err
        assert "Traceback" not in err

    def test_abort_exits_130(self):
        def run():
            raise typer.Abort()

        with pytest.raises(SystemExit) as exc_info:
            run_cli(self._make_app(run), args=["run"])
        assert exc_info.value.code == 130

    def test_keyboard_interrupt_exits_130(self):
        def run():
            raise KeyboardInterrupt

        with pytest.raises(SystemExit) as exc_info:
            run_cli(self._make_app(run), args=["run"])
        assert exc_info.value.code == 130

    def test_interrupted_banner_is_abort_only(self, capsys):
        """typer.core converts KeyboardInterrupt to Exit(130) before run_cli can
        catch it, so 130 arrives via the int-return path and prints nothing."""

        def kb():
            raise KeyboardInterrupt

        code, err = self._run(self._make_app(kb), ["kb"], capsys)
        assert code == 130
        assert "Interrupted." not in err

        def abort():
            raise typer.Abort()

        code, err = self._run(self._make_app(abort), ["abort"], capsys)
        assert code == 130
        assert "Interrupted." in err

    def test_unrelated_exception_still_propagates(self):
        def run():
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            run_cli(self._make_app(run), args=["run"])

    def _validated(self, value: str):
        from pydantic import BaseModel, field_validator

        class Spec(BaseModel):
            n: int
            name: str = "ok"

            @field_validator("name")
            @classmethod
            def _name(cls, v: str) -> str:
                if v == "bad":
                    raise InvalidInputError("name is reserved")
                return v

        def build():
            n = 1 if value == "bad" else value
            Spec(n=n, name="bad" if value in ("bad", "mixed") else "ok")

        return self._make_app(build)

    def test_a_flow_exception_inside_a_validation_error_exits_1(self, capsys):
        """pydantic wraps it; the boundary still answers the InvalidInputError it was."""
        with pytest.raises(SystemExit) as exit_info:
            run_cli(self._validated("bad"), args=["build"])
        assert exit_info.value.code == 1
        assert "name is reserved" in capsys.readouterr().err

    def test_a_flow_exception_beside_ordinary_bad_input_exits_1(self, capsys):
        """Before the wrap it escaped validation on its own, so it is still what is answered."""
        with pytest.raises(SystemExit) as exit_info:
            run_cli(self._validated("mixed"), args=["build"])
        assert exit_info.value.code == 1
        assert "name is reserved" in capsys.readouterr().err

    def test_an_ordinary_validation_error_still_propagates(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            run_cli(self._validated("x"), args=["build"])


class TestRunCliParserErrors:
    """click turns a ValueError from a ``parser=`` into a usage error showing only the raw
    value; an InvalidInputError is one, and run_cli answers it as the FlowException."""

    def _run(self, parse, capsys):
        app = create_cli("test")

        @app.command()
        def run(count: int = typer.Option(..., parser=parse)):
            print(count)

        with pytest.raises(SystemExit) as exc_info:
            run_cli(app, args=["run", "--count", "5"])
        return exc_info.value.code, capsys.readouterr().err

    def test_its_message_is_printed_and_it_exits_1(self, capsys):
        def parse(value):
            raise InvalidInputError(f"count must be even, got {value}")

        code, err = self._run(parse, capsys)
        assert code == 1
        assert "count must be even, got 5" in err
        assert "Usage" not in err

    def test_one_wrapped_by_a_model_the_parser_builds_is_answered_too(self, capsys):
        from pydantic import BaseModel, field_validator

        class Count(BaseModel):
            n: int

            @field_validator("n")
            @classmethod
            def _even(cls, v: int) -> int:
                raise InvalidInputError(f"count must be even, got {v}")

        code, err = self._run(lambda value: Count(n=value).n, capsys)
        assert code == 1
        assert "count must be even, got 5" in err

    def test_a_plain_value_error_stays_a_usage_error(self, capsys):
        def parse(value):
            raise ValueError("not a count")

        code, err = self._run(parse, capsys)
        assert code == 2
        assert "Invalid value" in err

    def test_a_flow_exception_that_is_a_value_error_itself_stays_a_usage_error(self, capsys):
        class OddCount(InvalidInputError, ValueError):
            pass

        def parse(value):
            raise OddCount("count must be even")

        code, _ = self._run(parse, capsys)
        assert code == 2

    def test_a_bad_parameter_raised_while_handling_one_is_kept(self, capsys):
        app = create_cli("test")

        @app.command()
        def run():
            try:
                raise InvalidInputError("count is odd")
            except InvalidInputError:
                raise typer.BadParameter("count must be even")

        with pytest.raises(SystemExit) as exc_info:
            run_cli(app, args=["run"])
        assert exc_info.value.code == 2
        assert "count must be even" in capsys.readouterr().err


class TestExceptionResolution:
    def test_missing_module_or_attr_resolves_empty(self):
        assert _exc("pf_core_no_such_module", "Abort") == ()
        assert _exc("typer", "NoSuchException") == ()

    def test_duplicate_classes_collapse(self):
        """Pre-vendoring typer re-exported click's classes — same object twice."""
        group = _exc("click.exceptions", "Abort")
        assert _merge(group, group) == group

    def test_installed_typer_and_click_are_both_covered(self):
        assert typer.Abort in _ABORT
        assert click.exceptions.Abort in _ABORT
        assert issubclass(typer.BadParameter, _USAGE)
        assert issubclass(click.exceptions.UsageError, _USAGE)


class TestRunCliErrorMessagesAreData:
    """Messages print through rich, which reads brackets as markup."""

    def _run(self, exc, capsys):
        app = create_cli("test")

        @app.command()
        def fail():
            raise exc

        with pytest.raises(SystemExit):
            run_cli(app, args=["fail"])
        return capsys.readouterr().err

    def test_a_flow_exception_keeps_its_bracketed_example(self, capsys):
        err = self._run(InvalidInputError("give it two numbers [x, y] as fractions"), capsys)
        assert "[x, y]" in err

    def test_an_app_error_keeps_its_bracketed_example(self, capsys):
        """Keep a letter-led run — rich passes `[1, 2, 3]` through and proves nothing."""
        with patch("pf_core.cli.log_exception"):
            err = self._run(ClientError("upstream wanted [x, y] and got none"), capsys)
        assert "[x, y]" in err

    def test_a_tag_like_run_is_printed_not_interpreted(self, capsys):
        err = self._run(InvalidInputError("write [bold] to embolden"), capsys)
        assert "[bold]" in err

    def test_a_closing_tag_does_not_crash_the_error_handler(self, capsys):
        """Unescaped, `[/]` raises inside the handler — a traceback instead of the error."""
        err = self._run(InvalidInputError("use [/] to close a tag"), capsys)
        assert "[/]" in err
