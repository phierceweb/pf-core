"""Tests for pf_core.utils.env — env-var resolver helpers."""

from __future__ import annotations

import logging

import pytest

from pf_core.utils.env import (
    resolve_bool,
    resolve_int,
    resolve_positive_int,
    resolve_str,
)


# ---------------------------------------------------------------------------
# resolve_int
# ---------------------------------------------------------------------------


class TestResolveInt:
    def test_explicit_arg_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("MY_VAR", "99")
        assert resolve_int(7, "MY_VAR", default=1) == 7

    def test_explicit_arg_wins_even_if_zero(self, monkeypatch):
        """Zero is a valid explicit value (not None) — must beat env."""
        monkeypatch.setenv("MY_VAR", "99")
        assert resolve_int(0, "MY_VAR", default=1) == 0

    def test_env_used_when_arg_is_none(self, monkeypatch):
        monkeypatch.setenv("MY_VAR", "42")
        assert resolve_int(None, "MY_VAR", default=1) == 42

    def test_default_used_when_neither_set(self, monkeypatch):
        monkeypatch.delenv("MY_VAR", raising=False)
        assert resolve_int(None, "MY_VAR", default=5) == 5

    def test_negative_int_in_env_works(self, monkeypatch):
        monkeypatch.setenv("MY_VAR", "-3")
        assert resolve_int(None, "MY_VAR", default=1) == -3

    def test_zero_in_env_works(self, monkeypatch):
        """Zero string ``"0"`` parses fine — distinct from "unset"."""
        monkeypatch.setenv("MY_VAR", "0")
        assert resolve_int(None, "MY_VAR", default=99) == 0

    def test_malformed_env_falls_back_to_default(self, monkeypatch, caplog):
        monkeypatch.setenv("MY_VAR", "not-an-int")
        with caplog.at_level(logging.WARNING, logger="pf_core.utils.env"):
            assert resolve_int(None, "MY_VAR", default=42) == 42
        # A warning was emitted naming the var and the bad value
        assert any(
            "env_var_malformed" in r.getMessage() and "MY_VAR" in r.getMessage()
            for r in caplog.records
        )

    def test_empty_string_env_falls_back_to_default(self, monkeypatch, caplog):
        """Empty-string env value isn't a valid int — fall back to default,
        warn so operators don't silently lose their intended override."""
        monkeypatch.setenv("MY_VAR", "")
        with caplog.at_level(logging.WARNING, logger="pf_core.utils.env"):
            assert resolve_int(None, "MY_VAR", default=7) == 7
        assert any("env_var_malformed" in r.getMessage() for r in caplog.records)

    def test_whitespace_in_env_stripped_before_parse(self, monkeypatch):
        """Operators sometimes paste env values with trailing whitespace
        (especially via CI / shell substitution). Strip before parsing."""
        monkeypatch.setenv("MY_VAR", "  42  ")
        assert resolve_int(None, "MY_VAR", default=1) == 42


# ---------------------------------------------------------------------------
# resolve_str
# ---------------------------------------------------------------------------


class TestResolveStr:
    def test_explicit_arg_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("MY_VAR", "from-env")
        assert resolve_str("explicit", "MY_VAR", default="fallback") == "explicit"

    def test_env_used_when_arg_is_none(self, monkeypatch):
        monkeypatch.setenv("MY_VAR", "from-env")
        assert resolve_str(None, "MY_VAR", default="fallback") == "from-env"

    def test_default_used_when_neither_set(self, monkeypatch):
        monkeypatch.delenv("MY_VAR", raising=False)
        assert resolve_str(None, "MY_VAR", default="fallback") == "fallback"

    def test_default_can_be_none(self, monkeypatch):
        """Optional values: a ``None`` default + unset env returns ``None``,
        not the empty string. Lets callers distinguish "not configured"."""
        monkeypatch.delenv("MY_VAR", raising=False)
        assert resolve_str(None, "MY_VAR") is None

    def test_empty_string_env_is_treated_as_set(self, monkeypatch):
        """``os.environ["X"] = ""`` is *set* per OS semantics — preserve
        that. If a caller wants empty-as-unset, they can check explicitly."""
        monkeypatch.setenv("MY_VAR", "")
        assert resolve_str(None, "MY_VAR", default="fallback") == ""

    def test_explicit_empty_string_arg_wins(self, monkeypatch):
        """Explicit ``""`` is a real value — beats env and default."""
        monkeypatch.setenv("MY_VAR", "from-env")
        assert resolve_str("", "MY_VAR", default="fallback") == ""

    def test_default_default_is_none(self, monkeypatch):
        monkeypatch.delenv("MY_VAR", raising=False)
        # Signature: default=None
        assert resolve_str(None, "MY_VAR") is None


# ---------------------------------------------------------------------------
# resolve_bool
# ---------------------------------------------------------------------------


class TestResolveBool:
    def test_resolve_bool_explicit_wins(self, monkeypatch):
        monkeypatch.setenv("FOO_ENABLED", "false")
        assert resolve_bool(True, "FOO_ENABLED", default=False) is True

    def test_resolve_bool_explicit_false_wins(self, monkeypatch):
        """Explicit False (not None) must win over env — `False` is not 'unset'."""
        monkeypatch.setenv("FOO_ENABLED", "true")
        assert resolve_bool(False, "FOO_ENABLED", default=True) is False

    def test_resolve_bool_env_used_when_no_explicit(self, monkeypatch):
        for raw in ("1", "true", "True", "TRUE", "yes", "on", " true "):
            monkeypatch.setenv("FOO_ENABLED", raw)
            assert resolve_bool(None, "FOO_ENABLED", default=False) is True, raw

    def test_resolve_bool_env_falsy_values(self, monkeypatch):
        for raw in ("0", "false", "False", "no", "off", " no "):
            monkeypatch.setenv("FOO_ENABLED", raw)
            assert resolve_bool(None, "FOO_ENABLED", default=True) is False, raw

    def test_resolve_bool_default_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("FOO_ENABLED", raising=False)
        assert resolve_bool(None, "FOO_ENABLED", default=True) is True
        assert resolve_bool(None, "FOO_ENABLED", default=False) is False

    def test_resolve_bool_malformed_falls_back_to_default(self, monkeypatch, caplog):
        monkeypatch.setenv("FOO_ENABLED", "maybe")
        with caplog.at_level("WARNING"):
            assert resolve_bool(None, "FOO_ENABLED", default=False) is False
        assert any("env_var_malformed" in r.message for r in caplog.records)

    def test_empty_string_env_falls_back_to_default(self, monkeypatch, caplog):
        """Empty string is not in _TRUTHY or _FALSY → warns + falls back."""
        monkeypatch.setenv("FOO_ENABLED", "")
        with caplog.at_level(logging.WARNING, logger="pf_core.utils.env"):
            assert resolve_bool(None, "FOO_ENABLED", default=True) is True
        assert any("env_var_malformed" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# resolve_positive_int
# ---------------------------------------------------------------------------


class TestResolvePositiveInt:
    def test_explicit_arg_wins(self, monkeypatch):
        monkeypatch.setenv("WORKERS", "99")
        assert resolve_positive_int(8, "WORKERS", default=4) == 8

    def test_explicit_below_min_raises(self):
        """An out-of-range explicit arg is a caller bug — fail fast."""
        with pytest.raises(ValueError, match="must be >= 1"):
            resolve_positive_int(0, "WORKERS", default=4)

    def test_reads_env(self, monkeypatch):
        monkeypatch.setenv("WORKERS", "12")
        assert resolve_positive_int(None, "WORKERS", default=4) == 12

    def test_env_below_min_warns_and_defaults(self, monkeypatch, caplog):
        """An out-of-range *env* value is an operator typo — warn, don't crash."""
        monkeypatch.setenv("WORKERS", "-3")
        with caplog.at_level(logging.WARNING, logger="pf_core.utils.env"):
            assert resolve_positive_int(None, "WORKERS", default=4) == 4
        assert any(
            "env_var_out_of_range" in r.getMessage() and "WORKERS" in r.getMessage()
            for r in caplog.records
        )

    def test_malformed_env_defaults(self, monkeypatch):
        monkeypatch.setenv("WORKERS", "not-an-int")
        assert resolve_positive_int(None, "WORKERS", default=4) == 4

    def test_custom_min(self, monkeypatch):
        monkeypatch.setenv("CHUNK", "0")
        assert resolve_positive_int(None, "CHUNK", default=50, min_value=1) == 50
        assert resolve_positive_int(2, "CHUNK", default=50, min_value=2) == 2
        with pytest.raises(ValueError):
            resolve_positive_int(1, "CHUNK", default=50, min_value=2)
