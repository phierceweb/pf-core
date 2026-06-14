"""uvicorn entry point: ``python -m app`` (or ``bin/web``)."""

from __future__ import annotations

import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host=os.environ.get("WEB_HOST", "127.0.0.1"),
        port=int(os.environ.get("WEB_PORT", "8000")),
        reload=bool(os.environ.get("WEB_RELOAD")),
    )
