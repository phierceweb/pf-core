"""Unit tests for the optional-extra import-guard helper."""

from __future__ import annotations

from pf_core._extras import extra_import_error, install_target


def test_install_target_known_extra() -> None:
    assert install_target("llm") == "pf-core[llm]"
    assert install_target("http") == "pf-core[http]"


def test_install_target_unknown_extra_falls_back() -> None:
    assert install_target("somethingnew") == "pf-core[somethingnew]"


def test_extra_import_error_names_extra_package_and_command() -> None:
    err = extra_import_error("llm", "json_repair", feature="pf_core.llm.parse")
    assert isinstance(err, ImportError)
    msg = str(err)
    assert "pf_core.llm.parse" in msg
    assert "'llm'" in msg
    assert "json_repair" in msg
    assert "pip install pf-core[llm]" in msg
