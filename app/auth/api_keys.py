# app/auth/api_keys.py
"""
Auth + metering for the public/paid historical-data API (v1 and v2).

Keys are high-entropy random tokens stored as SHA-256 hashes (standard
GitHub/Stripe-style token storage; no per-key salt needed).

v2 adds licensable tiers with hard monthly quotas + per-minute rate limits:

    starter  free taster     60 rpm      5,000 req/month
    trader   paid            120 rpm    100,000 req/month
    dev      paid, volume    600 rpm  2,000,000 req/month

Per-minute limiting uses Redis when REDIS_URL is set (correct across
multiple workers/instances) and falls back to an in-process window
otherwise (fine for a single-worker deployment; documented limitation).
Monthly quotas are enforced from api_key_usage in Postgres, so they are
always instance-safe.
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
import time
from collections import defaultdict
from datetime import date
from typing import Dict, Optional, Tuple

from fastapi import Depends, Header, HTTPException

from app.db import get_db

log = logging.getLogger("api_keys")

API_KEY_PREFIX = "futhub_"
DEFAULT_RATE_LIMIT_PER_MINUTE = 60

# tier -> (rate_limit_per_minute, monthly_quota)
TIER_LIMITS: Dict[str, Tuple[int, int]] = {
    "starter": (60, 5_000),
    "trader": (120, 100_000),
    "dev": (600, 2_000_000),
}


def generate_api_key() -> str:
    return f"{API_KEY_PREFIX}{secrets.token_urlsafe(32)}"


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


# ---------------------- per-minute rate limiting -----------------------------

_RATE_WINDOW: Dict[int, Tuple[float, int]] = defaultdict(lambda: (0.0, 0))

_redis = None
_redis_checked = False


async def _get_redis():
    """Lazy optional Redis client. None if REDIS_URL unset or unreachable."""
    global _redis, _redis_checked
    if _redis_checked:
        return _redis
    _redis_checked = True
    url = os.getenv("REDIS_URL")
    if not url:
        return None
    try:
        import redis.asyncio as aioredis  # type: ignore

        _redis = aioredis.from_url(url, decode_responses=True)
        await _redis.ping()
        log.info("API rate limiting backed by Redis")
    except Exception as e:
        log.warning("REDIS_URL set but unusable (%s) - falling back to in-process limiter", e)
        _redis = None
    return _redis


async def _check_rate_limit(key_id: int, limit_per_minute: int) -> None:
    r = await _get_redis()
    if r is not None:
        try:
            bucket = f"rl:apikey:{key_id}:{int(time.time() // 60)}"
            n = await r.incr(bucket)
            if n == 1:
                await r.expire(bucket, 90)
            if n > limit_per_minute:
                raise HTTPException(status_code=429, detail="Rate limit exceeded for this API key")
            return
        except HTTPException:
            raise
        except Exception:
            pass  # Redis hiccup -> fall through to in-process limiter

    now = time.time()
    window_start, count = _RATE_WINDOW[key_id]
    if now - window_start >= 60:
        _RATE_WINDOW[key_id] = (now, 1)
        return
    if count >= limit_per_minute:
        raise HTTPException(status_code=429, detail="Rate limit exceeded for this API key")
    _RATE_WINDOW[key_id] = (window_start, count + 1)


# ---------------------- monthly quota + metering -----------------------------


async def _check_and_meter_quota(conn, key_id: int, monthly_quota: int) -> Dict[str, int]:
    """Upsert today's usage row and enforce the calendar-month quota."""
    today = date.today()
    month_start = today.replace(day=1)
    try:
        used = await conn.fetchval(
            """
            SELECT COALESCE(SUM(requests), 0) FROM api_key_usage
            WHERE api_key_id = $1 AND day >= $2
            """,
            key_id, month_start,
        )
        if used is not None and used >= monthly_quota:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "monthly_quota_exceeded",
                    "quota": monthly_quota,
                    "used": int(used),
                    "resets": month_start.replace(
                        month=month_start.month % 12 + 1,
                        year=month_start.year + (1 if month_start.month == 12 else 0),
                    ).isoformat(),
                    "message": "Monthly request quota reached - upgrade the key's tier for more.",
                },
            )
        await conn.execute(
            """
            INSERT INTO api_key_usage (api_key_id, day, requests)
            VALUES ($1, $2, 1)
            ON CONFLICT (api_key_id, day) DO UPDATE SET requests = api_key_usage.requests + 1
            """,
            key_id, today,
        )
        return {"used": int(used or 0) + 1, "quota": monthly_quota}
    except HTTPException:
        raise
    except Exception as e:
        # Usage table missing (migration 010 not applied) - don't take the
        # whole API down over metering.
        log.warning("quota metering unavailable: %s", e)
        return {"used": 0, "quota": monthly_quota}


# ---------------------- the dependency ---------------------------------------


async def require_api_key(
    x_api_key: str = Header(..., alias="X-API-Key"),
    conn=Depends(get_db),
) -> Dict:
    try:
        row = await conn.fetchrow(
            """
            SELECT id, user_id, name, key_prefix, revoked_at, rate_limit_per_minute,
                   COALESCE(tier, 'starter') AS tier,
                   monthly_quota
            FROM api_keys
            WHERE key_hash = $1
            """,
            hash_api_key(x_api_key),
        )
    except Exception:
        # tier/monthly_quota columns absent (migration 010 not applied yet) -
        # fall back to the pre-tier shape so v1 keeps working mid-rollout.
        row = await conn.fetchrow(
            """
            SELECT id, user_id, name, key_prefix, revoked_at, rate_limit_per_minute,
                   'starter'::text AS tier, NULL::int AS monthly_quota
            FROM api_keys
            WHERE key_hash = $1
            """,
            hash_api_key(x_api_key),
        )
    if not row:
        raise HTTPException(status_code=401, detail="Invalid API key")
    if row["revoked_at"] is not None:
        raise HTTPException(status_code=401, detail="This API key has been revoked")

    tier = row["tier"] if row["tier"] in TIER_LIMITS else "starter"
    tier_rpm, tier_quota = TIER_LIMITS[tier]
    rpm = row["rate_limit_per_minute"] or tier_rpm
    quota = row["monthly_quota"] or tier_quota

    await _check_rate_limit(row["id"], rpm)
    usage = await _check_and_meter_quota(conn, row["id"], quota)

    # Best-effort last-used tracking.
    try:
        await conn.execute("UPDATE api_keys SET last_used_at = NOW() WHERE id = $1", row["id"])
    except Exception:
        pass

    out = dict(row)
    out["tier"] = tier
    out["usage"] = usage
    return out
