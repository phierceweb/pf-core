"""Shared result vocabulary for ``pf-doctor`` checks."""

from __future__ import annotations

import re
from dataclasses import dataclass

_SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD", re.IGNORECASE)
_URL_CREDS = re.compile(r"^([a-z0-9+.-]+://)[^@/]+@", re.IGNORECASE)


@dataclass(frozen=True)
class CheckResult:
    group: str
    name: str
    status: str  # PASS | WARN | FAIL | SKIP
    detail: str


def redact_value(name: str, value: str) -> str:
    """Redact secrets: key-like vars to presence-only, URL credentials masked."""
    if _SECRET_NAME.search(name):
        return "set (redacted)"
    return _URL_CREDS.sub(r"\1***@", value)
