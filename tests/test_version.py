"""Guard against ``pf_core.__version__`` drifting from the package metadata.

The version was hardcoded as a literal in ``pf_core/__init__.py`` for a long
time and silently fell out of sync with ``pyproject.toml`` on nearly every
release (see CHANGELOG v0.22, v0.23, v0.36). It's now derived from the
installed distribution metadata so there is a single source of truth. This
test fails if anyone reintroduces a hand-maintained literal.
"""
from __future__ import annotations

from importlib.metadata import version

import pf_core


def test_version_matches_installed_distribution_metadata():
    assert pf_core.__version__ == version("pf-core")


def test_version_is_not_the_unknown_fallback():
    # In the test environment pf-core is always installed, so the
    # PackageNotFoundError fallback must not be what we resolved to.
    assert pf_core.__version__ != "0.0.0+unknown"
