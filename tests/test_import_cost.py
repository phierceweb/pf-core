"""The foundation helpers a CLI reaches for first load no logging stack and no package
metadata until something logs or asks for ``__version__``."""

import subprocess
import sys

HEAVY = ("structlog", "rich", "asyncio", "importlib.metadata")


def _loaded(statement: str) -> list[str]:
    code = f"import sys; {statement}; print(' '.join(m for m in {HEAVY!r} if m in sys.modules))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    return out.stdout.split()


def test_utils_io_loads_no_logging_stack():
    assert _loaded("import pf_core.utils.io") == []


def test_utils_json_loads_no_logging_stack():
    assert _loaded("import pf_core.utils.json") == []


def test_each_logger_loads_on_first_use():
    assert "structlog" in _loaded(
        "from pf_core.utils.json import safe_json_loads; safe_json_loads('{bad', label='t')"
    )
    assert "structlog" in _loaded("import pf_core.utils.io as io; io._logger().debug('probe')")


def test_version_resolves_on_access():
    assert "importlib.metadata" in _loaded("import pf_core; pf_core.__version__")
