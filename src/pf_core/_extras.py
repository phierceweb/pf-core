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


# Module prefix -> the extra needed to import it. Longest prefix wins, so a
# subpackage may sit above its parent. Unlisted means base install: no extras.
# Enforced against the source tree by tests/test_extras_tiers.py.
_MODULE_EXTRA: dict[str, str] = {
    "pf_core.alembic": "db",
    "pf_core.budget._schema": "tracking",
    "pf_core.budget.audit": "tracking",
    "pf_core.budget.repo": "tracking",
    "pf_core.budget.scheduler": "tracking",
    "pf_core.budget.snapshot_job": "tracking",
    "pf_core.cli": "cli",
    "pf_core.cli.jobs": "jobs",
    "pf_core.clients.anthropic": "anthropic",
    "pf_core.clients.brave": "llm",
    "pf_core.clients.openrouter": "llm",
    "pf_core.db": "db",
    "pf_core.eval": "eval",
    # Minimum tier to import, not the extra you'd install for the feature:
    # jobs modules import under [tracking]; only the CLI needs typer.
    "pf_core.jobs": "tracking",
    "pf_core.llm.cache": "tracking",
    "pf_core.llm.parse": "validate",
    "pf_core.llm.step": "tracking",
    "pf_core.llm.tracked": "tracking",
    "pf_core.llm.tracking": "tracking",
    "pf_core.llm.validate": "validate",
    # [crawl] = [http,articles]: article_fetch imports utils.urls, which needs httpx.
    "pf_core.utils.article_fetch": "crawl",
    "pf_core.utils.phash": "image-phash",
    "pf_core.utils.url_liveness": "http",
    "pf_core.utils.urls": "http",
    "pf_core.web": "web",
    # Both dashboards need [web] + a DB tier; [admin] supplies both.
    "pf_core.web.jobs_admin": "admin",
    "pf_core.web.llm_admin": "admin",
}


def required_extra(module: str) -> str | None:
    """The extra needed to import *module*, or ``None`` if it is base-install.

    Longest matching prefix wins, so ``pf_core.llm.tracking.repo`` resolves to
    ``tracking`` rather than to whatever ``pf_core.llm`` would give.
    """
    best: str | None = None
    best_len = -1
    for prefix, extra in _MODULE_EXTRA.items():
        if (module == prefix or module.startswith(prefix + ".")) and len(prefix) > best_len:
            best, best_len = extra, len(prefix)
    return best


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
