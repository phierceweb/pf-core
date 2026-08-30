"""pf-core: a dependency-light Python foundation.

The base install (``pip install pf-core``) is the architectural foundation
only: structured logging, an exception hierarchy, config + env resolvers,
utils, and the ``Service`` base class — five small deps (structlog,
python-dotenv, pyyaml, nanoid, rich), no httpx/pydantic/LLM stack.

Everything else ships as opt-in, orthogonally-composable extras: anti-slop
output guards (``[validate]``), LLM clients (``[llm]`` ⊇ ``[validate]``), HTTP
utils (``[http]``), CLI scaffolding (``[cli]``), and the FastAPI + SQLAlchemy
app framework (``[db]``, ``[web]``, ``[jobs]``, ``[tracking]``, ``[eval]``,
``[admin]``). See ``docs/INSTALLATION.md`` for the extras matrix.
"""


def __getattr__(name: str) -> str:
    """PEP 562: ``__version__`` reads package metadata on first access, not at import."""
    if name != "__version__":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib.metadata import PackageNotFoundError, version

    try:
        value = version("pf-core")
    except PackageNotFoundError:  # running from a source tree with no install
        value = "0.0.0+unknown"
    globals()["__version__"] = value
    return value
