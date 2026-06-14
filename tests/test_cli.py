"""Tests for pf_core.cli — CLI framework."""

from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

from pf_core.cli import create_cli, run_cli
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
