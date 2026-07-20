"""Tests for pf_core.utils.slugify."""

from __future__ import annotations

import pytest

from pf_core.utils.slugify import slugify


def test_basic_lowercase_and_separator():
    assert slugify("Hello World") == "hello-world"


def test_punctuation_runs_collapse_to_one_sep():
    assert slugify("rock 'n' roll") == "rock-n-roll"
    assert slugify("TCP/IP") == "tcp-ip"
    assert slugify("hello -- world!!") == "hello-world"


def test_digits_preserved():
    assert slugify("Track 01") == "track-01"


def test_nfkd_diacritics_fold_to_ascii():
    assert slugify("São Paulo") == "sao-paulo"
    assert slugify("Crème brûlée") == "creme-brulee"
    assert slugify("café") == "cafe"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Søren", "soren"),          # ø
        ("Ångström", "angstrom"),    # å (+ ö via NFKD)
        ("Æon", "aeon"),             # æ
        ("œuf", "oeuf"),             # œ
        ("Sigurður", "sigurdur"),    # ð
        ("Þórr", "thorr"),           # þ
        ("Łukasz", "lukasz"),        # ł
        ("Straße", "strasse"),       # ß
    ],
)
def test_special_letter_map(raw, expected):
    assert slugify(raw) == expected


def test_unmapped_non_ascii_drops():
    assert slugify("北京 café") == "cafe"


def test_leading_trailing_junk_trimmed():
    assert slugify("  --Hello--  ") == "hello"


def test_custom_separator():
    assert slugify("Hello World", sep="_") == "hello_world"


def test_empty_and_symbol_only_return_empty():
    assert slugify("") == ""
    assert slugify("!!!") == ""
    assert slugify("★☆★") == ""


def test_idempotent():
    for raw in ("Crème brûlée", "rock 'n' roll", "Track 01", "Søren"):
        once = slugify(raw)
        assert slugify(once) == once


def test_reexported_from_utils_namespace():
    from pf_core.utils import slugify as reexported

    assert reexported is slugify
