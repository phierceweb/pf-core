"""pf_core.guards.framework_env — the env-read rule through every name ``os`` or its environment
is bound to, and the one shape that hands the environment on unread."""

from __future__ import annotations

from pathlib import Path

import pytest

from pf_core.guards.framework import check_framework


def _env_lines(tmp_path: Path, source: str) -> list[int]:
    (tmp_path / "m.py").write_text(source, encoding="utf-8")
    return [b.line for b in check_framework(tmp_path) if b.rule == "env-read"]


@pytest.mark.parametrize(
    ("imports", "read"),
    [
        ("import os as o", 'o.environ["API_KEY"]'),
        ("import os as o", 'o.getenv("TOKEN")'),
        ("from os import environ", 'environ["API_KEY"]'),
        ("from os import environ", 'environ.get("API_KEY")'),
        ("from os import getenv", 'getenv("TOKEN")'),
        ("from os import environ as ENV", 'dict(ENV)["API_KEY"]'),
        ("from os import getenv as ge", 'ge("TOKEN")'),
    ],
)
def test_a_read_through_another_name_is_a_read(imports: str, read: str, tmp_path: Path) -> None:
    assert _env_lines(tmp_path, f"{imports}\n\nkey = {read}\n") == [3]


@pytest.mark.parametrize(
    ("imports", "passed"),
    [
        ("import os as o", "o.environ"),
        ("from os import environ", "environ"),
        ("from os import environ as ENV", "ENV"),
    ],
)
def test_handing_the_environment_on_through_another_name_is_not_a_read(
    imports: str, passed: str, tmp_path: Path
) -> None:
    source = (
        f'import subprocess\n{imports}\n\nsubprocess.run(["x"], env={{**{passed}, "A": "1"}})\n'
    )
    assert _env_lines(tmp_path, source) == []


def test_a_copy_through_another_name_is_still_a_read(tmp_path: Path) -> None:
    assert _env_lines(tmp_path, "from os import environ\n\nenv = {**environ}\n") == [3]


def test_one_breach_per_line_however_it_is_spelled(tmp_path: Path) -> None:
    source = 'import os\nimport os as o\n\nkey = os.environ["A"] + o.environ["B"]\n'
    assert _env_lines(tmp_path, source) == [4]


@pytest.mark.parametrize(
    "source",
    [
        "import os.path as osp\n\np = osp.join('a', 'b')\n",
        "from os import path, sep\n\np = path.join('a', sep)\n",
        "import os as o\n\np = o.path.join('a', 'b')\n",
        "from os import environ\n\n# environ['X'] is only named here\n",
    ],
)
def test_other_uses_of_os_are_not_reads(source: str, tmp_path: Path) -> None:
    assert _env_lines(tmp_path, source) == []
