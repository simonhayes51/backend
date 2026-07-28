# app/routers/admin.py
"""
Admin-only user management — grant/revoke premium from the browser, no
terminal or pgAdmin needed (the deploy environment has no shell access).

Gated by require_admin: ADMIN_DISCORD_IDS env or users.account_type='admin'
(see app/auth/entitlements.py). Every grant/revoke is written to
admin_audit_log so there's a record of who gave what to whom.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.auth.entitlements import compute_entitlements, invalidate_entitlements_cache
from app.auth.api_keys import TIER_LIMITS
from app.db import get_db, get_core_pool, get_player_pool
from app.routers.dashboard import _heartbeats_by_worker, _parse_detail_counts, _status_card, _iso
from app.services import market_events as me

router = APIRouter(prefix="/api/admin", tags=["admin"])

VALID_TIERS = {"free", "pro", "elite"}


async def require_admin(req: Request) -> Dict[str, Any]:
    ent = await compute_entitlements(req)
    if not ent["user_id"]:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not ent.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin only")
    return ent


class TierUpdate(BaseModel):
    tier: str = Field(pattern="^(free|pro|elite)$")
    days: Optional[int] = Field(None, ge=1, le=3650, description="optional expiry in days")


@router.get("/users")
async def search_users(
    q: str = Query("", description="username / discord id / user id substring"),
    limit: int = Query(20, ge=1, le=100),
    admin=Depends(require_admin),
    conn=Depends(get_db),
):
    q = (q or "").strip()
    where = "TRUE"
    params: list[Any] = []
    if q:
        where = "(LOWER(username) LIKE LOWER($1) OR id LIKE $1 OR discord_id::text LIKE $1)"
        params.append(f"%{q}%")
    params.append(limit)
    rows = await conn.fetch(
        f"""
        SELECT id, discord_id, username, tier, plan, premium_until, account_type, created_at
        FROM users
        WHERE {where}
        ORDER BY created_at DESC NULLS LAST
        LIMIT ${len(params)}
        """,
        *params,
    )
    return {
        "users": [
            {
                "id": r["id"],
                "discord_id": str(r["discord_id"]) if r["discord_id"] else None,
                "username": r["username"],
                "tier": r["tier"],
                "plan": r["plan"],
                "premium_until": r["premium_until"].isoformat() if r["premium_until"] else None,
                "account_type": r["account_type"],
            }
            for r in rows
        ]
    }


@router.post("/users/{user_id}/tier")
async def set_user_tier(
    user_id: str,
    payload: TierUpdate,
    admin=Depends(require_admin),
    conn=Depends(get_db),
):
    """Grant or revoke premium. tier='free' revokes; 'pro'/'elite' grant.
    Optional days sets premium_until as a time-limited grant. Takes effect
    within the entitlements cache TTL (~60s) - immediately for the target
    once their cache entry is invalidated below."""
    row = await conn.fetchrow(
        "SELECT id, username, tier FROM users WHERE id = $1 OR discord_id::text = $1",
        user_id,
    )
    if not row:
        raise HTTPException(404, "No user matching that id / discord id")

    tier = payload.tier
    until = (
        datetime.now(timezone.utc) + timedelta(days=payload.days)
        if payload.days and tier != "free"
        else None
    )
    # 'basic' is the users.tier column's legacy no-grant default; 'free' is
    # only an API-level alias for it.
    stored_tier = "basic" if tier == "free" else tier

    await conn.execute(
        "UPDATE users SET tier = $2, premium_until = $3 WHERE id = $1",
        row["id"], stored_tier, until,
    )

    # Audit trail (best-effort - never block the grant on it)
    try:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_audit_log (
                id BIGSERIAL PRIMARY KEY,
                admin_user_id TEXT NOT NULL,
                action TEXT NOT NULL,
                target_user_id TEXT NOT NULL,
                detail TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        await conn.execute(
            "INSERT INTO admin_audit_log (admin_user_id, action, target_user_id, detail) VALUES ($1,$2,$3,$4)",
            str(admin["user_id"]),
            "set_tier",
            str(row["id"]),
            f"tier={stored_tier}" + (f" until={until.isoformat()}" if until else ""),
        )
    except Exception:
        pass

    invalidate_entitlements_cache(str(row["id"]))
    return {
        "ok": True,
        "user_id": row["id"],
        "username": row["username"],
        "previous_tier": row["tier"],
        "tier": stored_tier,
        "premium_until": until.isoformat() if until else None,
    }


# --------------------------- API key sales ops --------------------------------
# Fulfillment for the paid Data API tiers (trader/dev). The sales flow is:
# buyer pays a Stripe Payment Link -> admin upgrades their key here. The
# key inherits its tier's rate limit + monthly quota from TIER_LIMITS.


class KeyTierUpdate(BaseModel):
    tier: str = Field(pattern="^(starter|trader|dev)$")


@router.get("/api-keys")
async def search_api_keys(
    q: str = Query("", description="username / discord id / key prefix substring"),
    limit: int = Query(30, ge=1, le=100),
    admin=Depends(require_admin),
    conn=Depends(get_db),
):
    q = (q or "").strip()
    where = "k.revoked_at IS NULL"
    params: list[Any] = []
    if q:
        params.append(f"%{q}%")
        where += (
            " AND (LOWER(u.username) LIKE LOWER($1) OR k.user_id LIKE $1"
            " OR u.discord_id::text LIKE $1 OR k.key_prefix LIKE $1)"
        )
    params.append(limit)
    rows = await conn.fetch(
        f"""
        SELECT k.id, k.user_id, k.name, k.key_prefix,
               COALESCE(k.tier, 'starter') AS tier,
               k.monthly_quota, k.rate_limit_per_minute,
               k.created_at, k.last_used_at,
               u.username, u.discord_id,
               COALESCE((
                   SELECT SUM(requests) FROM api_key_usage
                   WHERE api_key_id = k.id AND day >= date_trunc('month', now())::date
               ), 0) AS used_this_month
        FROM api_keys k
        LEFT JOIN users u ON u.id = k.user_id
        WHERE {where}
        ORDER BY k.created_at DESC
        LIMIT ${len(params)}
        """,
        *params,
    )
    return {
        "keys": [
            {
                "id": r["id"],
                "user_id": r["user_id"],
                "username": r["username"],
                "discord_id": str(r["discord_id"]) if r["discord_id"] else None,
                "name": r["name"],
                "key_prefix": r["key_prefix"],
                "tier": r["tier"],
                "monthly_quota": r["monthly_quota"],
                "rpm": r["rate_limit_per_minute"],
                "used_this_month": int(r["used_this_month"] or 0),
                "last_used_at": r["last_used_at"].isoformat() if r["last_used_at"] else None,
            }
            for r in rows
        ],
        "tiers": {t: {"rpm": rpm, "monthly_quota": quota} for t, (rpm, quota) in TIER_LIMITS.items()},
    }


@router.post("/api-keys/{key_id}/tier")
async def set_api_key_tier(
    key_id: int,
    payload: KeyTierUpdate,
    admin=Depends(require_admin),
    conn=Depends(get_db),
):
    """Upgrade/downgrade a key after a Data API sale. Applies the tier's
    canonical rate limit and monthly quota; takes effect on the key's very
    next request (require_api_key reads the row live)."""
    rpm, quota = TIER_LIMITS[payload.tier]
    row = await conn.fetchrow(
        """
        UPDATE api_keys
           SET tier = $2, rate_limit_per_minute = $3, monthly_quota = $4
         WHERE id = $1 AND revoked_at IS NULL
        RETURNING id, user_id, key_prefix, tier
        """,
        key_id, payload.tier, rpm, quota,
    )
    if not row:
        raise HTTPException(404, "Key not found or revoked")

    try:
        await conn.execute(
            "INSERT INTO admin_audit_log (admin_user_id, action, target_user_id, detail) VALUES ($1,$2,$3,$4)",
            str(admin["user_id"]),
            "set_api_key_tier",
            str(row["user_id"]),
            f"key_id={key_id} prefix={row['key_prefix']} tier={payload.tier} rpm={rpm} quota={quota}",
        )
    except Exception:
        pass

    return {"ok": True, "key_id": row["id"], "key_prefix": row["key_prefix"], "tier": row["tier"], "rpm": rpm, "monthly_quota": quota}


@router.get("/sbc/imports")
async def sbc_imports(admin=Depends(require_admin)) -> Dict[str, Any]:
    """SBC collector status + a daily import count - reuses dashboard.py's
    existing cross-pool heartbeat merge and detail-count parser rather
    than reimplementing them for this one worker."""
    core_pool = await get_core_pool()
    player_pool = await get_player_pool()

    heartbeats = await _heartbeats_by_worker(core_pool, player_pool)
    hb = heartbeats.get("futbin_sbc_sync")
    counts = _parse_detail_counts(hb["detail"] if hb else None)
    status = _status_card("SBC Collector", hb, counts.get("sets_written"))

    async with player_pool.acquire() as conn:
        daily = await conn.fetch(
            """
            SELECT date_trunc('day', first_seen_at) AS day, count(*) AS new_sets
            FROM market_events
            WHERE kind = 'sbc' AND first_seen_at >= now() - interval '7 days'
            GROUP BY 1 ORDER BY 1 DESC
            """
        )
        total = await conn.fetchval("SELECT count(*) FROM market_events WHERE kind = 'sbc'")

    return {
        "status": status,
        "total_sbc_events": int(total or 0),
        "daily_imports": [
            {"day": _iso(r["day"]), "new_sets": r["new_sets"]} for r in daily
        ],
    }


# --------------------------- v2 admin area -------------------------------
# Everything below is Phase 4's v2 Admin area - all reused query helpers
# and existing tables (nothing new is synthesized), all gated by the same
# require_admin already used above.

# Generous on purpose: this app's real refresh cadences range 60-300s
# (fair_value.py/analytics_engine.py/recommendation_engine.py poll
# intervals), so 15 minutes comfortably covers one full missed cycle
# before flagging "stale" rather than false-alarming on normal jitter.
_STALE_AFTER_SECONDS = 15 * 60


def _freshness(computed_at: Optional[datetime]) -> Dict[str, Any]:
    if not computed_at:
        return {"status": "unknown", "computed_at": None, "age_seconds": None}
    age = (datetime.now(timezone.utc) - computed_at).total_seconds()
    return {
        "status": "ok" if age < _STALE_AFTER_SECONDS else "stale",
        "computed_at": computed_at.isoformat(),
        "age_seconds": round(age),
    }


@router.get("/pipeline/health")
async def pipeline_health(admin=Depends(require_admin)) -> Dict[str, Any]:
    """Freshness of the backend's own internal refresh loops (fair value,
    analytics, recommendations, event impact, market regime) - distinct
    from /collectors/status below, which covers the external auto_sync
    scrapers. None of these loops write pipeline_heartbeats themselves
    (only auto_sync's Cron workers do), so the only honest signal here is
    each output's own computed_at/detected_at watermark."""
    player_pool = await get_player_pool()
    core_pool = await get_core_pool()

    async with player_pool.acquire() as conn:
        fair_value_at = await conn.fetchval("SELECT max(computed_at) FROM fair_value_mv")
        scores_at = await conn.fetchval("SELECT max(computed_at) FROM card_scores_latest")
        recs_at = await conn.fetchval("SELECT max(computed_at) FROM recommendations_latest")
        impact_at = await conn.fetchval("SELECT max(computed_at) FROM event_market_impact")

    async with core_pool.acquire() as conn:
        regime_at = await conn.fetchval("SELECT max(detected_at) FROM market_states")

    return {
        "engines": [
            {"name": "Fair Value", **_freshness(fair_value_at)},
            {"name": "Analytics Engine (card scores)", **_freshness(scores_at)},
            {"name": "Recommendation Engine", **_freshness(recs_at)},
            {"name": "Event Impact", **_freshness(impact_at)},
            {"name": "Market Regime", **_freshness(regime_at)},
        ]
    }


@router.get("/collectors/status")
async def collectors_status(admin=Depends(require_admin)) -> Dict[str, Any]:
    """Every auto_sync worker that has ever written a heartbeat, not just
    the fixed subset dashboard.py's public /stats hardcodes for the demo
    page - this is the admin-only, complete view."""
    player_pool = await get_player_pool()
    core_pool = await get_core_pool()
    heartbeats = await _heartbeats_by_worker(core_pool, player_pool)

    collectors = []
    for worker, hb in sorted(heartbeats.items()):
        counts = _parse_detail_counts(hb["detail"])
        collectors.append({
            "worker": worker,
            "status": "ok" if hb["ok"] else "failing",
            "last_run_at": _iso(hb["last_run_at"]),
            "detail": hb["detail"],
            "counts": counts,
        })
    return {"collectors": collectors}


@router.get("/market-events")
async def admin_market_events(
    kind: Optional[str] = Query(None),
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    admin=Depends(require_admin),
) -> Dict[str, Any]:
    """Same list query the public /api/v2/sbc/events route uses
    (app/services/market_events.get_events), just without the kind='sbc'
    default - lets admins see every event kind that's ever landed, not
    only SBCs."""
    player_pool = await get_player_pool()
    events = await me.get_events(player_pool, kind=kind, limit=limit, offset=offset)
    return {"items": events, "count": len(events)}


@router.get("/subscriptions")
async def admin_subscriptions(
    status: Optional[str] = Query(None, description="filter by Stripe subscription status"),
    limit: int = Query(30, ge=1, le=100),
    admin=Depends(require_admin),
    conn=Depends(get_db),
) -> Dict[str, Any]:
    """Real Stripe subscription rows (main.py's webhook handler writes
    these on checkout.session.completed / customer.subscription.*) joined
    to users for a readable name - no separate billing dashboard exists
    today."""
    where = "WHERE s.status = $1" if status else "WHERE TRUE"
    params: List[Any] = [status] if status else []
    params.append(limit)
    rows = await conn.fetch(
        f"""
        SELECT s.id, s.user_id, u.username, s.stripe_subscription_id,
               s.status, s.plan_id, s.current_period_start,
               s.current_period_end, s.cancel_at_period_end, s.created_at
        FROM subscriptions s
        LEFT JOIN users u ON u.id = s.user_id
        {where}
        ORDER BY s.created_at DESC
        LIMIT ${len(params)}
        """,
        *params,
    )
    return {
        "items": [
            {
                "id": r["id"],
                "user_id": r["user_id"],
                "username": r["username"],
                "stripe_subscription_id": r["stripe_subscription_id"],
                "status": r["status"],
                "plan_id": r["plan_id"],
                "current_period_start": _iso(r["current_period_start"]),
                "current_period_end": _iso(r["current_period_end"]),
                "cancel_at_period_end": r["cancel_at_period_end"],
                "created_at": _iso(r["created_at"]),
            }
            for r in rows
        ]
    }


@router.get("/api-usage")
async def admin_api_usage(
    days: int = Query(14, ge=1, le=90),
    admin=Depends(require_admin),
    conn=Depends(get_db),
) -> Dict[str, Any]:
    """Daily request-volume trend across every API key, complementing the
    existing /api-keys endpoint's per-key monthly total - reuses the same
    api_key_usage table the request-counting middleware already writes
    to, no new tracking added."""
    daily = await conn.fetch(
        """
        SELECT day, SUM(requests) AS requests, COUNT(DISTINCT api_key_id) AS active_keys
        FROM api_key_usage
        WHERE day >= (CURRENT_DATE - ($1 || ' days')::interval)
        GROUP BY day
        ORDER BY day DESC
        """,
        str(days),
    )
    top_keys = await conn.fetch(
        """
        SELECT k.id, k.key_prefix, k.name, u.username, COALESCE(k.tier, 'starter') AS tier,
               SUM(uu.requests) AS requests
        FROM api_key_usage uu
        JOIN api_keys k ON k.id = uu.api_key_id
        LEFT JOIN users u ON u.id = k.user_id
        WHERE uu.day >= (CURRENT_DATE - ($1 || ' days')::interval)
        GROUP BY k.id, k.key_prefix, k.name, u.username, k.tier
        ORDER BY requests DESC
        LIMIT 10
        """,
        str(days),
    )
    return {
        "daily": [
            {"day": _iso(r["day"]) or str(r["day"]), "requests": int(r["requests"] or 0), "active_keys": r["active_keys"]}
            for r in daily
        ],
        "top_keys": [
            {
                "id": r["id"], "key_prefix": r["key_prefix"], "name": r["name"],
                "username": r["username"], "tier": r["tier"], "requests": int(r["requests"] or 0),
            }
            for r in top_keys
        ],
    }
