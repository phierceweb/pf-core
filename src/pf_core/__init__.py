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

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("pf-core")
except PackageNotFoundError:  # running from a source tree with no install
    __version__ = "0.0.0+unknown"
