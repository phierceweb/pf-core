"""Every admin router factory must refuse to mount without auth.

`make_jobs_router` shipped open-by-default for eight minor releases while its
sibling `make_admin_router` raised, because nothing checked the class. This
walks `pf_core.web` and asserts the invariant for any factory added later.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

import pytest

pytest.importorskip("fastapi")

from fastapi import APIRouter  # noqa: E402

import pf_core.web  # noqa: E402
from pf_core.exceptions import ConfigurationError  # noqa: E402

# Routers that are public on purpose. `health_router` is a read-only
# `GET /health` returning ok/error/skipped with no detail, and takes no auth
# parameter at all.
PUBLIC_BY_DESIGN = {"pf_core.web.health.health_router"}


def _router_factories() -> list[tuple[str, object]]:
    found = []
    for info in pkgutil.walk_packages(pf_core.web.__path__, "pf_core.web."):
        try:
            module = importlib.import_module(info.name)
        except ImportError:  # optional extra not installed
            continue
        for name, obj in vars(module).items():
            if name.startswith("_") or not inspect.isfunction(obj):
                continue
            if obj.__module__ != info.name:
                continue
            if inspect.signature(obj).return_annotation in (APIRouter, "APIRouter"):
                found.append((f"{info.name}.{name}", obj))
    return sorted(found)


def test_finds_the_known_factories():
    names = {name for name, _ in _router_factories()}
    assert {
        "pf_core.web.jobs_admin.make_jobs_router",
        "pf_core.web.llm_admin.make_admin_router",
    } <= names, f"router discovery broke — found only {names}"


def test_allowlist_entries_still_exist():
    names = {name for name, _ in _router_factories()}
    stale = PUBLIC_BY_DESIGN - names
    assert not stale, f"PUBLIC_BY_DESIGN names factories that no longer exist: {stale}"


@pytest.mark.parametrize(
    "name, factory",
    [(n, f) for n, f in _router_factories() if n not in PUBLIC_BY_DESIGN],
)
def test_refuses_to_mount_without_auth(name, factory):
    with pytest.raises(ConfigurationError):
        factory()
