"""pf_core.guards.sources — rows as Python numbers them, and a file an opt-in check cannot read
failing the gate instead of being skipped."""

from __future__ import annotations

from pathlib import Path

import pytest

from pf_core.guards.comments import CommentBudget, scan_comment_budget
from pf_core.guards.config import FunctionLimits
from pf_core.guards.framework import check_framework, scan_framework
from pf_core.guards.functions import scan_function_lengths
from pf_core.guards.sources import source_lines, unparsed
from pf_core.guards.structure import run_cli

BROKEN = "import requests\n\n\ndef fetch(url):\n    return requests.get(url, timeout=(\n"


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_source_lines_splits_only_where_python_ends_a_row() -> None:
    assert source_lines("a\x0cb\u2028c\nd\r\ne") == ["a\x0cb\u2028c", "d\r", "e"]


def test_unparsed_names_each_broken_file_once_across_roots(tmp_path: Path) -> None:
    _write(tmp_path, "src/ok.py", "X = 1\n")
    _write(tmp_path, "src/broken.py", BROKEN)
    found = unparsed([tmp_path / "src", tmp_path / "src"])
    assert [(Path(e.filename or "").name, e.lineno) for e in found] == [("broken.py", 5)]


@pytest.mark.parametrize(
    ("data", "line"),
    [
        (b"\xef\xbb\xbfimport requests\n", 1),
        ("# -*- coding: latin-1 -*-\nNAME = 'caf\xe9'\nimport requests\n".encode("latin-1"), 3),
    ],
    ids=["utf-8-bom", "latin-1-cookie"],
)
def test_a_file_python_reads_is_read_as_python_reads_it(
    data: bytes, line: int, tmp_path: Path
) -> None:
    (tmp_path / "enc.py").write_bytes(data)
    assert unparsed([tmp_path]) == []
    assert [(b.path, b.line, b.rule) for b in check_framework(tmp_path)] == [
        ("enc.py", line, "requests")
    ]


@pytest.mark.parametrize("newline", [b"\r\n", b"\r"], ids=["crlf", "cr"])
def test_rows_end_where_python_ends_them(newline: bytes, tmp_path: Path) -> None:
    (tmp_path / "rows.py").write_bytes(newline.join([b"X = 1", b"print(X)", b""]))
    assert [(b.line, b.rule) for b in check_framework(tmp_path)] == [(2, "print")]


def test_bytes_python_cannot_decode_do_not_parse(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_bytes(b"NAME = 'caf\xe9'\n")
    assert [Path(e.filename or "").name for e in unparsed([tmp_path])] == ["bad.py"]


@pytest.mark.parametrize(
    "scan",
    [
        lambda root: check_framework(root),
        lambda root: scan_function_lengths(root, FunctionLimits(hard=60, soft=48)),
        lambda root: scan_comment_budget([root], CommentBudget()),
    ],
    ids=["framework", "function-length", "comment-budget"],
)
def test_a_scan_raises_on_a_file_it_cannot_read(scan, tmp_path: Path) -> None:
    _write(tmp_path, "broken.py", BROKEN)
    with pytest.raises(SyntaxError) as caught:
        scan(tmp_path)
    assert (caught.value.filename or "").endswith("broken.py")


@pytest.mark.parametrize(
    "scan",
    [
        lambda root: scan_framework(root, skip_unparsed=True),
        lambda root: scan_function_lengths(
            root, FunctionLimits(hard=3, soft=2), skip_unparsed=True
        ),
        lambda root: scan_comment_budget(
            [root], CommentBudget(min_code_lines=1), skip_unparsed=True
        ),
    ],
    ids=["framework", "function-length", "comment-budget"],
)
def test_skip_unparsed_passes_over_the_file_and_reads_the_rest(scan, tmp_path: Path) -> None:
    _write(tmp_path, "broken.py", BROKEN)
    _write(
        tmp_path,
        "other.py",
        "import requests\n\ndef f():\n    # a\n    # b\n    # c\n    return 1\n",
    )
    paths = {v.path for v in scan(tmp_path)}
    assert "other.py" in paths and "broken.py" not in paths


class TestGate:
    @pytest.fixture
    def repo(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        _write(tmp_path, "pkg/fn.py", "def f():\n    return 1\n")
        _write(tmp_path, "pkg/broken.py", BROKEN)
        monkeypatch.chdir(tmp_path)
        return tmp_path

    def _config(self, repo: Path, body: str = "") -> None:
        _write(repo, ".pf-guards.toml", f'[tool.pf_guards]\nroot = "pkg"\n{body}')

    @pytest.mark.parametrize(
        "body",
        [
            "[tool.pf_guards.framework]\n",
            "max_function_lines = 60\n",
            "[tool.pf_guards.comment_budget]\n",
        ],
        ids=["framework", "function-length", "comment-budget"],
    )
    def test_an_opted_in_check_fails_on_a_file_it_cannot_read(
        self, repo: Path, body: str, capsys
    ) -> None:
        self._config(repo, body)
        assert run_cli([]) == 1
        assert "FAIL  pkg/broken.py:5: does not parse" in capsys.readouterr().out

    def test_named_once_with_every_check_on(self, repo: Path, capsys) -> None:
        self._config(
            repo,
            "max_function_lines = 60\n[tool.pf_guards.comment_budget]\n[tool.pf_guards.framework]\n",
        )
        assert run_cli([]) == 1
        assert capsys.readouterr().out.count("pkg/broken.py") == 1

    @pytest.mark.parametrize("flags", [["--framework"], ["--framework", "--report"], ["--report"]])
    def test_report_mode_does_not_excuse_it(self, repo: Path, flags: list[str], capsys) -> None:
        self._config(repo)
        assert run_cli(flags) == 1
        assert "pkg/broken.py:5: does not parse" in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("body", "found"),
        [
            ("[tool.pf_guards.framework]\n", "FRAMEWORK other.py:1: imports 'requests'"),
            ("max_function_lines = 3\n", "FAIL  other.py:3 f: 5 lines"),
            ("[tool.pf_guards.comment_budget]\nmin_code_lines = 1\n", "PROSE other.py"),
        ],
        ids=["framework", "function-length", "comment-budget"],
    )
    @pytest.mark.parametrize("flags", [[], ["--report"]], ids=["gate", "report"])
    def test_the_files_that_parse_are_still_checked(
        self, repo: Path, body: str, found: str, flags: list[str], capsys
    ) -> None:
        _write(
            repo,
            "pkg/other.py",
            "import requests\n\ndef f():\n    # a\n    # b\n    # c\n    return 1\n",
        )
        self._config(repo, body)
        assert run_cli(flags) == 1
        out = capsys.readouterr().out
        assert "pkg/broken.py:5: does not parse" in out
        assert found in out

    def test_an_exemption_for_a_file_it_cannot_read_is_not_stale(self, repo: Path, capsys) -> None:
        self._config(
            repo,
            "[tool.pf_guards.framework]\n[[tool.pf_guards.framework.exempt]]\n"
            'rule = "requests"\npath = "broken.py"\nreason = "the vendored client keeps its own"\n',
        )
        assert run_cli([]) == 1
        assert "STALE" not in capsys.readouterr().out

    def test_a_file_with_a_bom_passes(self, repo: Path, capsys) -> None:
        (repo / "pkg/broken.py").unlink()
        (repo / "pkg/bom.py").write_bytes(b"\xef\xbb\xbfX = 1\n")
        self._config(repo, "[tool.pf_guards.framework]\n")
        assert run_cli([]) == 0, capsys.readouterr().out

    def test_a_coding_cookie_passes_the_whole_gate(self, repo: Path, capsys) -> None:
        (repo / "pkg/broken.py").unlink()
        (repo / "pkg/legacy.py").write_bytes(
            "# -*- coding: latin-1 -*-\nNAME = 'caf\xe9'\n".encode("latin-1")
        )
        self._config(repo, "max_function_lines = 60\n")
        assert run_cli([]) == 0, capsys.readouterr().out

    @pytest.mark.parametrize("flags", [[], ["--framework"]], ids=["gate", "framework"])
    def test_a_null_byte_names_its_file(self, repo: Path, flags: list[str], capsys) -> None:
        (repo / "pkg/broken.py").unlink()
        (repo / "pkg/nul.py").write_bytes(b"X = 1\x00\n")
        self._config(repo, "[tool.pf_guards.framework]\n")
        assert run_cli(flags) == 1
        out = capsys.readouterr().out
        assert "FAIL  pkg/nul.py: does not parse" in out
        assert "None" not in out

    def test_without_an_opt_in_check_nothing_changes(self, repo: Path, capsys) -> None:
        """The size and layering gate never parsed; upgrading pf-core must not turn it red."""
        self._config(repo)
        assert run_cli([]) == 0
        assert "does not parse" not in capsys.readouterr().out
