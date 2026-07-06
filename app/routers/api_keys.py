# app/routers/api_keys.py
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.auth.entitlements import compute_entitlements
from app.auth.api_keys import generate_api_key, hash_api_key, DEFAULT_RATE_LIMIT_PER_MINUTE
from app.db import get_db

router = APIRouter(prefix="/api/api-keys", tags=["api-keys"])


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
        SELECT id, name, key_prefix, rate_limit_per_minute, created_at, last_used_at, revoked_at
        FROM api_keys
        WHERE user_id = $1
        ORDER BY created_at DESC
        """,
        uid,
    )
    return {"items": [dict(r) for r in rows]}


@router.post("")
async def create_api_key(payload: ApiKeyCreate, request: Request, conn = Depends(get_db)):
    """
    Issues a new API key for the paid historical-data tier
    (/api/public/v1/*). The plaintext key is returned exactly once here -
    only its hash is stored, so it can never be shown again after this
    response.

    NOTE: gated on is_premium, which - per a real gap found while building
    this - currently means "any logged-in user" (compute_entitlements()
    grants premium to every authenticated session, not just paying ones).
    Selling API access as its own product on top of that needs a real
    decision about pricing/tier before this is a genuine paid tier rather
    than a free perk of having an account.
    """
    uid = _uid(request)
    ent = await compute_entitlements(request)
    if not ent.get("is_premium"):
        raise HTTPException(
            402,
            detail={
                "error": "premium_required",
                "message": "API access requires a Premium account.",
                "upgrade_url": "/billing",
            },
        )

    existing = await conn.fetchval(
        "SELECT COUNT(*) FROM api_keys WHERE user_id = $1 AND revoked_at IS NULL", uid
    )
    if int(existing or 0) >= 5:
        raise HTTPException(400, "Maximum of 5 active API keys per account - revoke one first.")

    key = generate_api_key()
    row = await conn.fetchrow(
        """
        INSERT INTO api_keys (user_id, name, key_hash, key_prefix, rate_limit_per_minute)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING id, created_at
        """,
        uid,
        payload.name or "Untitled key",
        hash_api_key(key),
        key[:14],
        DEFAULT_RATE_LIMIT_PER_MINUTE,
    )
    return {
        "ok": True,
        "id": row["id"],
        "key": key,  # shown once
        "keyPrefix": key[:14],
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
