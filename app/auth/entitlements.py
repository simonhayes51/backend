# app/auth/entitlements.py
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import Optional, Set, Literal, Dict, Any
import os
import asyncpg
import aiohttp
from fastapi import Request, HTTPException

Feature = Literal["smart_buy", "watchlist", "trade_finder", "deal_confidence", "backtest", "smart_trending"]

# ---- Env / config ----
FREE_WATCHLIST_MAX = int(os.getenv("WATCHLIST_FREE_MAX", "3"))
PREMIUM_WATCHLIST_MAX = int(os.getenv("WATCHLIST_PREMIUM_MAX", "500"))

FREE_TRENDING = {"timeframes": {"24h"}, "limit": 5, "smart": False}
PREMIUM_TRENDING = {"timeframes": {"4h", "6h", "24h"}, "limit": 20, "smart": True}

# Discord
DISCORD_SERVER_ID = os.getenv("DISCORD_SERVER_ID")          # guild id (required to read roles)
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")          # bot token
DISCORD_PREMIUM_ROLE_ID = os.getenv("DISCORD_PREMIUM_ROLE_ID")  # optional: prefer an ID match
PREMIUM_ROLE_NAME = os.getenv("DISCORD_PREMIUM_ROLE_NAME", "Premium")

FEATURE_MATRIX: Dict[Feature, Dict[str, Any]] = {
    "smart_buy":       {"roles": {"Premium"}, "plans": {"pro", "premium"}},
    "trade_finder":    {"roles": {"Premium"}, "plans": {"pro", "premium"}},
    "deal_confidence": {"roles": {"Premium"}, "plans": {"pro", "premium"}},
    "backtest":        {"roles": {"Premium"}, "plans": {"pro", "premium"}},
    "smart_trending":  {"roles": {"Premium"}, "plans": {"pro", "premium"}},
    # "watchlist" is handled via limits
}

def _now(): return datetime.now(timezone.utc)

def _is_premium(plan: Optional[str], premium_until: Optional[datetime], roles: Set[str]) -> bool:
    if plan and plan.lower() in {"pro", "premium"}:
        return True
    if premium_until and premium_until > _now():
        return True
    if "Premium" in roles:
        return True
    return False

async def _load_user_row(pool: asyncpg.Pool, user_id: str) -> Optional[asyncpg.Record]:
    # optional table with plan/premium_until/roles (JSON/text[])
    return await pool.fetchrow("SELECT plan, premium_until, roles FROM users WHERE id=$1", user_id)

# ---- light caches to avoid Discord rate limits ----
_ROLES_MAP_CACHE: Dict[str, Any] = {"at": 0.0, "ttl": 300.0, "data": {}}   # id -> name
_MEMBER_ROLES_CACHE: Dict[str, Any] = {"at": 0.0, "ttl": 60.0, "data": {}} # user_id -> set(role_ids)

async def _guild_roles_map() -> Dict[str, str]:
    if not (DISCORD_SERVER_ID and DISCORD_BOT_TOKEN):
        return {}
    import time
    now = time.time()
    if now - _ROLES_MAP_CACHE["at"] < _ROLES_MAP_CACHE["ttl"]:
        return _ROLES_MAP_CACHE["data"]
    url = f"https://discord.com/api/v10/guilds/{DISCORD_SERVER_ID}/roles"
    try:
        async with aiohttp.ClientSession(headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}) as sess:
            async with sess.get(url, timeout=10) as r:
                if r.status != 200:
                    return {}
                js = await r.json()
        mp = {str(role["id"]): role.get("name") for role in js if isinstance(role, dict)}
        _ROLES_MAP_CACHE.update({"at": now, "data": mp})
        return mp
    except Exception:
        return {}

async def _member_role_ids(user_id: str) -> Set[str]:
    if not (DISCORD_SERVER_ID and DISCORD_BOT_TOKEN):
        return set()
    import time
    now = time.time()
    cache = _MEMBER_ROLES_CACHE
    data = cache["data"].get(user_id)
    if data and (now - cache["at"] < cache["ttl"]):
        return data
    url = f"https://discord.com/api/v10/guilds/{DISCORD_SERVER_ID}/members/{user_id}"
    try:
        async with aiohttp.ClientSession(headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}) as sess:
            async with sess.get(url, timeout=10) as r:
                if r.status != 200:
                    return set()
                js = await r.json()
        role_ids = {str(rid) for rid in js.get("roles", [])}
        cache["at"] = now
        cache["data"][user_id] = role_ids
        return role_ids
    except Exception:
        return set()

async def _discord_has_premium(user_id: str) -> tuple[bool, Set[str]]:
    """
    Returns (has_premium_role, role_names_set)
    """
    ids = await _member_role_ids(user_id)
    names: Set[str] = set()
    if not ids:
        return (False, names)

    # Map ids -> names if we can
    mp = await _guild_roles_map()
    names = {mp.get(i, i) for i in ids}  # fall back to id strings if names not known

    # Prefer explicit ID match when provided
    if DISCORD_PREMIUM_ROLE_ID and DISCORD_PREMIUM_ROLE_ID in ids:
        names.add("Premium")
        return (True, names)

    # Otherwise accept by name
    has_by_name = PREMIUM_ROLE_NAME in names
    if has_by_name:
        names.add("Premium")
    return (has_by_name, names)

async def compute_entitlements(req: Request) -> Dict[str, Any]:
    # ---- get user id from session (your login stores "user_id") ----
    sess = getattr(req, "session", {}) or {}
    user_id = sess.get("user_id") or (sess.get("user") or {}).get("id")
    pool: asyncpg.Pool = req.app.state.pool

    plan = None
    premium_until = None
    roles_from_db: Set[str] = set()

    if user_id:
        row = await _load_user_row(pool, user_id)
        if row:
            plan = row["plan"]
            premium_until = row["premium_until"]
            roles_from_db = set(row["roles"] or [])

    # ---- pull Discord roles and see if Premium is present ----
    has_premium_discord = False
    discord_role_names: Set[str] = set()
    if user_id:
        has_premium_discord, discord_role_names = await _discord_has_premium(user_id)

    roles_union: Set[str] = roles_from_db | discord_role_names
    if has_premium_discord:
        roles_union.add("Premium")

    premium = _is_premium(plan, premium_until, roles_union)

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
        "roles": sorted(roles_union),
        "is_premium": premium,
        "features": sorted(features),
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
