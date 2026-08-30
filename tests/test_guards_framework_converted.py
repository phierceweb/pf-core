"""pf_core.guards.framework_converted — which functions' ``ValueError`` pydantic or argparse
converts, resolved per module rather than by bare name across the tree."""

from __future__ import annotations

from pathlib import Path

import pytest

from pf_core.guards.framework import check_framework

RAISES = '    raise ValueError("bad")\n'


def _breaches(tmp_path: Path, files: dict[str, str], root: str = "") -> list[tuple[str, int]]:
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    breaches = check_framework(tmp_path / root)
    return [(b.path, b.line) for b in breaches if b.rule == "ValueError"]


class TestValidatorDecorators:
    @pytest.mark.parametrize(
        ("imports", "decorator"),
        [
            ("from pydantic import field_validator", '@field_validator("x")'),
            ("from pydantic import field_validator as fv", '@fv("x")'),
            ("import pydantic as pd", '@pd.field_validator("x")'),
            ("from pydantic import model_validator", '@model_validator(mode="after")'),
            ("from pydantic.v1 import validator", '@validator("x")'),
            ("from pydantic.functional_validators import field_validator", '@field_validator("x")'),
        ],
    )
    def test_a_validator_imported_from_pydantic_is_carved_out(
        self, imports: str, decorator: str, tmp_path: Path
    ) -> None:
        source = f"{imports}\n\n\nclass M:\n    {decorator}\n    def _x(cls, v):\n    {RAISES}"
        assert _breaches(tmp_path, {"m.py": source}) == []

    def test_an_attrs_field_validator_is_not_pydantic(self, tmp_path: Path) -> None:
        source = (
            "import attrs\n\n\n"
            "@attrs.define\n"
            "class Job:\n"
            "    retries: int = attrs.field()\n\n"
            "    @retries.validator\n"
            "    def _check(self, attribute, value):\n"
            f"    {RAISES}"
        )
        assert _breaches(tmp_path, {"m.py": source}) == [("m.py", 10)]

    @pytest.mark.parametrize(
        "imports",
        ["from mylib import field_validator", "from attrs import validator as field_validator"],
    )
    def test_a_same_named_decorator_from_elsewhere_is_not_pydantic(
        self, imports: str, tmp_path: Path
    ) -> None:
        source = f'{imports}\n\n\n@field_validator("x")\ndef _x(v):\n{RAISES}'
        assert _breaches(tmp_path, {"m.py": source}) == [("m.py", 6)]

    def test_an_unimported_decorator_name_is_not_pydantic(self, tmp_path: Path) -> None:
        source = f'@field_validator("x")\ndef _x(v):\n{RAISES}'
        assert _breaches(tmp_path, {"m.py": source}) == [("m.py", 3)]


class TestAnnotatedValidators:
    @pytest.mark.parametrize(
        "wrapper", ["AfterValidator", "BeforeValidator", "PlainValidator", "WrapValidator"]
    )
    def test_a_function_wrapped_by_pydantic_is_carved_out(
        self, wrapper: str, tmp_path: Path
    ) -> None:
        source = (
            "from typing import Annotated\n\n"
            f"from pydantic import {wrapper}, BaseModel\n\n\n"
            f"def _positive(v):\n{RAISES}\n\n"
            f"class Order(BaseModel):\n    qty: Annotated[int, {wrapper}(_positive)]\n"
        )
        assert _breaches(tmp_path, {"m.py": source}) == []

    def test_the_func_keyword_and_a_qualified_wrapper(self, tmp_path: Path) -> None:
        source = (
            "from typing import Annotated\n\nimport pydantic\n\n\n"
            f"def _a(v):\n{RAISES}\n\ndef _b(v):\n{RAISES}\n\n"
            "A = Annotated[int, pydantic.AfterValidator(func=_a)]\n"
            "B = Annotated[int, pydantic.functional_validators.BeforeValidator(_b)]\n"
        )
        assert _breaches(tmp_path, {"m.py": source}) == []

    def test_a_function_a_wrapped_lambda_calls_is_carved_out(self, tmp_path: Path) -> None:
        source = (
            "from typing import Annotated\n\nfrom pydantic import AfterValidator\n\n\n"
            f"def _check(v):\n{RAISES}\n\n"
            "Qty = Annotated[int, AfterValidator(lambda v: _check(abs(v)))]\n"
        )
        assert _breaches(tmp_path, {"m.py": source}) == []

    def test_a_wrapper_from_elsewhere_is_not_pydantic(self, tmp_path: Path) -> None:
        source = (
            "from mylib import AfterValidator\n\n\n"
            f"def _check(v):\n{RAISES}\n\n"
            "CHECK = AfterValidator(_check)\n"
        )
        assert _breaches(tmp_path, {"m.py": source}) == [("m.py", 5)]


class TestTypeConverters:
    def test_an_imported_callable_does_not_carve_a_same_named_function(
        self, tmp_path: Path
    ) -> None:
        """``type=json.loads`` in one module says nothing about another module's ``loads``."""
        files = {
            "cli.py": 'import json\n\n\ndef build(p):\n    p.add_argument("--x", type=json.loads)\n',
            "codec.py": f"def loads(text):\n{RAISES}",
        }
        assert _breaches(tmp_path, files) == [("codec.py", 2)]

    def test_a_from_imported_callable_is_not_a_local_method(self, tmp_path: Path) -> None:
        source = (
            "from json import loads\n\n\n"
            f"class Codec:\n    def loads(self, text):\n    {RAISES}\n\n"
            'def build(p):\n    p.add_argument("--x", type=loads)\n'
        )
        assert _breaches(tmp_path, {"cli.py": source}) == [("cli.py", 6)]

    def test_a_converter_in_another_module_is_carved_only_through_an_import(
        self, tmp_path: Path
    ) -> None:
        files = {
            "a.py": f'def size(text):\n{RAISES}\n\ndef build(p):\n    p.add_argument("-s", type=size)\n',
            "b.py": f"def size(text):\n{RAISES}",
        }
        assert _breaches(tmp_path, files) == [("b.py", 2)]

    @pytest.mark.parametrize(
        ("imports", "passed"),
        [
            ("from .conv import lpi", "lpi"),
            ("from pkg.conv import lpi", "lpi"),
            ("from pkg.conv import lpi as screen", "screen"),
            ("from . import conv", "conv.lpi"),
            ("import pkg.conv as conv", "conv.lpi"),
            ("import pkg.conv", "pkg.conv.lpi"),
        ],
    )
    def test_an_imported_converter_is_carved_in_its_own_module(
        self, imports: str, passed: str, tmp_path: Path
    ) -> None:
        files = {
            "pkg/__init__.py": "",
            "pkg/conv.py": f"def lpi(text):\n{RAISES}",
            "pkg/cli.py": f'{imports}\n\n\ndef build(p):\n    p.add_argument("--lpi", type={passed})\n',
        }
        assert _breaches(tmp_path, files, root="pkg") == []

    def test_a_relative_import_resolves_against_its_package(self, tmp_path: Path) -> None:
        files = {
            "conv.py": f"def lpi(text):\n{RAISES}",
            "sub/conv.py": f"def lpi(text):\n{RAISES}",
            "sub/cli.py": 'from .conv import lpi\n\n\ndef build(p):\n    p.add_argument("-l", type=lpi)\n',
        }
        assert _breaches(tmp_path, files) == [("conv.py", 2)]

    def test_a_function_a_type_lambda_calls_is_carved_out(self, tmp_path: Path) -> None:
        source = (
            "import argparse\n\n\n"
            "def _size(s):\n"
            '    if not s.endswith("k"):\n'
            '        raise ValueError("size must end in k")\n'
            "    return int(s[:-1]) * 1024\n\n\n"
            "def build():\n"
            "    p = argparse.ArgumentParser()\n"
            '    p.add_argument("--size", type=lambda s: _size(s.strip()))\n'
            "    return p\n"
        )
        assert _breaches(tmp_path, {"cli.py": source}) == []

    @pytest.mark.parametrize(
        "imports",
        ["from pf_core.utils import dates", "import pf_core.utils.dates as dates"],
    )
    def test_a_module_from_outside_the_root_carves_nothing_same_named_inside_it(
        self, imports: str, tmp_path: Path
    ) -> None:
        """The import points at pf-core's ``dates``, not the local one that shares its name."""
        files = {
            "dates.py": f"def parse_date(text):\n{RAISES}",
            "cli.py": (
                f"{imports}\n\n\n"
                'def build(p):\n    p.add_argument("--day", type=dates.parse_date)\n'
            ),
        }
        assert _breaches(tmp_path, files) == [("dates.py", 2)]

    def test_a_directory_that_is_no_package_is_also_its_namespace_package(
        self, tmp_path: Path
    ) -> None:
        files = {
            "tools/conv.py": f"def lpi(text):\n{RAISES}",
            "tools/cli.py": (
                "from tools.conv import lpi\n\n\n"
                'def build(p):\n    p.add_argument("--lpi", type=lpi)\n'
            ),
        }
        assert _breaches(tmp_path, files, root="tools") == []


class TestMethodConverters:
    """A method passed as ``type=`` is resolved to its class, never to a same-named function."""

    def test_a_bound_method_is_carved_out(self, tmp_path: Path) -> None:
        source = (
            "class Command:\n"
            f"    def _positive(self, text):\n    {RAISES}\n"
            '    def parser(self, p):\n        p.add_argument("--n", type=self._positive)\n\n\n'
            f"class Other:\n    def _positive(self, text):\n    {RAISES}\n\n"
            f"def _positive(text):\n{RAISES}"
        )
        assert _breaches(tmp_path, {"cmd.py": source}) == [("cmd.py", 11), ("cmd.py", 15)]

    @pytest.mark.parametrize("passed", ["Level.parse", "cls.parse"])
    def test_a_class_or_classmethod_attribute_is_carved_out(
        self, passed: str, tmp_path: Path
    ) -> None:
        source = (
            "from enum import Enum\n\n\n"
            "class Level(Enum):\n"
            '    LOW = "low"\n\n'
            "    @classmethod\n"
            f"    def parse(cls, text):\n    {RAISES}\n"
            "    @classmethod\n"
            f'    def build(cls, p):\n        p.add_argument("--level", type={passed})\n'
        )
        assert _breaches(tmp_path, {"levels.py": source}) == []

    def test_a_method_of_an_imported_class_is_carved_in_its_module(self, tmp_path: Path) -> None:
        files = {
            "pkg/__init__.py": "",
            "pkg/levels.py": f"class Level:\n    @classmethod\n    def parse(cls, text):\n    {RAISES}",
            "pkg/cli.py": (
                "from .levels import Level\n\n\n"
                'def build(p):\n    p.add_argument("--level", type=Level.parse)\n'
            ),
        }
        assert _breaches(tmp_path, files, root="pkg") == []

    def test_a_same_named_method_of_another_class_is_not_carved(self, tmp_path: Path) -> None:
        source = (
            f"class A:\n    def parse(self, text):\n    {RAISES}\n"
            f"class B:\n    def parse(self, text):\n    {RAISES}\n"
            'def build(p):\n    p.add_argument("--x", type=A.parse)\n'
        )
        assert _breaches(tmp_path, {"m.py": source}) == [("m.py", 7)]
