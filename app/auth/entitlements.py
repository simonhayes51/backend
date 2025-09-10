# app/auth/entitlements.py
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Optional, Set, Literal, Dict, Any

import aiohttp
from fastapi import Request, HTTPException

Feature = Literal[
    "smart_buy",
    "watchlist",
    "trade_finder",
    "deal_confidence",
    "backtest",
    "smart_trending",
]

# ---------- Env / Config ----------
FREE_WATCHLIST_MAX = int(os.getenv("WATCHLIST_FREE_MAX", "3"))
PREMIUM_WATCHLIST_MAX = int(os.getenv("WATCHLIST_PREMIUM_MAX", "500"))

FREE_TRENDING = {
    "timeframes": {"24h"},     # free: only 24h
    "limit": 5,                # free: top 5
    "smart": False,            # free: no Smart tab
}
PREMIUM_TRENDING = {
    "timeframes": {"6h", "12h", "24h"},  # keep aligned with backend support
    "limit": 20,
    "smart": True,
}

FEATURE_MATRIX: Dict[Feature, Dict[str, Any]] = {
    "smart_buy":       {"roles": {"Premium"}, "plans": {"pro", "premium"}},
    "trade_finder":    {"roles": {"Premium"}, "plans": {"pro", "premium"}},
    "deal_confidence": {"roles": {"Premium"}, "plans": {"pro", "premium"}},
    "backtest":        {"roles": {"Premium"}, "plans": {"pro", "premium"}},
    "smart_trending":  {"roles": {"Premium"}, "plans": {"pro", "premium"}},
    # "watchlist" is governed by limits instead of a hard block
}

# ---------- Discord role check (cached) ----------
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_SERVER_ID = os.getenv("DISCORD_SERVER_ID")
DISCORD_PREMIUM_ROLE_ID = os.getenv("DISCORD_PREMIUM_ROLE_ID")

_ROLE_CACHE: Dict[str, Dict[str, Any]] = {}
ROLE_CACHE_TTL = int(os.getenv("ROLE_CACHE_TTL", "300"))  # seconds

async def _has_discord_premium_role(user_id: str) -> bool:
    """
    True if the Discord user has the configured Premium role in the configured guild.
    Caches results briefly to avoid hammering Discord.
    """
    if not (DISCORD_BOT_TOKEN and DISCORD_SERVER_ID and DISCORD_PREMIUM_ROLE_ID and user_id):
        return False

    now = time.time()
    hit = _ROLE_CACHE.get(user_id)
    if hit and (now - hit["at"] < ROLE_CACHE_TTL):
        return bool(hit["ok"])

    ok = False
    try:
        async with aiohttp.ClientSession(
            headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
        ) as sess:
            async with sess.get(
                f"https://discord.com/api/v10/guilds/{DISCORD_SERVER_ID}/members/{user_id}"
            ) as resp:
                if resp.status == 200:
                    js = await resp.json()
                    roles = js.get("roles") or []
                    ok = DISCORD_PREMIUM_ROLE_ID in roles
    except Exception:
        ok = False

    _ROLE_CACHE[user_id] = {"ok": ok, "at": now}
    return ok

# ---------- Helpers ----------
def _now() -> datetime:
    return datetime.now(timezone.utc)

def _is_premium_by_discord(roles_has_premium: bool) -> bool:
    return bool(roles_has_premium)

# ---------- Public API ----------
async def compute_entitlements(req: Request) -> Dict[str, Any]:
    """
    Determines the user's entitlements. Premium is granted if the user has the Discord
    role whose ID is DISCORD_PREMIUM_ROLE_ID in DISCORD_SERVER_ID.
    """
    # Your auth sets these in session during /api/callback
    user_id = (req.session or {}).get("user_id")

    # Default (no DB dependency)
    plan: Optional[str] = None
    premium_until: Optional[datetime] = None

    # Check Discord role
    has_premium_role = await _has_discord_premium_role(user_id) if user_id else False
    is_premium = _is_premium_by_discord(has_premium_role)

    # Surface a friendly role name so FEATURE_MATRIX role checks still work
    roles: Set[str] = {"Premium"} if has_premium_role else set()

    limits = {
        "watchlist_max": PREMIUM_WATCHLIST_MAX if is_premium else FREE_WATCHLIST_MAX,
        "trending": PREMIUM_TRENDING if is_premium else FREE_TRENDING,
    }

    features = {
        "smart_buy",
        "trade_finder",
        "deal_confidence",
        "backtest",
        "smart_trending",
    } if is_premium else set()

    return {
        "user_id": user_id,
        "plan": plan,
        "premium_until": premium_until,
        "roles": list(roles),
        "is_premium": is_premium,
        "features": list(features),
        "limits": limits,
    }

def require_feature(feature: Feature):
    """
    FastAPI dependency to guard routes:
      app.include_router(
          smart_buy_router,
          dependencies=[Depends(require_feature("smart_buy"))],
      )
    """
    async def _dep(req: Request):
        ent = await compute_entitlements(req)
        if ent["is_premium"]:
            return True

        conf = FEATURE_MATRIX.get(feature, {})
        allowed = False
        if conf:
            # role name "Premium" is injected when Discord role is present
            if set(ent["roles"]) & set(conf.get("roles", set())):
                allowed = True
            if ent.get("plan") and str(ent["plan"]).lower() in conf.get("plans", set()):
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
