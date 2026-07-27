# app/routers/v2/health.py
#
# Placeholder so /api/v2/* exists and is mounted before any real v2
# feature router lands - lets the new frontend/deploy confirm CORS,
# session-cookie sharing, and routing all work end to end before
# Phase 1 adds anything that reads real data.
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["v2"])


@router.get("/health")
async def health() -> dict:
    return {"ok": True, "version": "v2"}
