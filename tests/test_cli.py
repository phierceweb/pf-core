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
