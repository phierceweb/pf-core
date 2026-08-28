"""DDL collection for the ``pf_tables`` fixture."""

from __future__ import annotations

import re
from collections.abc import Iterable

import pytest

_CREATE_OBJECT_RE = re.compile(
    r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:TEMP(?:ORARY)?\s+)?(?:UNIQUE\s+)?"
    r"(TABLE|INDEX|VIEW)\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"`\[]?([\w.]+)",
    re.IGNORECASE,
)


def created_objects(statements: Iterable[str]) -> set[tuple[str, str]]:
    """The ``(kind, name)`` pairs *statements* create, lowercased."""
    return {
        (match.group(1).lower(), match.group(2).lower())
        for stmt in statements
        for match in _CREATE_OBJECT_RE.finditer(stmt)
    }


_DROP_OBJECT_RE = re.compile(
    r"\bDROP\s+(TABLE|INDEX|VIEW)\s+(?:IF\s+EXISTS\s+)?[\"`\[]?([\w.]+)",
    re.IGNORECASE,
)


def dropped_objects(statements: Iterable[str]) -> set[tuple[str, str]]:
    """The ``(kind, name)`` pairs *statements* drop, lowercased."""
    return {
        (match.group(1).lower(), match.group(2).lower())
        for stmt in statements
        for match in _DROP_OBJECT_RE.finditer(stmt)
    }


def schema_ddl(request) -> list[str]:
    """DDL from the ``pf_schema`` fixture, or ``[]`` when none is defined."""
    try:
        value = request.getfixturevalue("pf_schema")
    except pytest.FixtureLookupError as exc:
        # Only a missing pf_schema is optional; a lookup failure for something
        # pf_schema itself requires is a real error.
        if exc.argname != "pf_schema":
            raise
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return list(value)
    return []


def marker_ddl(request) -> list[str]:
    """DDL from the ``@pytest.mark.pf_tables(...)`` marker closest to the test."""
    marker = request.node.get_closest_marker("pf_tables")
    if marker is None:
        return []
    statements: list[str] = []
    for arg in marker.args:
        if isinstance(arg, str):
            statements.append(arg)
        elif isinstance(arg, (list, tuple)):
            statements.extend(arg)
    return statements
