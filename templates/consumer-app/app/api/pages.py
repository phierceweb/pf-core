"""__NAME__ routes — the day-1 vertical slice.

One route, proving the app factory + install work. Replace with real routes;
call a service or orchestrator from each handler (no business logic here).
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/")
def index() -> dict:
    return {"app": "__NAME__", "status": "ok"}
