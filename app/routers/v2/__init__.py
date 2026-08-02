# app/routers/v2/
from __future__ import annotations

from fastapi import APIRouter

from app.routers.v2.health import router as health_router
from app.routers.v2.players import router as players_router
from app.routers.v2.market import router as market_router
from app.routers.v2.sbc import router as sbc_router
from app.routers.v2.analytics import router as analytics_router
from app.routers.v2.recommendations import router as recommendations_router
from app.routers.v2.dashboard import router as dashboard_router
from app.routers.v2.trades import router as trades_router
from app.routers.v2.ai_chat import router as ai_chat_router
from app.routers.v2.futgg_market import router as futgg_market_router

router = APIRouter(prefix="/api/v2")
router.include_router(health_router)
router.include_router(players_router)
router.include_router(market_router)
router.include_router(sbc_router)
router.include_router(analytics_router)
router.include_router(recommendations_router)
router.include_router(dashboard_router)
router.include_router(trades_router)
router.include_router(ai_chat_router)
# FUT.GG-backed market-intelligence layer (migrations/038) - see that
# router module's own docstring for the full endpoint list and why it's
# ungated. Registered last: purely additive, no path overlap with any
# router above (confirmed against players_router/market_router's own
# path prefixes before adding this).
router.include_router(futgg_market_router)
