# app/routers/watchlist.py
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional, AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.auth.entitlements import compute_entitlements
from app.db import get_watchlist_db
from app.ea_client import ea_lowest_bin_price, get_configured_sid

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])

# ------------ DB deps (WATCHLIST database only) ------------------------------
async def get_watch_db() -> AsyncGenerator:
    async for conn in get_watchlist_db():
        yield conn


def _uid_param(request: Request) -> str:
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(401, "Not authenticated")
    return str(uid)

# ------------ EA official price fetch (self-contained; no import from main) ---
# PS and Xbox share one FUT market, so a single console EA session covers both;
# PC prices differ and aren't available from a console account.
PRICE_CACHE_TTL = 5
_price_cache: Dict[str, Dict[str, Any]] = {}

def _plat(p: str) -> str:
    p = (p or "").lower()
    if p in ("ps", "playstation", "console"): return "ps"
    if p in ("xbox", "xb"): return "xbox"
    if p in ("pc", "origin"): return "pc"
    return "ps"

async def _fetch_price(card_id: int, platform: str) -> Dict[str, Any]:
    key = f"{card_id}|{platform}"
    now = time.time()
    if key in _price_cache and now - _price_cache[key]["at"] < PRICE_CACHE_TTL:
        c = _price_cache[key]
        return {"price": c["price"], "isExtinct": c["isExtinct"], "updatedAt": c["updatedAt"]}

    sid = get_configured_sid()
    if not sid or platform == "pc":
        cached = _price_cache.get(key)
        if cached:
            return {"price": cached["price"], "isExtinct": cached["isExtinct"], "updatedAt": cached["updatedAt"]}
        raise HTTPException(502, "Failed to fetch price: no EA session configured")

    last_err = None
    for attempt in (0, 1, 2):
        try:
            price = await ea_lowest_bin_price(card_id, sid)
            updated = time.time()
            _price_cache[key] = {"at": now, "price": price, "isExtinct": price is None, "updatedAt": updated}
            return {"price": price, "isExtinct": price is None, "updatedAt": updated}
        except Exception as e:
            last_err = str(e)
        await asyncio.sleep(0.2 * (3 ** attempt))

    cached = _price_cache.get(key)
    if cached:
        return {"price": cached["price"], "isExtinct": cached["isExtinct"], "updatedAt": cached["updatedAt"]}
    raise HTTPException(502, f"Failed to fetch price: {last_err}")

# ------------ Models -----------------------------------------------------------
class WatchlistCreate(BaseModel):
    card_id: int
    player_name: str
    version: Optional[str] = None
    platform: str  # ps|xbox|pc
    notes: Optional[str] = None

# ------------ Endpoints --------------------------------------------------------
@router.get("")
async def list_watch_items(
    request: Request,
    wdb = Depends(get_watch_db),
):
    uid = _uid_param(request)
    rows = await wdb.fetch(
        "SELECT * FROM watchlist WHERE user_id=$1 ORDER BY started_at DESC NULLS LAST",
        uid,
    )
    items = [dict(r) for r in rows]
    if not items:
        return {"ok": True, "items": []}

    # live prices (console by row platform)
    tasks = [_fetch_price(int(it["card_id"]), _plat(it["platform"])) for it in items]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    enriched = []
    for it, live in zip(items, results):
        live = live if isinstance(live, dict) else {}
        live_price = live.get("price") if isinstance(live, dict) else None

        change = change_pct = None
        if isinstance(live_price, (int, float)) and it.get("started_price"):
            try:
                base = int(it["started_price"])
                change = int(live_price) - base
                change_pct = round((change / base) * 100, 2) if base else None
            except Exception:
                pass

        enriched.append({
            "id": it["id"],
            "card_id": it["card_id"],
            "player_name": it["player_name"],
            "version": it["version"],
            "platform": it["platform"],
            "started_price": it["started_price"],
            "started_at": it["started_at"].isoformat() if it.get("started_at") else None,  # ← safe
            "current_price": int(live_price) if isinstance(live_price, (int, float)) else None,
            "is_extinct": bool(live.get("isExtinct", False)),
            "updated_at": live.get("updatedAt"),
            "change": change,
            "change_pct": change_pct,
            "notes": it["notes"],
            "name": None,
            "rating": None,
            "club": None,
            "nation": None,
        })

    return {"ok": True, "items": enriched}

@router.get("/usage")
async def usage(request: Request, wdb = Depends(get_watch_db)):
    ent = await compute_entitlements(request)
    uid = _uid_param(request)
    used = await wdb.fetchval(
        "SELECT COUNT(*) FROM watchlist WHERE user_id=$1",
        uid,
    )
    return {
        "used": int(used or 0),
        "max": int(ent["limits"]["watchlist_max"]),
        "is_premium": bool(ent["is_premium"]),
    }

@router.post("")
async def add_watch_item(payload: WatchlistCreate, request: Request, wdb = Depends(get_watch_db)):
    ent = await compute_entitlements(request)
    uid = _uid_param(request)

    used = await wdb.fetchval(
        "SELECT COUNT(*) FROM watchlist WHERE user_id=$1",
        uid,
    )
    max_allowed = int(ent["limits"]["watchlist_max"])
    if int(used or 0) >= max_allowed:
        raise HTTPException(
            402,
            detail={
                "error": "limit_reached",
                "feature": "watchlist",
                "message": f"Free plan allows up to {max_allowed} watchlist players.",
                "upgrade_url": "/billing",
            },
        )

    plat = _plat(payload.platform)
    live = await _fetch_price(int(payload.card_id), plat)
    val = live.get("price")
    live_price = int(val) if isinstance(val, (int, float)) else None
    start_price = live_price if isinstance(live_price, (int, float)) else 0

    row = await wdb.fetchrow(
        f"""
        INSERT INTO watchlist (
            user_id, card_id, player_name, version, platform,
            started_price, last_price, last_checked, notes
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,NOW(),$8)
        ON CONFLICT (user_id, card_id, platform) DO UPDATE
          SET player_name = EXCLUDED.player_name,
              version     = EXCLUDED.version,
              notes       = EXCLUDED.notes,
              last_price  = EXCLUDED.last_price,
              last_checked= NOW()
        RETURNING id
        """,
        uid,
        int(payload.card_id),
        payload.player_name,
        payload.version,
        plat,
        start_price,
        live_price,
        payload.notes,
    )

    return {
        "ok": True,
        "id": row["id"],
        "start_price": start_price,
        "is_extinct": bool(live.get("isExtinct", False)),
    }

@router.delete("/{watch_id}")
async def delete_watch_item(watch_id: int, request: Request, wdb = Depends(get_watch_db)):
    uid = _uid_param(request)
    res = await wdb.execute(
        "DELETE FROM watchlist WHERE id=$1 AND user_id=$2",
        watch_id,
        uid,
    )
    if res == "DELETE 0":
        raise HTTPException(404, "Watch item not found")
    return {"ok": True}

@router.post("/{watch_id}/refresh")
async def refresh_watch_item(
    watch_id: int,
    request: Request,
    wdb = Depends(get_watch_db),
):
    uid = _uid_param(request)
    w = await wdb.fetchrow(
        "SELECT * FROM watchlist WHERE id=$1 AND user_id=$2",
        watch_id,
        uid,
    )
    if not w:
        raise HTTPException(404, "Watch item not found")

    plat = _plat(w["platform"])
    live = await _fetch_price(int(w["card_id"]), plat)
    val = live.get("price")
    live_price = int(val) if isinstance(val, (int, float)) else None

    await wdb.execute(
        "UPDATE watchlist SET last_price=$1, last_checked=NOW() WHERE id=$2",
        live_price,
        watch_id,
    )

    change = change_pct = None
    if isinstance(live_price, (int, float)) and int(w["started_price"] or 0) > 0:
        change = int(live_price) - int(w["started_price"])
        change_pct = round((change / int(w["started_price"])) * 100, 2)

    return {
        "ok": True,
        "item": {
            "id": w["id"],
            "card_id": w["card_id"],
            "player_name": w["player_name"],
            "version": w["version"],
            "platform": w["platform"],
            "started_price": w["started_price"],
            "started_at": w["started_at"].isoformat() if w["started_at"] else None,  # ← safe
            "current_price": live_price,
            "is_extinct": bool(live.get("isExtinct", False)),
            "updated_at": live.get("updatedAt"),
            "change": change,
            "change_pct": change_pct,
            "notes": w["notes"],
            "name": None,
            "rating": None,
            "club": None,
            "nation": None,
        },
    }
