"""Tests for bin/new-consumer — the consumer scaffold generator.

Generates projects into tmp dirs and asserts they are conformant AND runnable
(the day-1 slice actually executes), so the scaffold can't silently rot.
"""

from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import os
import sys
import tomllib
from pathlib import Path

import pytest

PF_ROOT = Path(__file__).resolve().parent.parent
GENERATOR = PF_ROOT / "bin" / "new-consumer"


def _load_generator():
    # bin/new-consumer is extensionless, so use SourceFileLoader directly.
    loader = importlib.machinery.SourceFileLoader("_pf_new_consumer", str(GENERATOR))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _no_unsubstituted_tokens(root: Path) -> None:
    for f in root.rglob("*"):
        if f.is_file():
            text = f.read_text(encoding="utf-8", errors="ignore")
            assert "__PKG__" not in text, f"unsubstituted __PKG__ in {f}"
            assert "__NAME__" not in text, f"unsubstituted __NAME__ in {f}"
            assert "__EXTRAS__" not in text, f"unsubstituted __EXTRAS__ in {f}"


@pytest.mark.parametrize("layout,extras_token", [("lib", "pf-core[cli]"), ("app", "pf-core[full]")])
def test_scaffold_structure(tmp_path, layout, extras_token):
    gen = _load_generator()
    rc = gen.main(["demo-proj", "--layout", layout, "--dest", str(tmp_path)])
    assert rc == 0
    proj = tmp_path / "demo-proj"

    assert (proj / "pyproject.toml").is_file()
    assert (proj / "bin" / "run").is_file()
    assert (proj / "bin" / "setup").is_file()
    assert os.access(proj / "bin" / "run", os.X_OK), "bin/run must be executable"
    # .ai/rules copied from pf-core
    assert (proj / ".ai" / "rules" / "project-structure.md").is_file()
    assert (proj / ".ai" / "plans" / ".gitkeep").is_file()

    meta = tomllib.loads((proj / "pyproject.toml").read_text())
    assert meta["project"]["name"] == "demo-proj"
    assert any(extras_token in d for d in meta["project"]["dependencies"])

    _no_unsubstituted_tokens(proj)


def test_lib_package_path_substituted(tmp_path):
    gen = _load_generator()
    gen.main(["my-tool", "--layout", "lib", "--dest", str(tmp_path)])
    proj = tmp_path / "my-tool"
    # __PKG__ in the path became my_tool
    assert (proj / "src" / "my_tool" / "cli.py").is_file()
    assert not (proj / "src" / "__PKG__").exists()


def test_refuses_nonempty_dest(tmp_path):
    gen = _load_generator()
    (tmp_path / "taken").mkdir()
    (tmp_path / "taken" / "x").write_text("existing")
    with pytest.raises(SystemExit):
        gen.main(["taken", "--layout", "lib", "--dest", str(tmp_path)])


def test_custom_extras(tmp_path):
    gen = _load_generator()
    gen.main(["x-proj", "--layout", "lib", "--extras", "llm,redis", "--dest", str(tmp_path)])
    meta = tomllib.loads((tmp_path / "x-proj" / "pyproject.toml").read_text())
    assert any("pf-core[llm,redis]" in d for d in meta["project"]["dependencies"])


def test_scaffolded_lib_cli_actually_runs(tmp_path):
    """The day-1 slice executes: generate, import the package, run its CLI."""
    gen = _load_generator()
    gen.main(["runnable-demo", "--layout", "lib", "--dest", str(tmp_path)])
    src = str(tmp_path / "runnable-demo" / "src")
    sys.path.insert(0, src)
    try:
        for m in [k for k in sys.modules if k.startswith("runnable_demo")]:
            del sys.modules[m]
        from typer.testing import CliRunner

        cli = importlib.import_module("runnable_demo.cli")
        result = CliRunner().invoke(cli.app, ["hello", "Ada"])
        assert result.exit_code == 0
        assert "hello, Ada" in result.output
    finally:
        sys.path.remove(src)
        for m in [k for k in sys.modules if k.startswith("runnable_demo")]:
            del sys.modules[m]


def test_scaffolded_app_serves_index(tmp_path):
    """The app day-1 slice boots: generate, import the app, hit the index route."""
    gen = _load_generator()
    gen.main(["webdemo", "--layout", "app", "--dest", str(tmp_path)])
    root = str(tmp_path / "webdemo")
    sys.path.insert(0, root)

    def _purge():
        for m in [k for k in sys.modules if k == "app" or k.startswith("app.")]:
            del sys.modules[m]

    _purge()
    sys.path.remove(root)
    sys.path.insert(0, root)
    try:
        from starlette.testclient import TestClient

        app_mod = importlib.import_module("app")
        resp = TestClient(app_mod.app).get("/")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
    finally:
        sys.path.remove(root)
        _purge()
