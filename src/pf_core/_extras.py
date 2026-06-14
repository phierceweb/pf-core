"""Friendly errors for missing optional-dependency extras.

The foundation install (``pip install pf-core``) is dependency-light: it does
not ship httpx, pydantic, json-repair, tenacity, or typer. Modules that need
those live behind opt-in extras ([http], [llm], [cli], [jobs], ...). When such
a module is imported without its extra installed, the bare third-party
``ImportError`` ("No module named 'json_repair'") is opaque. This helper turns
it into a message that names the extra and the exact pip command.

Usage at the top of a gated leaf module::

    try:
        import httpx
    except ImportError as e:  # pragma: no cover - exercised by bare-install CI
        from pf_core._extras import extra_import_error

        raise extra_import_error(
            "llm", "httpx", feature="pf_core.clients.openrouter"
        ) from e
"""

from __future__ import annotations

# Extra name -> the pip target a user should install. Anything not listed
# falls back to ``pf-core[<extra>]``.
_INSTALL: dict[str, str] = {
    "http": "pf-core[http]",
    "cli": "pf-core[cli]",
    "validate": "pf-core[validate]",
    "llm": "pf-core[llm]",
    "db": "pf-core[db]",
    "web": "pf-core[web]",
    "jobs": "pf-core[jobs]",
    "tracking": "pf-core[tracking]",
    "eval": "pf-core[eval]",
    "admin": "pf-core[admin]",
    "articles": "pf-core[articles]",
    "jsonschema": "pf-core[jsonschema]",
    "redis": "pf-core[redis]",
    "ratelimit": "pf-core[ratelimit]",
}


def install_target(extra: str) -> str:
    """Return the ``pip install`` target for an extra (e.g. ``pf-core[llm]``)."""
    return _INSTALL.get(extra, f"pf-core[{extra}]")


def extra_import_error(extra: str, package: str, *, feature: str) -> ImportError:
    """Build an ``ImportError`` that names the missing extra and pip command.

    Args:
        extra: The optional-dependency extra that ships ``package`` (e.g. ``"llm"``).
        package: The third-party import name that failed (e.g. ``"json_repair"``).
        feature: The pf-core module or capability the caller was importing, used
            in the message (e.g. ``"pf_core.llm.parse"``).

    Returns:
        An ``ImportError`` to ``raise ... from`` the original failure.
    """
    return ImportError(
        f"{feature} requires the '{extra}' extra; '{package}' is not installed. "
        f"Install it with:  pip install {install_target(extra)}"
    )
