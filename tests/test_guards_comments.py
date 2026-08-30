"""pf_core.guards.comments — the opt-in comment budget: counting prose, the limits, the gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from pf_core.exceptions import ConfigurationError
from pf_core.guards.comments import (
    CommentBudget,
    parse_comment_budget,
    prose_and_code,
    scan_comment_budget,
)
from pf_core.guards.config import load_guards_config
from pf_core.guards.structure import run_cli


def _module(prose: int, code: int) -> str:
    """``prose`` comment lines, then ``code`` lines of code."""
    return "".join(f"# note {i}\n" for i in range(prose)) + "".join(
        f"x{i} = {i}\n" for i in range(code)
    )


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class TestProseAndCode:
    def test_comments_and_every_docstring_line_are_prose(self) -> None:
        src = (
            '"""Module.\n\nMore."""\n'
            "# a comment\n"
            "class C:\n"
            '    """Class."""\n'
            "    def m(self):\n"
            '        """Method."""\n'
            "        return 1\n"
        )
        assert prose_and_code(src) == (6, 3)

    def test_blank_lines_are_neither(self) -> None:
        assert prose_and_code("x = 1\n\n\n# c\n\ny = 2\n") == (1, 2)

    def test_a_line_with_a_trailing_comment_counts_as_prose(self) -> None:
        assert prose_and_code("x = 1  # why\ny = 2\n") == (1, 1)

    def test_a_string_that_is_not_a_docstring_is_code(self) -> None:
        assert prose_and_code('x = 1\nMSG = """\nhello\n"""\n') == (0, 4)

    @pytest.mark.parametrize("splitter", ["\x0c", "\u2028"], ids=["ff", "ls"])
    def test_a_character_python_does_not_end_a_line_on_keeps_rows_aligned(
        self, splitter: str
    ) -> None:
        src = f'SEP = "{splitter}"\n# a {splitter} note\ny = 2\n'
        assert prose_and_code(src) == (1, 2)


class TestScan:
    def test_a_module_over_the_file_ratio_is_named(self, tmp_path: Path) -> None:
        _write(tmp_path, "pkg/wordy.py", _module(20, 30))
        _write(tmp_path, "pkg/plain.py", _module(5, 30))
        found = scan_comment_budget([tmp_path], CommentBudget(file=0.5, total=1.0))
        assert [(v.scope, v.path, v.prose, v.code, v.limit) for v in found] == [
            ("module", "pkg/wordy.py", 20, 30, 0.5)
        ]

    def test_a_module_under_the_code_floor_is_not_judged(self, tmp_path: Path) -> None:
        _write(tmp_path, "tiny.py", _module(20, 24))
        assert scan_comment_budget([tmp_path], CommentBudget(file=0.1, total=1.0)) == []

    def test_the_total_spans_every_root_and_ignores_the_floor(self, tmp_path: Path) -> None:
        _write(tmp_path, "a/one.py", _module(3, 10))
        _write(tmp_path, "b/two.py", _module(3, 10))
        found = scan_comment_budget(
            [tmp_path / "a", tmp_path / "b"], CommentBudget(file=1.0, total=0.25)
        )
        assert [(v.scope, v.prose, v.code) for v in found] == [("total", 6, 20)]

    def test_total_root_limits_the_total_but_not_the_modules(self, tmp_path: Path) -> None:
        """Per-module limits everywhere, the total over the package: the prototype's shape."""
        _write(tmp_path, "src/pkg/one.py", _module(1, 30))
        _write(tmp_path, "tests/test_one.py", _module(20, 30))
        roots = [str(tmp_path / "src/pkg"), str(tmp_path / "tests")]
        package_only = CommentBudget(file=0.5, total=0.22, total_root=(roots[0],))
        found = scan_comment_budget(roots, package_only)
        assert [(v.scope, v.path) for v in found] == [("module", f"{roots[1]}/test_one.py")]
        everything = scan_comment_budget(roots, CommentBudget(file=0.5, total=0.22))
        assert [v.scope for v in everything] == ["module", "total"]

    def test_total_root_must_be_a_scanned_root(self, tmp_path: Path) -> None:
        _write(tmp_path, "src/one.py", _module(0, 30))
        with pytest.raises(ConfigurationError, match="total_root"):
            scan_comment_budget([tmp_path / "src"], CommentBudget(total_root=("lib",)))

    def test_multi_root_paths_carry_their_root(self, tmp_path: Path) -> None:
        _write(tmp_path, "a/one.py", _module(30, 30))
        _write(tmp_path, "b/two.py", _module(0, 30))
        found = scan_comment_budget(
            [str(tmp_path / "a"), str(tmp_path / "b")], CommentBudget(file=0.5, total=1.0)
        )
        assert [v.path for v in found] == [f"{tmp_path / 'a'}/one.py"]


class TestConfig:
    def test_absent_means_off(self, tmp_path: Path) -> None:
        p = tmp_path / ".pf-guards.toml"
        p.write_text('[tool.pf_guards]\nroot = "src"\n', encoding="utf-8")
        assert load_guards_config(p).comment_budget is None

    def test_an_empty_table_turns_it_on_with_the_defaults(self, tmp_path: Path) -> None:
        p = tmp_path / ".pf-guards.toml"
        p.write_text("[tool.pf_guards.comment_budget]\n", encoding="utf-8")
        assert load_guards_config(p).comment_budget == CommentBudget()

    def test_every_key(self) -> None:
        got = parse_comment_budget({"file": 0.5, "total": 0.3, "min_code_lines": 10})
        assert got == CommentBudget(file=0.5, total=0.3, min_code_lines=10)

    @pytest.mark.parametrize(
        ("raw", "roots"), [("src/pkg", ("src/pkg",)), (["src/a", "src/b"], ("src/a", "src/b"))]
    )
    def test_total_root_takes_a_path_or_a_list(self, raw: object, roots: tuple) -> None:
        assert parse_comment_budget({"total_root": raw}).total_root == roots

    @pytest.mark.parametrize(
        ("raw", "named"),
        [
            ({"file": 0}, "file"),
            ({"total": -0.1}, "total"),
            ({"total": "0.2"}, "total"),
            ({"min_code_lines": 0}, "min_code_lines"),
            ({"min_code_lines": 2.5}, "min_code_lines"),
            ({"ratio": 0.2}, "ratio"),
            ({"total_root": 3}, "total_root"),
            ({"total_root": ["src", 3]}, "total_root"),
            ({"total_root": []}, "total_root"),
            (0.22, "table"),
        ],
    )
    def test_a_malformed_value_is_refused_by_name(self, raw: object, named: str) -> None:
        with pytest.raises(ConfigurationError, match=named):
            parse_comment_budget(raw)


class TestGate:
    @pytest.fixture
    def repo(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        _write(tmp_path, "src/wordy.py", _module(30, 30))
        monkeypatch.chdir(tmp_path)
        return tmp_path

    def _config(self, repo: Path, body: str = "") -> None:
        _write(repo, ".pf-guards.toml", f'[tool.pf_guards]\nroot = "src"\n{body}')

    def test_without_the_table_the_check_does_not_run(self, repo: Path, capsys) -> None:
        self._config(repo)
        assert run_cli([]) == 0
        assert "PROSE" not in capsys.readouterr().out

    def test_over_the_budget_fails_the_gate(self, repo: Path, capsys) -> None:
        self._config(repo, "[tool.pf_guards.comment_budget]\n")
        assert run_cli([]) == 1
        out = capsys.readouterr().out
        assert "PROSE wordy.py: 30 prose lines to 30 code (100%, limit 48%)" in out
        assert "PROSE total: 30 prose lines to 30 code (100%, limit 22%)" in out

    def test_a_malformed_budget_exits_two(self, repo: Path, capsys) -> None:
        self._config(repo, "[tool.pf_guards.comment_budget]\nfile = 2\nratio = 1\n")
        assert run_cli([]) == 2
        assert "comment_budget" in capsys.readouterr().out

    def test_tests_in_the_roots_no_longer_dilute_the_total(self, repo: Path, capsys) -> None:
        _write(
            repo,
            "src/wordy.py",
            "".join(f"# why {i}\nx{i} = {i}\n" for i in range(12)) + _module(0, 28),
        )
        _write(repo, "tests/test_a.py", _module(0, 100))
        root = '[tool.pf_guards]\nroot = ["src", "tests"]\n[tool.pf_guards.comment_budget]\n'
        _write(repo, ".pf-guards.toml", root)
        assert run_cli([]) == 0
        _write(repo, ".pf-guards.toml", root + 'total_root = "src"\n')
        assert run_cli([]) == 1
        assert (
            "PROSE total (src): 12 prose lines to 40 code (30%, limit 22%)"
            in capsys.readouterr().out
        )

    def test_a_total_root_that_is_not_scanned_exits_two(self, repo: Path, capsys) -> None:
        self._config(repo, '[tool.pf_guards.comment_budget]\ntotal_root = "lib"\n')
        assert run_cli([]) == 2
        assert "total_root" in capsys.readouterr().out

    def test_every_opted_in_check_reports_in_one_run(self, repo: Path, capsys) -> None:
        _write(repo, "src/long.py", "def f():\n" + "    x = 1\n" * 70)
        self._config(repo, "max_function_lines = 60\n[tool.pf_guards.comment_budget]\n")
        assert run_cli([]) == 1
        out = capsys.readouterr().out
        assert "FAIL  long.py:1 f: 71 lines" in out
        assert "PROSE wordy.py:" in out
