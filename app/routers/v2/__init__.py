# app/routers/v2/
#
# FutHub v2 (AI Market Intelligence Platform) lives here as versioned
# routers on the SAME FastAPI app - not a second backend service. See
# the v2 plan for the reasoning: session auth, the three DB pools,
# Stripe billing, and entitlements are already generic, working
# dependencies here, so a separate service would just duplicate them
# for zero isolation benefit.
#
# `router` is the single aggregate router main.py mounts. Each v2
# feature area (sbc, recommendations, analytics, ...) adds its own
# module in this package and gets included here, not in main.py
# directly - main.py only ever gets the one `include_router(v2_router)`
# line.
from __future__ import annotations

from fastapi import APIRouter

from app.routers.v2.health import router as health_router

router = APIRouter(prefix="/api/v2")
router.include_router(health_router)
