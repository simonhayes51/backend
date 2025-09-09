# app/auth/entitlements.py
from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional, Set, Literal, Dict, Any
import os
import asyncpg
from fastapi import Request, HTTPException

Feature = Literal["smart_buy", "watchlist", "trade_finder", "deal_confidence", "backtest", "smart_trending"]

# ---- Configurable knobs (env overridable) ----
FREE_WATCHLIST_MAX = int(os.getenv("WATCHLIST_FREE_MAX", "3"))  # set to 1 if you prefer
PREMIUM_WATCHLIST_MAX = int(os.getenv("WATCHLIST_PREMIUM_MAX", "500"))

FREE_TRENDING = {
    "timeframes": {"24h"},   # free: only 24h
    "limit": 5,              # free: top 5
    "smart": False,          # free: no Smart tab
}
PREMIUM_TRENDING = {
    "timeframes": {"4h", "6h", "24h"},
    "limit": 20,
    "smart": True,
}

FEATURE_MATRIX: Dict[Feature, Dict[str, Any]] = {
    "smart_buy":       {"roles": {"Premium"}, "plans": {"pro", "premium"}},
    "trade_finder":    {"roles": {"Premium"}, "plans": {"pro", "premium"}},
    "deal_confidence": {"roles": {"Premium"}, "plans": {"pro", "premium"}},
    "backtest":        {"roles": {"Premium"}, "plans": {"pro", "premium"}},
    "smart_trending":  {"roles": {"Premium"}, "plans": {"pro", "premium"}},
    # "watchlist" is handled via limits instead of a hard block
}

def _now():
  return datetime.now(timezone.utc)

def _is_premium(plan: Optional[str], premium_until: Optional[datetime], roles: Set[str]) -> bool:
  if plan and plan.lower() in {"pro", "premium"}:
    return True
  if premium_until and premium_until > _now():
    return True
  if "Premium" in roles:
    return True
  return False

async def _load_user_row(pool: asyncpg.Pool, user_id: str) -> Optional[asyncpg.Record]:
  return await pool.fetchrow("SELECT plan, premium_until, roles FROM users WHERE id=$1", user_id)

async def compute_entitlements(req: Request) -> Dict[str, Any]:
  user = (req.session or {}).get("user") or {}
  user_id = user.get("id")
  pool: asyncpg.Pool = req.app.state.pool

  plan = None; premium_until = None; roles: Set[str] = set()
  if user_id:
    row = await _load_user_row(pool, user_id)
    if row:
      plan = row["plan"]
      premium_until = row["premium_until"]
      roles = set(row["roles"] or [])

  premium = _is_premium(plan, premium_until, roles)

  limits = {
    "watchlist_max": PREMIUM_WATCHLIST_MAX if premium else FREE_WATCHLIST_MAX,
    "trending": PREMIUM_TRENDING if premium else FREE_TRENDING,
  }

  features = set()
  if premium:
    features = {"smart_buy", "trade_finder", "deal_confidence", "backtest", "smart_trending"}

  return {
    "user_id": user_id,
    "plan": plan,
    "premium_until": premium_until,
    "roles": list(roles),
    "is_premium": premium,
    "features": list(features),
    "limits": limits,
  }

def require_feature(feature: Feature):
  async def _dep(req: Request):
    ent = await compute_entitlements(req)
    if ent["is_premium"]:
      return True
    conf = FEATURE_MATRIX.get(feature, {})
    allowed = False
    if conf:
      if set(ent["roles"]) & set(conf.get("roles", set())):
        allowed = True
      if ent["plan"] and ent["plan"].lower() in conf.get("plans", set()):
        allowed = True
    if not allowed:
      raise HTTPException(
        status_code=402,
        detail={
          "error": "payment_required",
          "feature": feature,
          "message": f"{feature.replace('_',' ').title()} is a premium feature.",
          "upgrade_url": "/billing",
        },
      )
    return True
  return _dep
