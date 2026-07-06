# app/auth/api_keys.py
"""
Auth for the public/paid historical-data API - deliberately independent of
app/auth/entitlements.py's require_feature(), which is currently a no-op
stub (always returns True regardless of the feature or the caller's
actual plan - see the comments in compute_entitlements(), which grants
every feature to any logged-in user). That's a real product decision to
revisit separately; this new external-facing surface shouldn't inherit
whatever that decision turns out to be, so it does its own key lookup,
revocation check, and rate limiting from scratch.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from collections import defaultdict
from typing import Dict, Tuple

from fastapi import Depends, Header, HTTPException

from app.db import get_db

API_KEY_PREFIX = "futhub_"
DEFAULT_RATE_LIMIT_PER_MINUTE = 60


def generate_api_key() -> str:
    return f"{API_KEY_PREFIX}{secrets.token_urlsafe(32)}"


def hash_api_key(key: str) -> str:
    # API keys are already high-entropy random tokens (not user-chosen
    # passwords), so a plain SHA-256 of the token is the standard approach
    # (same as GitHub/Stripe token storage) - no per-key salt needed.
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


# In-process sliding-window rate limiter keyed by api_key id. Resets on
# deploy and doesn't share state across multiple instances - fine for a
# single-worker deployment, but note this limitation before scaling out
# the API tier to multiple processes/machines.
_RATE_WINDOW: Dict[int, Tuple[float, int]] = defaultdict(lambda: (0.0, 0))


def _check_rate_limit(key_id: int, limit_per_minute: int) -> None:
    now = time.time()
    window_start, count = _RATE_WINDOW[key_id]
    if now - window_start >= 60:
        _RATE_WINDOW[key_id] = (now, 1)
        return
    if count >= limit_per_minute:
        raise HTTPException(status_code=429, detail="Rate limit exceeded for this API key")
    _RATE_WINDOW[key_id] = (window_start, count + 1)


async def require_api_key(
    x_api_key: str = Header(..., alias="X-API-Key"),
    conn = Depends(get_db),
) -> Dict:
    row = await conn.fetchrow(
        """
        SELECT id, user_id, name, revoked_at, rate_limit_per_minute
        FROM api_keys
        WHERE key_hash = $1
        """,
        hash_api_key(x_api_key),
    )
    if not row:
        raise HTTPException(status_code=401, detail="Invalid API key")
    if row["revoked_at"] is not None:
        raise HTTPException(status_code=401, detail="This API key has been revoked")

    _check_rate_limit(row["id"], row["rate_limit_per_minute"] or DEFAULT_RATE_LIMIT_PER_MINUTE)

    # Best-effort, non-blocking-ish usage tracking - don't fail the request
    # if this update has a hiccup.
    try:
        await conn.execute("UPDATE api_keys SET last_used_at = NOW() WHERE id = $1", row["id"])
    except Exception:
        pass

    return dict(row)
