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
    "portfolio_optimizer",
    "ai_copilot",
    "market_sentiment",
    "market_maker",
    "advanced_analytics",
    "leaderboard",
    "referrals",
    "bulk_trades",
]

# 3-tier subscription system
Tier = Literal["basic", "pro", "elite"]

# ---- Config (overridable via env) -------------------------------------------
# Watchlist limits by tier
BASIC_WATCHLIST_MAX = int(os.getenv("WATCHLIST_BASIC_MAX", "3"))
PRO_WATCHLIST_MAX = int(os.getenv("WATCHLIST_PRO_MAX", "25"))
ELITE_WATCHLIST_MAX = int(os.getenv("WATCHLIST_ELITE_MAX", "500"))

# Legacy support
FREE_WATCHLIST_MAX = BASIC_WATCHLIST_MAX
PREMIUM_WATCHLIST_MAX = ELITE_WATCHLIST_MAX

# Trending limits by tier
BASIC_TRENDING = {
    "timeframes": {"24h"},
    "limit": 5,
    "smart": False,
}
PRO_TRENDING = {
    "timeframes": {"6h", "24h"},
    "limit": 15,
    "smart": True,
}
ELITE_TRENDING = {
    "timeframes": {"4h", "6h", "24h"},
    "limit": 50,
    "smart": True,
}

# Legacy support
FREE_TRENDING = BASIC_TRENDING
PREMIUM_TRENDING = ELITE_TRENDING

# Feature matrix: defines which tiers can access which features
FEATURE_MATRIX: Dict[Feature, Dict[str, Any]] = {
    # Legacy features (Pro and Elite)
    "smart_buy":       {"roles": {"Premium"}, "plans": {"pro", "premium", "elite"}},
    "trade_finder":    {"roles": {"Premium"}, "plans": {"pro", "premium", "elite"}},
    "deal_confidence": {"roles": {"Premium"}, "plans": {"pro", "premium", "elite"}},
    "backtest":        {"roles": {"Premium"}, "plans": {"pro", "premium", "elite"}},
    "smart_trending":  {"roles": {"Premium"}, "plans": {"pro", "premium", "elite"}},

    # New tier-specific features
    "portfolio_optimizer": {"roles": {"Premium"}, "plans": {"pro", "premium", "elite"}},  # Pro+
    "ai_copilot":          {"roles": set(), "plans": {"basic", "pro", "premium", "elite"}},  # All tiers
    "market_sentiment":    {"roles": set(), "plans": {"basic", "pro", "premium", "elite"}},  # All tiers
    "market_maker":        {"roles": {"Premium"}, "plans": {"elite"}},  # Elite only
    "advanced_analytics":  {"roles": {"Premium"}, "plans": {"pro", "premium", "elite"}},  # Pro+
    "leaderboard":         {"roles": set(), "plans": {"basic", "pro", "premium", "elite"}},  # All tiers
    "referrals":           {"roles": set(), "plans": {"basic", "pro", "premium", "elite"}},  # All tiers
    "bulk_trades":         {"roles": {"Premium"}, "plans": {"elite"}},  # Elite only
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
    if plan and plan.lower() in {"pro", "premium", "elite"}:
        return True
    if premium_until and premium_until > _now():
        return True
    if "Premium" in roles:
        return True
    return False

def _get_tier(plan: Optional[str], premium_until: Optional[datetime], roles: Set[str]) -> Tier:
    """Determine user's subscription tier"""
    if not plan:
        plan_lower = "basic"
    else:
        plan_lower = plan.lower()

    # Map plans to tiers
    if plan_lower in {"elite"}:
        return "elite"
    elif plan_lower in {"pro", "premium"}:
        return "pro"
    else:
        return "basic"


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

    # Treat all authenticated users as having full premium access
    premium = bool(user_id)
    tier: Tier = "elite" if user_id else "basic"

    # Set generous limits for authenticated users
    if premium:
        watchlist_max = ELITE_WATCHLIST_MAX
        trending = ELITE_TRENDING
    else:
        watchlist_max = BASIC_WATCHLIST_MAX
        trending = BASIC_TRENDING

    limits = {
        "watchlist_max": watchlist_max,
        "trending": trending,
        "bulk_trades_max": 100 if tier == "elite" else (25 if tier == "pro" else 10),
    }

    # Grant all features to authenticated users
    features: Set[Feature] = set(FEATURE_MATRIX.keys()) if premium else set()

    return {
        "user_id": user_id,
        "plan": plan,
        "tier": tier,
        "premium_until": premium_until,
        "roles": list(roles),
        "is_premium": premium,
        "features": list(features),
        "limits": limits,
    }


def require_feature(feature: Feature):
    async def _dep(req: Request):
        return True

    return _dep
