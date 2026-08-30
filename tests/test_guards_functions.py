"""pf_core.guards.functions — the opt-in function-length limit: counting, limits, the gate."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pf_core.exceptions import ConfigurationError
from pf_core.guards.config import FunctionLimits, load_guards_config, parse_function_limits
from pf_core.guards.functions import function_lengths, scan_function_lengths
from pf_core.guards.structure import run_cli


def _fn(name: str, lines: int, indent: str = "") -> str:
    """A function spanning exactly ``lines`` lines, ``def`` line included."""
    body = "".join(f"{indent}    x = {i}\n" for i in range(lines - 1))
    return f"{indent}def {name}():\n{body}"


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class TestFunctionLengths:
    def test_a_nested_function_counts_on_its_own_and_inside_its_parent(self) -> None:
        tree = ast.parse("def outer():\n    def inner():\n        return 1\n    return inner\n")
        assert function_lengths(tree) == [("outer", 1, 4), ("outer.inner", 2, 2)]

    def test_a_method_is_named_by_its_class(self) -> None:
        tree = ast.parse("class C:\n    async def go(self):\n        pass\n")
        assert function_lengths(tree) == [("C.go", 2, 2)]

    def test_a_class_inside_a_function_is_named_through_it(self) -> None:
        tree = ast.parse("def f():\n    class K:\n        def m(self):\n            pass\n")
        assert [n for n, _, _ in function_lengths(tree)] == ["f", "f.K.m"]

    def test_decorators_are_not_counted_but_blanks_comments_and_docstrings_are(self) -> None:
        src = '@wrap\n@wrap\ndef f():\n    """Doc."""\n\n    # note\n    return 1\n'
        assert function_lengths(ast.parse(src)) == [("f", 3, 5)]


def _limits(**kw: object) -> FunctionLimits:
    return parse_function_limits({"hard": 10, "soft": 5, **kw}, soft_fraction=0.8)


class TestScan:
    def test_offenders_are_named_with_their_lengths_and_limits(self, tmp_path: Path) -> None:
        _write(tmp_path, "pkg/mod.py", _fn("short", 5) + _fn("warns", 6) + _fn("fails", 11))
        got = [
            (v.path, v.line, v.name, v.lines, v.limit, v.severity)
            for v in scan_function_lengths(tmp_path, _limits())
        ]
        assert got == [
            ("pkg/mod.py", 6, "warns", 6, 5, "soft"),
            ("pkg/mod.py", 12, "fails", 11, 10, "hard"),
        ]

    def test_a_layer_limit_applies_under_app_with_a_fractional_soft(self, tmp_path: Path) -> None:
        _write(tmp_path, "app/cli/run.py", _fn("main", 5))
        _write(tmp_path, "app/services/svc.py", _fn("work", 5))
        found = scan_function_lengths(tmp_path, _limits(layers={"cli": 3}))
        assert [(v.path, v.limit, v.severity) for v in found] == [("app/cli/run.py", 3, "hard")]
        warned = scan_function_lengths(tmp_path, _limits(layers={"cli": 6}))
        assert [(v.path, v.limit, v.severity) for v in warned] == [("app/cli/run.py", 4, "soft")]

    def test_the_longest_prefix_limit_wins(self, tmp_path: Path) -> None:
        _write(tmp_path, "tests/test_a.py", _fn("test_long", 30))
        _write(tmp_path, "tests/slow/test_b.py", _fn("test_long", 30))
        limits = _limits(limits={"tests": 40, "tests/slow": 20})
        found = scan_function_lengths(tmp_path, limits)
        assert [(v.path, v.limit) for v in found] == [("tests/slow/test_b.py", 20)]

    def test_path_prefix_is_reported_and_matched(self, tmp_path: Path) -> None:
        _write(tmp_path, "t.py", _fn("f", 12))
        found = scan_function_lengths(tmp_path, _limits(limits={"tests": 20}), path_prefix="tests/")
        assert found == []
        assert scan_function_lengths(tmp_path, _limits(), path_prefix="tests/")[0].path == (
            "tests/t.py"
        )


class TestConfig:
    def test_absent_means_off(self, tmp_path: Path) -> None:
        p = tmp_path / ".pf-guards.toml"
        p.write_text('[tool.pf_guards]\nroot = "src"\n', encoding="utf-8")
        assert load_guards_config(p).max_function_lines is None

    def test_a_bare_number_is_the_hard_limit(self, tmp_path: Path) -> None:
        p = tmp_path / ".pf-guards.toml"
        p.write_text(
            "[tool.pf_guards]\nsoft_fraction = 0.5\nmax_function_lines = 60\n", encoding="utf-8"
        )
        limits = load_guards_config(p).max_function_lines
        assert limits == FunctionLimits(hard=60, soft=30, soft_fraction=0.5)

    def test_the_table_form(self, tmp_path: Path) -> None:
        p = tmp_path / ".pf-guards.toml"
        p.write_text(
            "[tool.pf_guards.max_function_lines]\nhard = 60\nsoft = 45\n"
            "layers = { cli = 30 }\nlimits = { tests = 100 }\n",
            encoding="utf-8",
        )
        assert load_guards_config(p).max_function_lines == FunctionLimits(
            hard=60, soft=45, soft_fraction=0.8, layers={"cli": 30}, limits={"tests": 100}
        )

    @pytest.mark.parametrize(
        ("raw", "named"),
        [
            ({}, "hard"),
            ({"hard": 0}, "hard"),
            ({"hard": True}, "hard"),
            ({"hard": 60, "soft": -1}, "soft"),
            ({"hard": 60, "layers": {"cli": 0}}, "layers"),
            ({"hard": 60, "limits": ["tests"]}, "limits"),
            ({"hard": 60, "warn": 80}, "warn"),
            ("sixty", "max_function_lines"),
        ],
    )
    def test_a_malformed_value_is_refused_by_name(self, raw: object, named: str) -> None:
        with pytest.raises(ConfigurationError, match=named):
            parse_function_limits(raw, soft_fraction=0.8)


class TestGate:
    @pytest.fixture
    def repo(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        _write(tmp_path, "src/mod.py", _fn("ok", 3) + _fn("Long", 70))
        monkeypatch.chdir(tmp_path)
        return tmp_path

    def _config(self, repo: Path, body: str = "") -> None:
        _write(repo, ".pf-guards.toml", f'[tool.pf_guards]\nroot = "src"\n{body}')

    def test_without_the_key_the_check_does_not_run(self, repo: Path, capsys) -> None:
        self._config(repo)
        assert run_cli([]) == 0
        assert "Long" not in capsys.readouterr().out

    def test_a_function_over_the_hard_limit_fails_the_gate(self, repo: Path, capsys) -> None:
        self._config(repo, "max_function_lines = 60\n")
        assert run_cli([]) == 1
        out = capsys.readouterr().out
        assert "FAIL  mod.py:4 Long: 70 lines (function hard limit 60)" in out
        assert "1 function(s) over the hard limit" in out

    def test_over_the_soft_target_only_warns(self, repo: Path, capsys) -> None:
        self._config(repo, "[tool.pf_guards.max_function_lines]\nhard = 80\nsoft = 60\n")
        assert run_cli([]) == 0
        assert "WARN  mod.py:4 Long: 70 lines (function soft target 60)" in capsys.readouterr().out

    def test_a_malformed_limit_exits_two(self, repo: Path, capsys) -> None:
        self._config(repo, "[tool.pf_guards.max_function_lines]\nsoft = 60\n")
        assert run_cli([]) == 2
        assert "max_function_lines" in capsys.readouterr().out

    def test_the_framework_flag_runs_the_framework_check_alone(self, repo: Path, capsys) -> None:
        self._config(repo, "max_function_lines = 60\n")
        run_cli(["--framework", "--report"])
        assert "Long" not in capsys.readouterr().out

    def test_a_bad_soft_fraction_is_blamed_on_itself(self, repo: Path, capsys) -> None:
        """A derived soft target of zero is the fraction's fault, not a soft the user wrote."""
        self._config(repo, "soft_fraction = 0\nmax_function_lines = 60\n")
        assert run_cli([]) == 2
        out = capsys.readouterr().out
        assert "soft_fraction" in out
        assert "max_function_lines" not in out
