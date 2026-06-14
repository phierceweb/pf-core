"""Tests for pf_core.config — centralized configuration loader."""

from __future__ import annotations

import pytest

from pf_core.config import AppConfig, _to_bool, _to_list


class TestToBool:
    @pytest.mark.parametrize("val", ["1", "true", "True", "TRUE", "yes", "Yes", "on", "ON"])
    def test_truthy_values(self, val):
        assert _to_bool(val) is True

    @pytest.mark.parametrize("val", ["0", "false", "False", "no", "off", "", "anything"])
    def test_falsy_values(self, val):
        assert _to_bool(val) is False

    def test_strips_whitespace(self):
        assert _to_bool("  true  ") is True
        assert _to_bool("  false  ") is False


class TestToList:
    def test_comma_separated(self):
        assert _to_list("a,b,c") == ["a", "b", "c"]

    def test_strips_whitespace(self):
        assert _to_list(" a , b , c ") == ["a", "b", "c"]

    def test_empty_string(self):
        assert _to_list("") == []

    def test_single_value(self):
        assert _to_list("only") == ["only"]

    def test_custom_separator(self):
        assert _to_list("a|b|c", sep="|") == ["a", "b", "c"]

    def test_trailing_separator(self):
        assert _to_list("a,b,") == ["a", "b"]

    def test_empty_segments_skipped(self):
        assert _to_list("a,,b") == ["a", "b"]


class TestAppConfigDefaults:
    def test_default_values(self):
        cfg = AppConfig()
        assert cfg.DATABASE_URL == ""
        assert cfg.WEB_HOST == "127.0.0.1"
        assert cfg.WEB_PORT == 8000
        assert cfg.LOG_LEVEL == "INFO"
        assert cfg.APP_NAME == "App"
        assert cfg.CORS_ORIGINS == []
        assert cfg.REQUEST_TIMEOUT == 120
        assert cfg.THREAD_MAX_WORKERS == 4
        assert cfg.API_RATE_LIMIT_PER_MINUTE == 60
        assert cfg.MAX_PER_PAGE == 200
        assert cfg.ID_LENGTH == 12

    def test_yaml_empty_by_default(self):
        cfg = AppConfig()
        assert cfg.yaml == {}


class TestAppConfigEnvOverrides:
    def test_string_from_env(self, monkeypatch):
        monkeypatch.setenv("APP_NAME", "TestApp")
        cfg = AppConfig()
        assert cfg.APP_NAME == "TestApp"

    def test_int_from_env(self, monkeypatch):
        monkeypatch.setenv("WEB_PORT", "9000")
        cfg = AppConfig()
        assert cfg.WEB_PORT == 9000

    def test_invalid_int_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("WEB_PORT", "not_a_number")
        cfg = AppConfig()
        assert cfg.WEB_PORT == 8000

    def test_bool_from_env(self, monkeypatch):
        class MyConfig(AppConfig):
            DEBUG: bool = False

        monkeypatch.setenv("DEBUG", "true")
        cfg = MyConfig()
        assert cfg.DEBUG is True

    def test_list_from_env(self, monkeypatch):
        monkeypatch.setenv("CORS_ORIGINS", "http://localhost:3000,http://localhost:5000")
        cfg = AppConfig()
        assert cfg.CORS_ORIGINS == ["http://localhost:3000", "http://localhost:5000"]

    def test_string_stripped(self, monkeypatch):
        monkeypatch.setenv("APP_NAME", "  Padded  ")
        cfg = AppConfig()
        assert cfg.APP_NAME == "Padded"


class TestAppConfigOverrides:
    def test_overrides_win_over_defaults(self):
        cfg = AppConfig(overrides={"APP_NAME": "Overridden"})
        assert cfg.APP_NAME == "Overridden"

    def test_overrides_win_over_env(self, monkeypatch):
        monkeypatch.setenv("APP_NAME", "FromEnv")
        cfg = AppConfig(overrides={"APP_NAME": "FromOverride"})
        assert cfg.APP_NAME == "FromOverride"


class TestAppConfigYaml:
    def test_loads_yaml_file(self, tmp_path):
        yf = tmp_path / "config.yaml"
        yf.write_text("name: test\nsettings:\n  debug: true\n")
        cfg = AppConfig(yaml_file=yf)
        assert cfg.yaml["name"] == "test"
        assert cfg.yaml["settings"]["debug"] is True

    def test_missing_yaml_file_ignored(self, tmp_path):
        cfg = AppConfig(yaml_file=tmp_path / "nonexistent.yaml")
        assert cfg.yaml == {}

    def test_invalid_yaml_warns(self, tmp_path):
        yf = tmp_path / "bad.yaml"
        yf.write_text(":\n  - [\n")
        with pytest.warns(UserWarning, match="Failed to load"):
            cfg = AppConfig(yaml_file=yf)
        assert cfg.yaml == {}


class TestAppConfigSubclass:
    def test_subclass_defaults(self):
        class MyConfig(AppConfig):
            CUSTOM_SETTING: str = "default_value"
            CUSTOM_INT: int = 42

        cfg = MyConfig()
        assert cfg.CUSTOM_SETTING == "default_value"
        assert cfg.CUSTOM_INT == 42
        assert cfg.APP_NAME == "App"  # parent defaults still work

    def test_subclass_env_override(self, monkeypatch):
        class MyConfig(AppConfig):
            CUSTOM_SETTING: str = "default"

        monkeypatch.setenv("CUSTOM_SETTING", "from_env")
        cfg = MyConfig()
        assert cfg.CUSTOM_SETTING == "from_env"

    def test_subclass_overrides_parent_default(self):
        class MyConfig(AppConfig):
            APP_NAME: str = "MyApp"

        cfg = MyConfig()
        assert cfg.APP_NAME == "MyApp"


class TestAppConfigGet:
    def test_get_existing_key(self):
        cfg = AppConfig()
        assert cfg.get("APP_NAME") == "App"

    def test_get_missing_key_returns_default(self):
        cfg = AppConfig()
        assert cfg.get("NONEXISTENT") is None
        assert cfg.get("NONEXISTENT", "fallback") == "fallback"


class TestAppConfigFloat:
    def test_float_from_env(self, monkeypatch):
        class MyConfig(AppConfig):
            THRESHOLD: float = 0.5

        monkeypatch.setenv("THRESHOLD", "0.75")
        cfg = MyConfig()
        assert cfg.THRESHOLD == 0.75

    def test_invalid_float_falls_back(self, monkeypatch):
        class MyConfig(AppConfig):
            THRESHOLD: float = 0.5

        monkeypatch.setenv("THRESHOLD", "not_a_float")
        cfg = MyConfig()
        assert cfg.THRESHOLD == 0.5
