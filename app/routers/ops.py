# app/routers/ops.py
"""
Operational visibility — replaces the removed debug endpoints with a safe,
read-only freshness surface. The product's value IS fresh data, so staleness
must be observable (review issue C7/C8).

/api/ops/freshness reports the age of every pipeline output plus worker
heartbeats, and an overall 'ok' flag suitable for uptime monitors
(UptimeRobot/BetterStack hitting this URL and alerting on "ok": false is a
zero-infra way to get paged when a scraper dies).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request

from app.services.fair_value import get_freshness

router = APIRouter(prefix="/api/ops", tags=["ops"])

# Max acceptable staleness per signal, in minutes.
THRESHOLDS_MIN = {
    "last_sale_at": 60,            # sales sync runs every ~10 min
    "last_bin_at": 60,
    "last_catalog_price_at": 60 * 30,  # daily catalog crawl
    "fair_value_computed_at": 30,
}


def _age_minutes(ts: Optional[datetime]) -> Optional[float]:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return round((datetime.now(timezone.utc) - ts).total_seconds() / 60.0, 1)


@router.get("/freshness")
async def freshness(request: Request) -> Dict[str, Any]:
    player_pool = getattr(request.app.state, "player_pool", None)
    core_pool = getattr(request.app.state, "pool", None)
    if player_pool is None or core_pool is None:
        raise HTTPException(503, "pools not ready")

    raw = await get_freshness(player_pool)

    signals: Dict[str, Any] = {}
    ok = True
    for key, ts in raw.items():
        age = _age_minutes(ts)
        threshold = THRESHOLDS_MIN.get(key)
        stale = age is None or (threshold is not None and age > threshold)
        if key in ("last_sale_at", "last_bin_at") and stale:
            ok = False
        signals[key] = {
            "at": ts.isoformat() if ts else None,
            "age_minutes": age,
            "threshold_minutes": threshold,
            "stale": stale,
        }

    heartbeats = []
    try:
        async with core_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT worker, last_run_at, ok, detail FROM pipeline_heartbeats ORDER BY worker"
            )
        for r in rows:
            hb_age = _age_minutes(r["last_run_at"])
            heartbeats.append(
                {
                    "worker": r["worker"],
                    "last_run_at": r["last_run_at"].isoformat() if r["last_run_at"] else None,
                    "age_minutes": hb_age,
                    "ok": r["ok"],
                    "detail": r["detail"],
                }
            )
            if not r["ok"]:
                ok = False
    except Exception:
        heartbeats = []  # table may not exist until migration 010 runs

    return {"ok": ok, "signals": signals, "heartbeats": heartbeats}
