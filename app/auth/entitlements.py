from __future__ import annotations

"""
Entitlements: the single place that decides what a user can do.

This module previously granted every feature to any logged-in user and
require_feature() was a no-op — the paywall did not exist (review issue C1).
It now computes a real tier from, in priority order:

  1. users.plan ('elite' | 'pro' | 'premium' | anything else -> free)
  2. users.premium_until / user_profiles.is_premium + premium_until
  3. An 'active' or 'trialing' row in subscriptions (Stripe-synced)
  4. The Discord "Premium" role (legacy grandfathering, cached 5 min)

and gates features through FEATURE_MATRIX. require_feature() raises
HTTP 402 (payment required) when the caller's tier doesn't include the
feature, which the frontend already understands (axios dispatches a
`premium:blocked` event on 402).
"""

import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set, Literal

import aiohttp
import asyncpg
from fastapi import HTTPException, Request

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
    "fair_value",
    "undervalued_board",
    "anomaly_alerts",
    "realtime_alerts",
]

Tier = Literal["free", "pro", "elite"]

# ---- Limits (overridable via env) -------------------------------------------
FREE_WATCHLIST_MAX = int(os.getenv("WATCHLIST_FREE_MAX", os.getenv("WATCHLIST_BASIC_MAX", "3")))
PRO_WATCHLIST_MAX = int(os.getenv("WATCHLIST_PRO_MAX", "25"))
ELITE_WATCHLIST_MAX = int(os.getenv("WATCHLIST_ELITE_MAX", "500"))

# Legacy aliases still imported elsewhere
BASIC_WATCHLIST_MAX = FREE_WATCHLIST_MAX
PREMIUM_WATCHLIST_MAX = ELITE_WATCHLIST_MAX

FREE_TRENDING = {"timeframes": {"24h"}, "limit": 5, "smart": False}
PRO_TRENDING = {"timeframes": {"6h", "24h"}, "limit": 15, "smart": True}
ELITE_TRENDING = {"timeframes": {"4h", "6h", "24h"}, "limit": 50, "smart": True}

# Legacy aliases
BASIC_TRENDING = FREE_TRENDING
PREMIUM_TRENDING = ELITE_TRENDING

# Feature -> minimum tier. Everything not listed here is free.
_TIER_ORDER: Dict[str, int] = {"free": 0, "pro": 1, "elite": 2}

FEATURE_MIN_TIER: Dict[Feature, Tier] = {
    # Free — the hook
    "ai_copilot": "free",
    "market_sentiment": "free",
    "leaderboard": "free",
    "referrals": "free",
    "watchlist": "free",          # size still limited by tier below
    # Pro — the daily-driver tools
    "smart_buy": "pro",
    "trade_finder": "pro",
    "deal_confidence": "pro",
    "backtest": "pro",
    "smart_trending": "pro",
    "portfolio_optimizer": "pro",
    "advanced_analytics": "pro",
    "fair_value": "pro",
    "undervalued_board": "pro",
    "realtime_alerts": "pro",
    # Elite — the edge
    "market_maker": "elite",
    "bulk_trades": "elite",
    "anomaly_alerts": "elite",
}

# Kept importable for older code paths that referenced the matrix by name.
FEATURE_MATRIX: Dict[Feature, Dict[str, Any]] = {
    f: {"min_tier": t} for f, t in FEATURE_MIN_TIER.items()
}

# ---- Discord legacy-role fallback (optional) --------------------------------
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_SERVER_ID = os.getenv("DISCORD_SERVER_ID")
DISCORD_PREMIUM_ROLE_ID = os.getenv("DISCORD_PREMIUM_ROLE_ID")

# ---- Admins ------------------------------------------------------------------
# Server-side admin was previously nonexistent - "admin" only lived in the
# frontend's VITE_ADMIN_IDS cosmetic check. Admins are now: any id listed in
# ADMIN_DISCORD_IDS (comma-separated discord ids / user ids), or a users row
# with account_type='admin'. Admins get elite entitlements unconditionally.
ADMIN_DISCORD_IDS = {
    s.strip() for s in os.getenv("ADMIN_DISCORD_IDS", "").split(",") if s.strip()
}

_ROLE_CACHE: dict[str, dict] = {}
ROLE_CACHE_TTL = 300  # seconds

# Short cache of computed entitlements so hot endpoints don't re-query the DB
# on every request. Keyed by user_id.
_ENT_CACHE: dict[str, dict] = {}
ENT_CACHE_TTL = int(os.getenv("ENTITLEMENTS_CACHE_TTL", "60"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_roles(raw: Any) -> Set[str]:
    """users.roles is JSONB, which asyncpg returns as a JSON *string* unless
    a codec is registered - so set('[\"Premium\"]') would iterate characters
    and 'Premium' in roles would never match (found live via
    /api/entitlements/debug showing roles: ['[', ']'])."""
    if not raw:
        return set()
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return set()
    if isinstance(raw, (list, tuple, set)):
        return {str(r) for r in raw}
    return set()


async def user_has_premium_role(discord_user_id: str) -> bool:
    """Legacy grandfathering: True if the member holds the Premium role."""
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


async def _load_billing_state(pool: asyncpg.Pool, user_id: str) -> Dict[str, Any]:
    """Read every stored signal that could make this user paid."""
    out: Dict[str, Any] = {
        "plan": None,
        "tier_column": None,
        "account_type": None,
        "premium_until": None,
        "roles": set(),
        "profile_premium": False,
        "profile_premium_until": None,
        "active_subscription": False,
    }
    try:
        row = await pool.fetchrow(
            "SELECT plan, tier, account_type, premium_until, roles FROM users WHERE id=$1",
            user_id,
        )
        if row:
            out["plan"] = row["plan"]
            out["tier_column"] = row["tier"]
            out["account_type"] = row["account_type"]
            out["premium_until"] = _as_aware(row["premium_until"])
            out["roles"] = _parse_roles(row["roles"])
    except Exception:
        # users table may predate tier/account_type columns
        try:
            row = await pool.fetchrow(
                "SELECT plan, premium_until, roles FROM users WHERE id=$1", user_id
            )
            if row:
                out["plan"] = row["plan"]
                out["premium_until"] = _as_aware(row["premium_until"])
                out["roles"] = _parse_roles(row["roles"])
        except Exception:
            pass

    try:
        prow = await pool.fetchrow(
            "SELECT is_premium, premium_until FROM user_profiles WHERE user_id=$1",
            user_id,
        )
        if prow:
            out["profile_premium"] = bool(prow["is_premium"])
            out["profile_premium_until"] = _as_aware(prow["premium_until"])
    except Exception:
        pass

    try:
        sub = await pool.fetchrow(
            """
            SELECT 1 FROM subscriptions
            WHERE user_id = $1
              AND status IN ('active', 'trialing')
              AND (current_period_end IS NULL OR current_period_end > NOW())
            LIMIT 1
            """,
            user_id,
        )
        out["active_subscription"] = sub is not None
    except Exception:
        pass

    return out


def _is_admin(state: Dict[str, Any], user_id: str, discord_id: Optional[str]) -> bool:
    if (state.get("account_type") or "").lower() == "admin":
        return True
    ids = {str(user_id)}
    if discord_id:
        ids.add(str(discord_id))
    return bool(ids & ADMIN_DISCORD_IDS)


def _resolve_tier(state: Dict[str, Any], has_discord_premium: bool) -> Tier:
    plan = (state.get("plan") or "").lower()
    # users.tier is honored as a manual-grant channel: an admin running
    # UPDATE users SET tier='elite' WHERE ... should just work, without
    # having to fake a Stripe subscription. ('basic' means nothing here -
    # it's the column's legacy default, not a grant.)
    tier_col = (state.get("tier_column") or "").lower()
    now = _now()

    if plan == "elite" or tier_col == "elite":
        return "elite"

    paid = (
        plan in {"pro", "premium"}
        or tier_col in {"pro", "premium"}
        or state.get("active_subscription")
        or (state.get("premium_until") and state["premium_until"] > now)
        or (
            state.get("profile_premium")
            and (
                state.get("profile_premium_until") is None
                or state["profile_premium_until"] > now
            )
        )
        or "Premium" in (state.get("roles") or set())
        or has_discord_premium
    )
    return "pro" if paid else "free"


def _limits_for(tier: Tier) -> Dict[str, Any]:
    if tier == "elite":
        return {
            "watchlist_max": ELITE_WATCHLIST_MAX,
            "trending": ELITE_TRENDING,
            "bulk_trades_max": 100,
        }
    if tier == "pro":
        return {
            "watchlist_max": PRO_WATCHLIST_MAX,
            "trending": PRO_TRENDING,
            "bulk_trades_max": 25,
        }
    return {
        "watchlist_max": FREE_WATCHLIST_MAX,
        "trending": FREE_TRENDING,
        "bulk_trades_max": 10,
    }


def features_for_tier(tier: Tier) -> Set[Feature]:
    rank = _TIER_ORDER[tier]
    return {f for f, t in FEATURE_MIN_TIER.items() if _TIER_ORDER[t] <= rank}


async def compute_entitlements(req: Request) -> Dict[str, Any]:
    sess = req.session or {}
    user_id = sess.get("user_id") or (sess.get("user") or {}).get("id")

    if not user_id:
        tier: Tier = "free"
        return {
            "user_id": None,
            "plan": None,
            "tier": tier,
            "premium_until": None,
            "roles": [],
            "is_premium": False,
            "is_admin": False,
            "features": sorted(features_for_tier(tier)),
            "limits": _limits_for(tier),
        }

    cached = _ENT_CACHE.get(str(user_id))
    if cached and (time.time() - cached["at"] < ENT_CACHE_TTL):
        return dict(cached["ent"])

    pool: asyncpg.Pool = req.app.state.pool
    state = await _load_billing_state(pool, str(user_id))
    roles: Set[str] = set(sess.get("roles") or []) | set(state.get("roles") or set())
    discord_id = sess.get("discord_id") or user_id

    has_discord_premium = "Premium" in roles
    if not has_discord_premium:
        try:
            has_discord_premium = await user_has_premium_role(str(discord_id))
            if has_discord_premium:
                roles.add("Premium")
        except Exception:
            has_discord_premium = False

    is_admin = _is_admin(state, str(user_id), str(discord_id) if discord_id else None)
    tier = "elite" if is_admin else _resolve_tier(state, has_discord_premium)
    is_premium = tier in ("pro", "elite")

    premium_until = state.get("premium_until") or state.get("profile_premium_until")

    ent = {
        "user_id": str(user_id),
        "plan": state.get("plan"),
        "tier": tier,
        "premium_until": premium_until.isoformat() if premium_until else None,
        "roles": sorted(roles),
        "is_premium": is_premium,
        "is_admin": is_admin,
        "features": sorted(features_for_tier(tier)),
        "limits": _limits_for(tier),
    }
    _ENT_CACHE[str(user_id)] = {"at": time.time(), "ent": dict(ent)}
    return ent


def invalidate_entitlements_cache(user_id: str) -> None:
    """Call after any billing change (Stripe webhook, admin action)."""
    _ENT_CACHE.pop(str(user_id), None)


async def explain_entitlements(req: Request) -> Dict[str, Any]:
    """Self-serve diagnosis: every raw signal the tier decision reads, plus
    whether the Discord env is even configured. Answers 'why am I not
    premium?' without anyone having to read server logs. Exposed (for the
    logged-in user only) at /api/entitlements/debug."""
    sess = req.session or {}
    user_id = sess.get("user_id") or (sess.get("user") or {}).get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    pool: asyncpg.Pool = req.app.state.pool
    state = await _load_billing_state(pool, str(user_id))
    discord_id = sess.get("discord_id") or user_id

    discord_env_ready = bool(DISCORD_BOT_TOKEN and DISCORD_SERVER_ID and DISCORD_PREMIUM_ROLE_ID)
    has_discord_premium = False
    if discord_env_ready:
        try:
            has_discord_premium = await user_has_premium_role(str(discord_id))
        except Exception:
            has_discord_premium = False

    # Bypass the cache so this always reflects the live DB.
    invalidate_entitlements_cache(str(user_id))
    ent = await compute_entitlements(req)

    return {
        "resolved": {"tier": ent["tier"], "is_premium": ent["is_premium"], "is_admin": ent["is_admin"]},
        "signals": {
            "users.plan": state.get("plan"),
            "users.tier": state.get("tier_column"),
            "users.account_type": state.get("account_type"),
            "users.premium_until": state["premium_until"].isoformat() if state.get("premium_until") else None,
            "users.roles": sorted(state.get("roles") or []),
            "user_profiles.is_premium": state.get("profile_premium"),
            "user_profiles.premium_until": state["profile_premium_until"].isoformat() if state.get("profile_premium_until") else None,
            "subscriptions.active_or_trialing": state.get("active_subscription"),
            "discord.premium_role": has_discord_premium,
            "admin.matched": ent["is_admin"],
        },
        "config": {
            "discord_role_check_configured": discord_env_ready,
            "admin_ids_configured": bool(ADMIN_DISCORD_IDS),
        },
        "hint": (
            "Premium comes from ANY of: users.plan in (pro/premium/elite), users.tier in "
            "(pro/elite), an active/trialing subscriptions row, unexpired premium_until on "
            "users or user_profiles, the Discord Premium role (needs "
            "DISCORD_BOT_TOKEN/DISCORD_SERVER_ID/DISCORD_PREMIUM_ROLE_ID set), or admin "
            "(ADMIN_DISCORD_IDS / account_type='admin')."
        ),
    }


def require_feature(feature: Feature):
    """FastAPI dependency: 401 if anonymous, 402 if tier doesn't cover it."""

    async def _dep(req: Request) -> Dict[str, Any]:
        ent = await compute_entitlements(req)
        if not ent["user_id"]:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if feature not in ent["features"]:
            min_tier = FEATURE_MIN_TIER.get(feature, "pro")
            raise HTTPException(
                status_code=402,
                detail={
                    "error": "upgrade_required",
                    "feature": feature,
                    "required_tier": min_tier,
                    "current_tier": ent["tier"],
                    "message": f"'{feature}' needs the {min_tier.title()} plan.",
                },
            )
        return ent

    return _dep


def require_tier(min_tier: Tier):
    """FastAPI dependency for tier-gated (rather than feature-gated) routes."""

    async def _dep(req: Request) -> Dict[str, Any]:
        ent = await compute_entitlements(req)
        if not ent["user_id"]:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if _TIER_ORDER[ent["tier"]] < _TIER_ORDER[min_tier]:
            raise HTTPException(
                status_code=402,
                detail={
                    "error": "upgrade_required",
                    "required_tier": min_tier,
                    "current_tier": ent["tier"],
                    "message": f"This needs the {min_tier.title()} plan.",
                },
            )
        return ent

    return _dep
