"""Bare-install smoke check — run INSIDE a venv that has only `pip install .`
(no extras) of pf-core. Proves the foundation install is dependency-light and
that LLM/HTTP-gated modules raise a friendly ImportError naming their extra.

Invoked by `bin/verify-bare-install`. Not a pytest test (it must run in the
bare venv, not the dev venv) — the filename intentionally avoids `test_*` so
pytest does not collect it. Exits non-zero on any failure.
"""

import importlib
import sys

failures: list[str] = []


def check(desc, fn):
    try:
        fn()
        print(f"PASS: {desc}")
    except AssertionError as e:
        print(f"FAIL: {desc}: {e}")
        failures.append(desc)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {desc}: {type(e).__name__}: {e}")
        failures.append(desc)


# 1. Foundation modules import cleanly with only base deps installed.
foundation = [
    "pf_core", "pf_core.exceptions", "pf_core.log", "pf_core.config",
    "pf_core.output", "pf_core.parallel", "pf_core.services",
    "pf_core.services.base", "pf_core.utils", "pf_core.utils.env",
    "pf_core.utils.dates", "pf_core.utils.ids", "pf_core.utils.json",
    "pf_core.utils.similarity", "pf_core.utils.vocab",
    # generic JSON-from-messy-text recovery (lives outside pf_core.llm).
    "pf_core.utils.json_recovery",
]
for mod in foundation:
    check(f"import {mod}", lambda m=mod: importlib.import_module(m))

# 2. Heavy deps must be absent in a bare install.
for pkg in ["httpx", "pydantic", "json_repair", "tenacity", "typer"]:
    def _absent(p=pkg):
        try:
            importlib.import_module(p)
        except ImportError:
            return
        raise AssertionError(f"{p} unexpectedly importable")
    check(f"{pkg} NOT installed", _absent)


# 3. Gated modules raise a friendly ImportError naming the extra + pip command.
def _friendly(mod, needle):
    try:
        importlib.import_module(mod)
    except ImportError as e:
        msg = str(e)
        assert "pip install pf-core[" in msg, f"no install hint in: {msg!r}"
        assert needle in msg, f"expected {needle!r} in: {msg!r}"
        return
    raise AssertionError(f"{mod} imported but should have raised")


check("pf_core.llm.parse -> friendly [validate] error",
      lambda: _friendly("pf_core.llm.parse", "pf-core[validate]"))
check("pf_core.llm.validate -> friendly [validate] error",
      lambda: _friendly("pf_core.llm.validate", "pf-core[validate]"))
check("pf_core.clients.openrouter -> friendly [llm] error",
      lambda: _friendly("pf_core.clients.openrouter", "pf-core[llm]"))


# 4. Lazy util re-export raises a friendly [http] error on attribute access.
def _lazy_http():
    import pf_core.utils as u
    try:
        u.check_url  # noqa: B018
    except ImportError as e:
        assert "pf-core[http]" in str(e), str(e)
        return
    raise AssertionError("pf_core.utils.check_url did not raise")


check("pf_core.utils.check_url -> friendly [http] error", _lazy_http)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("ALL BARE-INSTALL CHECKS PASSED")
