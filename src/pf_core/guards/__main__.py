"""``python -m pf_core.guards`` -> run the structural gate."""
from __future__ import annotations

import sys

from pf_core.guards.structure import run_cli

if __name__ == "__main__":
    sys.exit(run_cli())
