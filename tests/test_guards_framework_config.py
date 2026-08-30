"""pf_core.guards.framework_config — [tool.pf_guards.framework] parsing and its refusals."""

from __future__ import annotations

from pathlib import Path

import pytest

from pf_core.exceptions import ConfigurationError
from pf_core.guards.config import load_guards_config
from pf_core.guards.framework_config import (
    FrameworkConfig,
    FrameworkExemption,
    parse_framework_config,
)
from pf_core.guards.framework_rules import RULES

REASON = "the CLI entry point is where user-facing output belongs"


def _load(tmp_path: Path, body: str) -> FrameworkConfig | None:
    p = tmp_path / ".pf-guards.toml"
    p.write_text('[tool.pf_guards]\nroot = "src"\n' + body, encoding="utf-8")
    return load_guards_config(p).framework


def test_no_section_means_the_check_is_off(tmp_path: Path) -> None:
    assert _load(tmp_path, "") is None


def test_an_empty_section_turns_every_rule_on(tmp_path: Path) -> None:
    assert _load(tmp_path, "[tool.pf_guards.framework]\n") == FrameworkConfig()


def test_the_full_section_parses(tmp_path: Path) -> None:
    cfg = _load(
        tmp_path,
        "[tool.pf_guards.framework]\n"
        'root = "src/pkg"\n'
        'disable = ["logging", "logger-exception"]\n'
        "[tool.pf_guards.framework.replace]\n"
        'requests = ["myapp.http", "the crawl User-Agent and size caps"]\n'
        'Exception = "a class from myapp.errors"\n'
        "[[tool.pf_guards.framework.exempt]]\n"
        'rule = "print"\n'
        'path = "cli.py"\n'
        f'reason = "{REASON}"\n',
    )
    assert cfg is not None
    assert cfg.root == "src/pkg"
    assert cfg.disable == ("logging", "logger-exception")
    default_why = next(r.why for r in RULES if r.name == "Exception")
    assert cfg.replace == {
        "requests": ("myapp.http", "the crawl User-Agent and size caps"),
        "Exception": ("a class from myapp.errors", default_why),
    }
    assert cfg.exempt == (FrameworkExemption("print", "cli.py", REASON),)


@pytest.mark.parametrize(
    "exemption",
    [
        {"rule": "print", "path": "cli.py"},
        {"rule": "print", "path": "cli.py", "reason": ""},
        {"rule": "print", "path": "cli.py", "reason": "   "},
        {"rule": "print", "path": "cli.py", "reason": "legacy code"},
    ],
    ids=["missing", "empty", "blank", "too-short"],
)
def test_an_exemption_without_a_reason_is_a_configuration_error(exemption: dict) -> None:
    with pytest.raises(ConfigurationError, match="reason"):
        parse_framework_config({"exempt": [exemption]})


@pytest.mark.parametrize(
    "raw",
    [
        {"disable": ["loging"]},
        {"replace": {"loging": "pf_core.log"}},
        {"exempt": [{"rule": "loging", "path": "a.py", "reason": REASON}]},
    ],
    ids=["disable", "replace", "exempt"],
)
def test_an_unknown_rule_is_refused_by_name(raw: dict) -> None:
    with pytest.raises(ConfigurationError, match="loging"):
        parse_framework_config(raw)


def test_an_unknown_key_is_refused_by_name() -> None:
    with pytest.raises(ConfigurationError, match="disabled"):
        parse_framework_config({"disabled": ["print"]})


def test_an_exemption_with_an_unknown_key_is_refused() -> None:
    with pytest.raises(ConfigurationError, match="because"):
        parse_framework_config(
            {"exempt": [{"rule": "print", "path": "cli.py", "reason": REASON, "because": "x"}]}
        )


def test_an_exemption_without_a_path_is_refused() -> None:
    with pytest.raises(ConfigurationError, match="path"):
        parse_framework_config({"exempt": [{"rule": "print", "reason": REASON}]})


@pytest.mark.parametrize("value", [["only-one"], ["a", "b", "c"], 3, ["a", 2]])
def test_a_malformed_replacement_is_refused(value: object) -> None:
    with pytest.raises(ConfigurationError, match="requests"):
        parse_framework_config({"replace": {"requests": value}})


@pytest.mark.parametrize("raw", [{"disable": "print"}, {"exempt": {"rule": "print"}}, "on"])
def test_a_wrongly_shaped_section_is_refused(raw: object) -> None:
    with pytest.raises(ConfigurationError):
        parse_framework_config(raw)


class TestPythonAPI:
    """The dataclasses refuse what the TOML refuses: a config built in code has no other gate."""

    @pytest.mark.parametrize(
        ("args", "named"),
        [
            (("print", "cli.py", "legacy code"), "reason"),
            (("print", "cli.py", ""), "reason"),
            (("loging", "cli.py", REASON), "loging"),
            (("print", " ", REASON), "path"),
        ],
        ids=["short-reason", "no-reason", "unknown-rule", "no-path"],
    )
    def test_an_exemption_is_validated_on_construction(
        self, args: tuple[str, str, str], named: str
    ) -> None:
        with pytest.raises(ConfigurationError, match=named):
            FrameworkExemption(*args)

    @pytest.mark.parametrize(
        ("kwargs", "named"),
        [
            ({"disable": ("loging",)}, "loging"),
            ({"replace": {"loging": ("pf_core.log", "structured events")}}, "loging"),
            ({"replace": {"requests": ("", "the crawl User-Agent")}}, "requests"),
            ({"replace": {"requests": ("myapp.http",)}}, "requests"),
        ],
        ids=["disable", "replace-rule", "replace-empty", "replace-shape"],
    )
    def test_a_config_is_validated_on_construction(self, kwargs: dict, named: str) -> None:
        with pytest.raises(ConfigurationError, match=named):
            FrameworkConfig(**kwargs)
