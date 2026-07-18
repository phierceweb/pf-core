"""Guard against ``pf_core.__version__`` drifting from the package metadata.

``__version__`` derives from the installed distribution metadata — the single
source of truth. Fails if a hand-maintained literal is reintroduced.
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
