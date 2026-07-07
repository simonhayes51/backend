# app/routers/api_keys.py
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.auth.entitlements import compute_entitlements
from app.auth.api_keys import (
    TIER_LIMITS,
    generate_api_key,
    hash_api_key,
)
from app.db import get_db

router = APIRouter(prefix="/api/api-keys", tags=["api-keys"])

# Which key tiers a given subscription tier may self-serve. Higher API
# tiers (trader/dev) are their own paid products - assigned after purchase
# (Stripe payment link / manual for now), not self-served.
SELF_SERVE_KEY_TIER = "starter"


def _uid(request: Request) -> str:
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(401, "Not authenticated")
    return str(uid)


class ApiKeyCreate(BaseModel):
    name: Optional[str] = None


@router.get("")
async def list_api_keys(request: Request, conn = Depends(get_db)):
    uid = _uid(request)
    rows = await conn.fetch(
        """
        SELECT id, name, key_prefix, rate_limit_per_minute,
               COALESCE(tier, 'starter') AS tier, monthly_quota,
               created_at, last_used_at, revoked_at
        FROM api_keys
        WHERE user_id = $1
        ORDER BY created_at DESC
        """,
        uid,
    )
    items: List[Dict[str, Any]] = []
    month_start = date.today().replace(day=1)
    for r in rows:
        d = dict(r)
        try:
            used = await conn.fetchval(
                "SELECT COALESCE(SUM(requests),0) FROM api_key_usage WHERE api_key_id=$1 AND day >= $2",
                r["id"], month_start,
            )
            d["used_this_month"] = int(used or 0)
        except Exception:
            d["used_this_month"] = None
        items.append(d)
    return {"items": items, "tiers": {t: {"rpm": rpm, "monthly_quota": q} for t, (rpm, q) in TIER_LIMITS.items()}}


@router.post("")
async def create_api_key(payload: ApiKeyCreate, request: Request, conn = Depends(get_db)):
    """
    Issues a new API key (v1 + v2 public data API). The plaintext key is
    returned exactly once - only its hash is stored.

    Self-serve keys are 'starter' tier (free taster: 60 rpm, 5k req/month)
    and require a Pro+ account. Trader/Dev tiers are sold separately and
    upgraded via admin/billing flows.
    """
    uid = _uid(request)
    ent = await compute_entitlements(request)
    if not ent.get("is_premium"):
        raise HTTPException(
            402,
            detail={
                "error": "premium_required",
                "message": "API access needs a Pro plan.",
                "upgrade_url": "/billing",
            },
        )

    existing = await conn.fetchval(
        "SELECT COUNT(*) FROM api_keys WHERE user_id = $1 AND revoked_at IS NULL", uid
    )
    if int(existing or 0) >= 5:
        raise HTTPException(400, "Maximum of 5 active API keys per account - revoke one first.")

    tier = SELF_SERVE_KEY_TIER
    rpm, quota = TIER_LIMITS[tier]

    key = generate_api_key()
    row = await conn.fetchrow(
        """
        INSERT INTO api_keys (user_id, name, key_hash, key_prefix, rate_limit_per_minute, tier, monthly_quota)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING id, created_at
        """,
        uid,
        payload.name or "Untitled key",
        hash_api_key(key),
        key[:14],
        rpm,
        tier,
        quota,
    )
    return {
        "ok": True,
        "id": row["id"],
        "key": key,  # shown once
        "keyPrefix": key[:14],
        "tier": tier,
        "rpm": rpm,
        "monthlyQuota": quota,
        "createdAt": row["created_at"].isoformat(),
        "warning": "Save this key now - it will not be shown again.",
    }


@router.delete("/{key_id}")
async def revoke_api_key(key_id: int, request: Request, conn = Depends(get_db)):
    uid = _uid(request)
    res = await conn.execute(
        "UPDATE api_keys SET revoked_at = NOW() WHERE id = $1 AND user_id = $2 AND revoked_at IS NULL",
        key_id, uid,
    )
    if res.rsplit(" ", 1)[-1] == "0":
        raise HTTPException(404, "Key not found or already revoked")
    return {"ok": True}
