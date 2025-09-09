# app/auth/entitlements.py
from datetime import datetime, timezone
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel
import asyncpg
from typing import Set, Optional, Literal

Feature = Literal["smart_buy", "trade_finder", "deal_confidence", "backtest"]

FEATURE_MATRIX: dict[Feature, dict] = {
    "smart_buy":       {"roles": {"Premium"}, "plans": {"pro", "premium"}},
    "trade_finder":    {"roles": {"Premium"}, "plans": {"pro", "premium"}},
    "deal_confidence": {"roles": {"Premium"}, "plans": {"pro", "premium"}},
    "backtest":        {"roles": {"Premium"}, "plans": {"pro", "premium"}},
}

class Entitlements(BaseModel):
    user_id: str
    plan: Optional[str] = None
    premium_until: Optional[datetime] = None
    roles: Set[str] = set()
    features: Set[str] = set()

async def _load_user_row(pool: asyncpg.Pool, user_id: str) -> Optional[asyncpg.Record]:
    return await pool.fetchrow(
        """SELECT id, plan, premium_until, roles
           FROM users WHERE id=$1""",
        user_id
    )

def _is_premium(plan: Optional[str], premium_until: Optional[datetime], roles: Set[str]) -> bool:
    now = datetime.now(timezone.utc)
    if plan and plan.lower() in {"pro", "premium"}: 
        return True
    if premium_until and premium_until > now:
        return True
    if "Premium" in roles:
        return True
    return False

async def require_feature(
    feature: Feature,
):
    async def _dep(req: Request):
        # 1) session/user
        user = (req.session or {}).get("user") or {}
        user_id = user.get("id")
        if not user_id:
            # unauthenticated
            raise HTTPException(status_code=401, detail={"error":"unauthenticated"})

        pool: asyncpg.Pool = req.app.state.pool

        # 2) DB lookup
        row = await _load_user_row(pool, user_id)
        plan = (row and row.get("plan")) or None
        premium_until = (row and row.get("premium_until")) or None
        roles = set((row and row.get("roles") or []) or [])

        # 3) compute entitlements
        is_premium = _is_premium(plan, premium_until, roles)
        allowed = False
        if is_premium:
            allowed = True
        else:
            conf = FEATURE_MATRIX.get(feature, {})
            # You could also offer per-feature overrides if needed:
            allowed = bool(conf and (roles & conf.get("roles", set())))

        if not allowed:
            # 402 Payment Required with actionable payload
            raise HTTPException(
                status_code=402,
                detail={
                    "error": "payment_required",
                    "feature": feature,
                    "upgrade_url": "/billing",
                    "message": f"{feature.replace('_',' ').title()} is a premium feature.",
                },
            )
        return True
    return _dep
