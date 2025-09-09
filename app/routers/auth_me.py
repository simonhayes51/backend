# app/routers/auth_me.py
from fastapi import APIRouter, Request
from pydantic import BaseModel
from datetime import datetime, timezone
from typing import Set

router = APIRouter(prefix="/api/auth", tags=["auth"])

class MeOut(BaseModel):
    user_id: str | None
    username: str | None
    plan: str | None
    premium_until: datetime | None
    roles: Set[str] = set()
    is_premium: bool
    features: Set[str] = set()

@router.get("/me", response_model=MeOut)
async def me(req: Request):
    user = (req.session or {}).get("user") or {}
    user_id = user.get("id")
    username = user.get("username") or user.get("global_name")

    pool = req.app.state.pool
    plan = None
    premium_until = None
    roles = set()

    if user_id:
        row = await pool.fetchrow("SELECT plan, premium_until, roles FROM users WHERE id=$1", user_id)
        if row:
            plan = row["plan"]
            premium_until = row["premium_until"]
            roles = set(row["roles"] or [])

    # reuse the helper logic
    from app.auth.entitlements import _is_premium
    is_premium = _is_premium(plan, premium_until, roles)
    features = set()
    if is_premium:
        features = {"smart_buy","trade_finder","deal_confidence","backtest"}

    return MeOut(
        user_id=user_id,
        username=username,
        plan=plan,
        premium_until=premium_until,
        roles=roles,
        is_premium=is_premium,
        features=features,
    )
