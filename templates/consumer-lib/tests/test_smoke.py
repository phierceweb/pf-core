"""Day-1 smoke test: the scaffolded CLI runs."""

from __future__ import annotations

from typer.testing import CliRunner

from __PKG__.cli import app

runner = CliRunner()


def test_hello_runs():
    result = runner.invoke(app, ["hello", "Ada"])
    assert result.exit_code == 0
    assert "hello, Ada" in result.output


def test_help_runs():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
