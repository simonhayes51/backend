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
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.auth.entitlements import compute_entitlements, invalidate_entitlements_cache
from app.db import get_db

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
