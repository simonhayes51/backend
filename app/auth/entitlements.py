from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Optional, Set, Literal, Dict, Any

import aiohttp
import asyncpg
from fastapi import Request, HTTPException

Feature = Literal[
    "smart_buy",
    "watchlist",
    "trade_finder",
    "deal_confidence",
    "backtest",
    "smart_trending",
]

# ---- Config (overridable via env) -------------------------------------------
FREE_WATCHLIST_MAX = int(os.getenv("WATCHLIST_FREE_MAX", "3"))
PREMIUM_WATCHLIST_MAX = int(os.getenv("WATCHLIST_PREMIUM_MAX", "500"))

FREE_TRENDING = {
    "timeframes": {"24h"},  # free users only see 24h
    "limit": 5,
    "smart": False,
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
    # "watchlist" is treated via limits instead of a hard block
}

# ---- Env needed to query Discord (optional; if missing we just skip) --------
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_SERVER_ID = os.getenv("DISCORD_SERVER_ID")
DISCORD_PREMIUM_ROLE_ID = os.getenv("DISCORD_PREMIUM_ROLE_ID")  # role ID you marked as Premium

# small cache to avoid hammering Discord
_ROLE_CACHE: dict[str, dict] = {}
ROLE_CACHE_TTL = 300  # seconds


def _now() -> datetime:
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
    """
    Reads entitlements from the 'users' table if it exists.
    If the table doesn't exist yet, we swallow the error and return None.
    """
    try:
        return await pool.fetchrow("SELECT plan, premium_until, roles FROM users WHERE id=$1", user_id)
    except Exception:
        return None


async def user_has_premium_role(discord_user_id: str) -> bool:
    """
    True if the Discord member has the 'Premium' role ID in your guild.
    Safe to call even if env is missing (just returns False).
    """
    if not (DISCORD_BOT_TOKEN and DISCORD_SERVER_ID and DISCORD_PREMIUM_ROLE_ID):
        return False

    now = time.time()
    hit = _ROLE_CACHE.get(discord_user_id)
    if hit and (now - hit["at"] < ROLE_CACHE_TTL):
        return bool(hit["ok"])

    ok = False
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://discord.com/api/v10/guilds/{DISCORD_SERVER_ID}/members/{discord_user_id}",
                headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    js = await resp.json()
                    roles = set(str(r) for r in (js.get("roles") or []))
                    ok = DISCORD_PREMIUM_ROLE_ID in roles
    except Exception:
        ok = False

    _ROLE_CACHE[discord_user_id] = {"ok": ok, "at": now}
    return ok


async def compute_entitlements(req: Request) -> Dict[str, Any]:
    """
    Single place that decides what a user can do.
    Sources:
      - session (user_id, roles)
      - users table (plan, premium_until, roles)
      - Discord live role check (fallback)
    """
    sess = req.session or {}
    user_id = sess.get("user_id") or (sess.get("user") or {}).get("id")
    pool: asyncpg.Pool = req.app.state.pool  # provided in app.lifespan

    plan = None
    premium_until = None
    roles: Set[str] = set(sess.get("roles") or [])

    if user_id:
        row = await _load_user_row(pool, user_id)
        if row:
            plan = row["plan"]
            premium_until = row["premium_until"]
            roles |= set(row["roles"] or [])

    # Fallback to live Discord check if we still don't have Premium in roles
    if user_id and "Premium" not in roles:
        try:
            if await user_has_premium_role(user_id):
                roles.add("Premium")
        except Exception:
            pass

    premium = _is_premium(plan, premium_until, roles)

    limits = {
        "watchlist_max": PREMIUM_WATCHLIST_MAX if premium else FREE_WATCHLIST_MAX,
        "trending": PREMIUM_TRENDING if premium else FREE_TRENDING,
    }

    features: Set[Feature] = set()
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
    """
    FastAPI dependency that errors with 402 if the user is not allowed to use the feature.
    """
    async def _dep(req: Request):
        ent = await compute_entitlements(req)

        # Simple pass if they're premium overall
        if ent["is_premium"]:
            return True

        conf = FEATURE_MATRIX.get(feature, {})
        allowed = False

        # Allow by role
        if conf:
            if set(ent["roles"]) & set(conf.get("roles", set())):
                allowed = True

            # Allow by plan
            plan = (ent.get("plan") or "").lower()
            if plan and plan in conf.get("plans", set()):
                allowed = True

        if not allowed:
            raise HTTPException(
                status_code=402,
                detail={
                    "error": "payment_required",
                    "feature": feature,
                    "message": f"{feature.replace('_', ' ').title()} is a premium feature.",
                    "upgrade_url": "/billing",
                },
            )
        return True

    return _dep
