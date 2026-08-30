"""pf_core.guards.framework — each rule against source that breaks it and source that only
mentions it, plus the carve-outs and exemptions.

The check fails silently: a regex that stops matching leaves a tree unchecked with nothing
else in the suite noticing, so every rule is pinned from both sides.
"""

from __future__ import annotations

import builtins
import importlib
import re
from pathlib import Path

import pytest

from pf_core.exceptions import ConfigurationError
from pf_core.guards.framework import check_framework, scan_framework, stale_exemptions
from pf_core.guards.framework_config import FrameworkConfig, FrameworkExemption
from pf_core.guards.framework_rules import RULES, FrameworkRule

BREACHES = {
    "logging": "import logging\n",
    "dotenv": "from dotenv import load_dotenv\n",
    "requests": "import requests\n",
    "httpx": "import httpx\n",
    "aiohttp": "import aiohttp\n",
    "urllib3": "import urllib3\n",
    "concurrent.futures": "import concurrent.futures\n",
    "multiprocessing": "from multiprocessing import Pool\n",
    "hashlib": "import hashlib\n",
    "Exception": "def f():\n    raise Exception('x')\n",
    "RuntimeError": "def f():\n    raise RuntimeError('x')\n",
    "ValueError": "def f():\n    raise ValueError('x')\n",
    "env-read": "import os\n\n\ndef f():\n    return os.environ.get('X')\n",
    "print": "def f():\n    print('x')\n",
    "logger-exception": "def f(logger):\n    logger.exception('x')\n",
    "atomic-write": "import os\n\n\ndef f(a, b):\n    os.replace(a, b)\n",
    "json-write": "import json\n\n\ndef f(p, o):\n    p.write_text(json.dumps(o, indent=2) + '\\n')\n",
}


def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


def _rules(root: Path, config: FrameworkConfig | None = None) -> list[tuple[str, str, int]]:
    return [(b.rule, b.path, b.line) for b in check_framework(root, config)]


def test_every_rule_has_a_breach_case() -> None:
    assert set(BREACHES) == {r.name for r in RULES}


@pytest.mark.parametrize("rule", sorted(BREACHES))
def test_each_rule_refuses_its_hand_roll(rule: str, tmp_path: Path) -> None:
    root = _tree(tmp_path, {"breach.py": BREACHES[rule]})
    found = check_framework(root)
    assert {b.rule for b in found} == {rule}
    assert all(b.use for b in found), "a breach that does not name its replacement teaches nothing"


@pytest.mark.parametrize("rule", sorted(BREACHES))
def test_a_docstring_that_names_a_rule_does_not_break_it(rule: str, tmp_path: Path) -> None:
    root = _tree(tmp_path, {"prose.py": f'"""{BREACHES[rule]}"""\n'})
    assert check_framework(root) == []


@pytest.mark.parametrize("rule", sorted(BREACHES))
def test_a_comment_that_names_a_rule_does_not_break_it(rule: str, tmp_path: Path) -> None:
    commented = "".join(f"# {line}\n" for line in BREACHES[rule].splitlines())
    root = _tree(tmp_path, {"prose.py": commented + "X = 1\n"})
    assert check_framework(root) == []


def test_f_string_text_is_prose_but_its_expressions_are_code(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            "prose.py": 'def f(n):\n    return f"never print({n}) or os.replace( here"\n',
            "code.py": 'import os\n\n\ndef g():\n    return f"home {os.environ["HOME"]}"\n',
        },
    )
    assert _rules(root) == [("env-read", "code.py", 5)]


@pytest.mark.parametrize(
    "splitter", ["\x0c", "\x0b", "\x1c", "\x85", "\u2028"], ids=["ff", "vt", "fs", "nel", "ls"]
)
def test_a_character_python_does_not_end_a_line_on_keeps_rows_aligned(
    splitter: str, tmp_path: Path
) -> None:
    """``str.splitlines`` ends a line on these and tokenize does not; blanking must agree."""
    source = (
        f'import os\nSEP = "{splitter}"\n# a {splitter} note\n'
        'print("x")\n# print(this) is prose\nkey = os.environ["K"]\n'
    )
    assert _rules(_tree(tmp_path, {"m.py": source})) == [
        ("print", "m.py", 4),
        ("env-read", "m.py", 6),
    ]


def test_a_form_feed_line_keeps_rows_aligned(tmp_path: Path) -> None:
    source = 'import os\n\x0c\nprint("x")\n# print(this) is prose\nkey = os.environ["K"]\n'
    assert _rules(_tree(tmp_path, {"m.py": source})) == [
        ("print", "m.py", 3),
        ("env-read", "m.py", 5),
    ]


def test_every_named_replacement_exists() -> None:
    """A message that names a symbol pf-core does not have sends the reader nowhere."""
    missing = []
    for rule in RULES:
        for dotted in re.findall(r"pf_core(?:\.[A-Za-z_]\w*)+", rule.use):
            parts = dotted.split(".")
            for i in range(len(parts), 0, -1):
                try:
                    obj = importlib.import_module(".".join(parts[:i]))
                except ModuleNotFoundError:
                    continue
                for attr in parts[i:]:
                    obj = getattr(obj, attr, None)
                if obj is None:
                    missing.append(f"{rule.name}: {dotted}")
                break
            else:
                missing.append(f"{rule.name}: {dotted}")
    assert not missing


class TestImports:
    @pytest.mark.parametrize(
        "source",
        [
            "from concurrent import futures\n",
            "import concurrent.futures.thread\n",
            "import logging.handlers\n",
            "from logging import getLogger\n",
        ],
    )
    def test_submodule_and_from_forms_are_caught(self, source: str, tmp_path: Path) -> None:
        assert len(check_framework(_tree(tmp_path, {"m.py": source}))) == 1

    def test_a_relative_import_of_a_same_named_module_is_not_stdlib(self, tmp_path: Path) -> None:
        root = _tree(tmp_path, {"pkg/m.py": "from .logging import setup\nfrom . import hashlib\n"})
        assert check_framework(root) == []


class TestPydanticValidators:
    def test_value_error_inside_a_validator_body_is_not_a_breach(self, tmp_path: Path) -> None:
        source = (
            "import pydantic\n"
            "from pydantic import BaseModel, field_validator, model_validator, validator\n\n\n"
            "class M(BaseModel):\n"
            "    x: int\n\n"
            '    @field_validator("x")\n'
            "    @classmethod\n"
            "    def _x(cls, v):\n"
            "        if v < 0:\n"
            '            raise ValueError("x must be non-negative")\n'
            "        return v\n\n"
            '    @model_validator(mode="after")\n'
            "    def _all(self):\n"
            "        def inner():\n"
            '            raise ValueError("nested, still lexically inside")\n'
            "        inner()\n"
            "        return self\n\n"
            '    @validator("x")\n'
            "    def _v1(cls, v):\n"
            '        raise ValueError("v1 style")\n\n'
            '    @pydantic.field_validator("x")\n'
            "    def _qualified(cls, v):\n"
            '        raise ValueError("qualified decorator")\n'
        )
        assert check_framework(_tree(tmp_path, {"m.py": source})) == []

    def test_only_value_error_is_carved_out(self, tmp_path: Path) -> None:
        source = (
            "from pydantic import BaseModel, field_validator\n\n\n"
            "class M(BaseModel):\n"
            "    x: int\n\n"
            '    @field_validator("x")\n'
            "    def _x(cls, v):\n"
            '        raise RuntimeError("pydantic does not convert this")\n'
        )
        assert _rules(_tree(tmp_path, {"m.py": source})) == [("RuntimeError", "m.py", 9)]

    def test_a_helper_the_validator_calls_is_not_inside_it(self, tmp_path: Path) -> None:
        source = (
            "from pydantic import BaseModel, field_validator\n\n\n"
            "def _check(v):\n"
            '    raise ValueError("outside the validator body")\n\n\n'
            "class M(BaseModel):\n"
            "    x: int\n\n"
            '    @field_validator("x")\n'
            "    def _x(cls, v):\n"
            "        return _check(v)\n"
        )
        assert _rules(_tree(tmp_path, {"m.py": source})) == [("ValueError", "m.py", 5)]


class TestArgparseConverters:
    def test_a_type_converter_may_raise_value_error(self, tmp_path: Path) -> None:
        source = (
            "import argparse\n\n\n"
            "def inches(text):\n"
            "    if float(text) <= 0:\n"
            '        raise ValueError("inches must be positive")\n'
            "    return float(text)\n\n\n"
            "def other(text):\n"
            '    raise ValueError("not passed as type=")\n\n\n'
            "def build():\n"
            "    p = argparse.ArgumentParser()\n"
            '    p.add_argument("--inches", type=inches)\n'
            "    return p\n"
        )
        assert _rules(_tree(tmp_path, {"cli.py": source})) == [("ValueError", "cli.py", 11)]

    def test_a_converter_defined_in_another_module(self, tmp_path: Path) -> None:
        root = _tree(
            tmp_path,
            {
                "types.py": 'def lpi(text):\n    raise ValueError("bad lpi")\n',
                "cli.py": (
                    "import argparse\n\nfrom . import types\n\n\n"
                    "def build():\n"
                    "    p = argparse.ArgumentParser()\n"
                    '    p.add_argument("--lpi", type=types.lpi)\n'
                    "    return p\n"
                ),
            },
        )
        assert check_framework(root) == []

    def test_only_value_error_is_carved_out(self, tmp_path: Path) -> None:
        source = (
            "def inches(text):\n"
            '    raise RuntimeError("argparse does not convert this")\n\n\n'
            "def build(p):\n"
            '    p.add_argument("--inches", type=inches)\n'
        )
        assert _rules(_tree(tmp_path, {"cli.py": source})) == [("RuntimeError", "cli.py", 2)]


class TestCalls:
    @pytest.mark.parametrize(
        "source",
        [
            "class Reporter:\n    def print(self, msg):\n        self.lines.append(msg)\n",
            "class Reporter:\n    async def print(self, msg):\n        await self.send(msg)\n",
            "class Reporter:\n    def  print (self, msg):\n        self.lines.append(msg)\n",
        ],
        ids=["def", "async-def", "spaced"],
    )
    def test_defining_a_print_is_not_calling_it(self, source: str, tmp_path: Path) -> None:
        assert check_framework(_tree(tmp_path, {"m.py": source})) == []

    def test_a_print_after_a_definition_on_its_line_is_a_call(self, tmp_path: Path) -> None:
        assert _rules(_tree(tmp_path, {"m.py": "def f(): print('x')\n"})) == [("print", "m.py", 1)]

    @pytest.mark.parametrize(
        "receiver",
        ["logger", "_log", "log", "_logger", "self._logger", "LOGGER", "logging", "app_log"],
    )
    def test_exception_on_any_logger_name(self, receiver: str, tmp_path: Path) -> None:
        source = f"def f(self):\n    {receiver}.exception('failed')\n"
        assert _rules(_tree(tmp_path, {"m.py": source})) == [("logger-exception", "m.py", 2)]

    @pytest.mark.parametrize(
        "call", ["future.exception()", "task.exception(timeout=1)", "self.dialog.exception()"]
    )
    def test_exception_on_anything_else_is_not_logging(self, call: str, tmp_path: Path) -> None:
        source = f"def f(self, future, task):\n    return {call}\n"
        assert check_framework(_tree(tmp_path, {"m.py": source})) == []


class TestEnvironment:
    def test_passing_the_environment_to_a_subprocess_is_not_a_read(self, tmp_path: Path) -> None:
        source = (
            "import os\nimport subprocess\n\n\n"
            "def run(cmd):\n"
            '    return subprocess.run(cmd, env={**os.environ, "LC_ALL": "C"}, check=True)\n\n\n'
            "def spawn(cmd):\n"
            "    return subprocess.Popen(\n"
            "        cmd,\n"
            "        env={\n"
            "            **os.environ,\n"
            '            "LC_ALL": "C",\n'
            "        },\n"
            "    )\n\n\n"
            "def wide(cmd):\n"
            '    return subprocess.run(["é", cmd], env={**os.environ})\n'
        )
        assert check_framework(_tree(tmp_path, {"m.py": source})) == []

    @pytest.mark.parametrize(
        "expr",
        [
            'os.environ["X"]',
            'os.environ.get("X")',
            'os.getenv("X")',
            '{**os.environ, "X": os.environ.get("X") or "0"}',
            "Settings(**os.environ)",
            '{**os.environ, "LC_ALL": "C"}',
            "run(cmd, env=dict(os.environ))",
            "run(cmd, env=os.environ.copy())",
            "run(cmd, extra={**os.environ})",
            'run(cmd, env={**os.environ, "X": os.environ["Y"]})',
        ],
    )
    def test_a_read_is_a_breach(self, expr: str, tmp_path: Path) -> None:
        source = f"import os\n\n\ndef f(cmd):\n    return {expr}\n"
        assert _rules(_tree(tmp_path, {"m.py": source})) == [("env-read", "m.py", 5)]

    def test_an_unpacked_copy_is_a_read_where_it_is_taken(self, tmp_path: Path) -> None:
        """The copy is where the settings leave ``os.environ``; what reads it later is anyone."""
        source = 'import os\n\nenv = {**os.environ}\napi_key = env["API_KEY"]\n'
        assert _rules(_tree(tmp_path, {"m.py": source})) == [("env-read", "m.py", 3)]


class TestExemptions:
    def _cfg(self, *exempt: tuple[str, str]) -> FrameworkConfig:
        reason = "the entry point is where user-facing output belongs"
        return FrameworkConfig(exempt=tuple(FrameworkExemption(r, p, reason) for r, p in exempt))

    def test_an_exemption_covers_its_rule_in_its_file(self, tmp_path: Path) -> None:
        root = _tree(tmp_path, {"cli.py": BREACHES["print"], "core.py": BREACHES["print"]})
        assert _rules(root, self._cfg(("print", "cli.py"))) == [("print", "core.py", 2)]

    def test_an_exemption_is_the_whole_path_not_the_basename(self, tmp_path: Path) -> None:
        root = _tree(tmp_path, {"cli.py": BREACHES["print"], "sub/cli.py": BREACHES["print"]})
        assert _rules(root, self._cfg(("print", "cli.py"))) == [("print", "sub/cli.py", 2)]

    def test_an_exemption_covers_one_rule_only(self, tmp_path: Path) -> None:
        root = _tree(tmp_path, {"cli.py": BREACHES["print"] + BREACHES["ValueError"]})
        assert [r for r, _, _ in _rules(root, self._cfg(("print", "cli.py")))] == ["ValueError"]

    def test_import_and_raise_rules_are_exemptible_too(self, tmp_path: Path) -> None:
        root = _tree(tmp_path, {"m.py": BREACHES["hashlib"] + BREACHES["ValueError"]})
        assert _rules(root, self._cfg(("hashlib", "m.py"), ("ValueError", "m.py"))) == []

    def test_check_framework_refuses_a_stale_exemption(self, tmp_path: Path) -> None:
        """The gate fails on one; the Python entry point must not quietly accept it."""
        root = _tree(tmp_path, {"cli.py": BREACHES["print"]})
        with pytest.raises(ConfigurationError, match=r"print in gone\.py"):
            check_framework(root, self._cfg(("print", "cli.py"), ("print", "gone.py")))

    def test_an_exemption_that_suppresses_nothing_is_stale(self, tmp_path: Path) -> None:
        root = _tree(tmp_path, {"cli.py": BREACHES["print"], "clean.py": "X = 1\n"})
        cfg = self._cfg(("print", "cli.py"), ("print", "clean.py"), ("print", "gone.py"))
        stale = stale_exemptions(scan_framework(root, cfg), cfg.exempt)
        assert [(e.rule, e.path) for e in stale] == [("print", "clean.py"), ("print", "gone.py")]


class TestConfiguration:
    def test_a_disabled_rule_does_not_run(self, tmp_path: Path) -> None:
        root = _tree(tmp_path, {"m.py": BREACHES["logging"] + BREACHES["print"]})
        assert [r for r, _, _ in _rules(root, FrameworkConfig(disable=("logging",)))] == ["print"]

    def test_a_replacement_is_named_in_the_breach(self, tmp_path: Path) -> None:
        cfg = FrameworkConfig(replace={"requests": ("myapp.http", "the crawl User-Agent")})
        [breach] = check_framework(_tree(tmp_path, {"m.py": BREACHES["requests"]}), cfg)
        assert (breach.use, breach.why) == ("myapp.http", "the crawl User-Agent")

    def test_a_multi_root_scan_prefixes_paths(self, tmp_path: Path) -> None:
        root = _tree(tmp_path, {"cli.py": BREACHES["print"]})
        found = scan_framework(root, FrameworkConfig(), path_prefix="src/")
        assert [(b.path, b.line) for b in found] == [("src/cli.py", 2)]


@pytest.mark.parametrize(
    "rule", [r for r in RULES if r.kind == "raise" and r.name != "Exception"], ids=lambda r: r.name
)
def test_a_raise_rule_names_a_subclass_of_the_builtin_it_refuses(rule: FrameworkRule) -> None:
    """The swap the message asks for must keep every existing ``except <builtin>`` working."""
    module, _, name = rule.use.rpartition(".")
    replacement = getattr(importlib.import_module(module), name)
    assert issubclass(replacement, getattr(builtins, rule.name))
