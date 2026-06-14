"""Tests for pf_core.utils.config_path — config-file override chain."""

from __future__ import annotations

from pathlib import Path

from pf_core.utils.config_path import resolve_config_path


def _bundled(tmp_path: Path) -> Path:
    d = tmp_path / "pkg" / "config"
    d.mkdir(parents=True)
    (d / "thing.yaml").write_text("bundled\n", encoding="utf-8")
    return d


def test_falls_through_to_bundled(tmp_path, monkeypatch):
    bundled = _bundled(tmp_path)
    monkeypatch.chdir(tmp_path)  # no ./config/thing.yaml
    monkeypatch.delenv("MYAPP_CONF_DIR", raising=False)
    p = resolve_config_path("thing.yaml", env_dir_var="MYAPP_CONF_DIR", bundled_dir=bundled)
    assert p == (bundled / "thing.yaml").resolve()
    assert p.is_absolute()


def test_cwd_config_wins_over_bundled(tmp_path, monkeypatch):
    bundled = _bundled(tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    cwd_file = tmp_path / "config" / "thing.yaml"
    cwd_file.write_text("cwd\n", encoding="utf-8")
    monkeypatch.delenv("MYAPP_CONF_DIR", raising=False)
    p = resolve_config_path("thing.yaml", env_dir_var="MYAPP_CONF_DIR", bundled_dir=bundled)
    assert p == cwd_file.resolve()


def test_env_dir_wins_over_everything(tmp_path, monkeypatch):
    bundled = _bundled(tmp_path)
    env_dir = tmp_path / "override"
    env_dir.mkdir()
    (env_dir / "thing.yaml").write_text("env\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "thing.yaml").write_text("cwd\n", encoding="utf-8")
    monkeypatch.setenv("MYAPP_CONF_DIR", str(env_dir))
    p = resolve_config_path("thing.yaml", env_dir_var="MYAPP_CONF_DIR", bundled_dir=bundled)
    assert p == (env_dir / "thing.yaml").resolve()


def test_env_dir_unset_or_missing_file_is_skipped(tmp_path, monkeypatch):
    bundled = _bundled(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MYAPP_CONF_DIR", str(tmp_path / "does-not-exist"))
    p = resolve_config_path("thing.yaml", env_dir_var="MYAPP_CONF_DIR", bundled_dir=bundled)
    assert p == (bundled / "thing.yaml").resolve()


def test_custom_cwd_subdir(tmp_path, monkeypatch):
    bundled = _bundled(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MYAPP_CONF_DIR", raising=False)
    (tmp_path / "config" / "prompts").mkdir(parents=True)
    f = tmp_path / "config" / "prompts" / "thing.yaml"
    f.write_text("nested\n", encoding="utf-8")
    p = resolve_config_path(
        "thing.yaml",
        env_dir_var="MYAPP_CONF_DIR",
        bundled_dir=bundled,
        cwd_subdir="config/prompts",
    )
    assert p == f.resolve()


def test_no_env_var_name_means_no_env_step(tmp_path, monkeypatch):
    bundled = _bundled(tmp_path)
    monkeypatch.chdir(tmp_path)
    p = resolve_config_path("thing.yaml", env_dir_var=None, bundled_dir=bundled)
    assert p == (bundled / "thing.yaml").resolve()
