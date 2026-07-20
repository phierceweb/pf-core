"""Shared utility modules.

Most utils are pure-stdlib and re-exported eagerly below. The URL-fetching
helpers (``url_liveness``, ``urls``) depend on httpx ([http] extra), so they
are re-exported **lazily** via :pep:`562` ``__getattr__`` — importing
``pf_core.utils`` (or any submodule like ``pf_core.utils.dates``) must not
drag httpx into the dependency-light foundation install. Accessing one of the
lazy names triggers the import, which raises a friendly error if [http] is
missing.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

# Eager: pure-stdlib re-exports, no third-party deps.
from pf_core.utils.similarity import (  # noqa: F401
    is_near_duplicate,
    jaccard,
    shingle,
)
from pf_core.utils.json import (  # noqa: F401
    canonical_json,
    safe_json_col,
    safe_json_loads,
)
from pf_core.utils.hashing import content_hash  # noqa: F401
from pf_core.utils.slugify import slugify  # noqa: F401
from pf_core.utils.throttle import Throttle  # noqa: F401
from pf_core.utils.vocab import SlugNormalizer  # noqa: F401

# Lazy: these names live in httpx-backed modules ([http] extra). Mapping name
# -> submodule it lives in.
_LAZY: dict[str, str] = {
    "CacheBackend": "pf_core.utils.url_liveness",
    "check_url_cached": "pf_core.utils.url_liveness",
    "archive_timestamp_is_round": "pf_core.utils.urls",
    "check_url": "pf_core.utils.urls",
    "domain_of": "pf_core.utils.urls",
}


def __getattr__(name: str) -> object:
    """PEP 562 hook: import httpx-backed re-exports on first access."""
    module_path = _LAZY.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(module_path)  # may raise friendly ImportError
    return getattr(module, name)


def __dir__() -> list[str]:
    return sorted([*globals(), *_LAZY])


if TYPE_CHECKING:  # keep static analysers / IDEs aware of the lazy names
    from pf_core.utils.url_liveness import CacheBackend, check_url_cached  # noqa: F401
    from pf_core.utils.urls import archive_timestamp_is_round, check_url, domain_of  # noqa: F401
