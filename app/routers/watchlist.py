# app/routers/watchlist.py
#
# This is the only live watchlist implementation - user-facing CRUD against
# the `watchlist` table on the separate WATCHLIST_DATABASE_URL pool
# (get_watchlist_db). A near-duplicate, unmounted router used to exist at
# app/routes/watchlist.py against a same-named `watchlist` table on the core
# pool instead - confirmed dead (never imported by main.py) and removed.
#
# Threshold alerting (price/liquidity, quiet hours, cool-off, DM +
# channel fallback) is live today, but not via a separate service module
# - it's main.py's own /api/watchlist-alerts endpoints + _alerts_poll_loop
# / _eval_alerts_for_pair, against a `watchlist_alerts` table on this same
# WATCHLIST_DATABASE_URL pool. app/services/watchlist_engine.py (a
# near-identical, earlier implementation against a differently-named
# `watchlist_items` table) was a dead superseded duplicate - never wired
# up - and has been removed; don't resurrect it or add a third
# "watchlist" concept alongside these two.
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional, AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.auth.entitlements import compute_entitlements
from app.db import get_core_pool, get_watchlist_db, get_player_db, get_player_pool
from app.futbin_client import fetch_price_by_card_id
from app.services.market_data_provider import FutggMarketDataProvider
from app.services.player_card_ondemand import ensure_cards_requested

log = logging.getLogger("watchlist")

VALID_SOURCES = {"futbin", "futgg"}


async def _futgg_provider() -> FutggMarketDataProvider:
    return FutggMarketDataProvider(await get_core_pool())

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

# ------------ futbin.com live price fetch, behind a short cache --------------
# 5-second in-process cache so bursts of requests for the same card+platform
# (e.g. list_watch_items fanning out over several rows) don't refetch futbin
# redundantly; still effectively "live" for a user-facing price.
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

    last_err = None
    for attempt in (0, 1, 2):
        try:
            price = await fetch_price_by_card_id(card_id, platform)
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
    # "futbin" (default, existing behavior) or "futgg" - which catalogue
    # card_id belongs to, so add/list/refresh all know which lookup/price
    # path to use for this row. Not validated against a live lookup here
    # (the caller already knows which card they're adding); an
    # unrecognized value falls back to "futbin" rather than erroring, so
    # older frontend builds that never send this field keep working.
    source: Optional[str] = "futbin"

# ------------ Endpoints --------------------------------------------------------
@router.get("")
async def list_watch_items(
    request: Request,
    wdb = Depends(get_watch_db),
    pdb = Depends(get_player_db),
):
    uid = _uid_param(request)
    rows = await wdb.fetch(
        "SELECT * FROM watchlist WHERE user_id=$1 ORDER BY started_at DESC NULLS LAST",
        uid,
    )
    items = [dict(r) for r in rows]
    if not items:
        return {"ok": True, "items": []}

    # Batch meta lookup (card_id is BIGINT), split by source - a FUT.GG
    # source_card_id will never match a row in the legacy fut_players
    # table (different id space entirely), so querying it unconditionally
    # for every row silently returned nothing for FUT.GG entries.
    futbin_ids = [int(it["card_id"]) for it in items if it.get("card_id") is not None and it.get("source") != "futgg"]
    futgg_ids = [int(it["card_id"]) for it in items if it.get("card_id") is not None and it.get("source") == "futgg"]

    meta_rows = await pdb.fetch(
        """
        SELECT card_id, name, rating, club, nation, version, image_url,
               card_bg_image, card_cutout_image, card_cutout_type, card_name,
               generated_card_url, generated_card_status, generated_card_flagged
        FROM fut_players
        WHERE card_id = ANY($1::bigint[])
        """,
        futbin_ids,
    )
    meta_map = {
        int(m["card_id"]): {
            "name": m["name"],
            "rating": m["rating"],
            "club": m["club"],
            "nation": m["nation"],
            "version": m["version"],
            "image_url": m["image_url"],
            "card_bg_image": m["card_bg_image"],
            "card_cutout_image": m["card_cutout_image"],
            "card_cutout_type": m["card_cutout_type"],
            "card_name": m["card_name"],
            "generated_card_url": m["generated_card_url"],
            "generated_card_status": m["generated_card_status"],
            "generated_card_flagged": m["generated_card_flagged"],
        }
        for m in meta_rows
    }

    futgg_provider = await _futgg_provider()
    futgg_rows = await futgg_provider.get_players_by_ids(futgg_ids)

    needs_card = [
        str(cid) for cid, m in meta_map.items()
        if m.get("generated_card_status") != "ready" or m.get("generated_card_flagged")
    ]
    if needs_card:
        try:
            player_pool = await get_player_pool()
            await ensure_cards_requested(player_pool, needs_card)
        except Exception:
            # Card rendering is an enhancement. A queue/database problem must
            # never stop users from opening their saved watchlist.
            pass

    enriched = []
    for it in items:
        card_id = int(it["card_id"]) if it.get("card_id") is not None else None
        is_futgg = it.get("source") == "futgg"

        if is_futgg:
            fg = futgg_rows.get(card_id)
            # FUT.GG's price already lives on its own tiered refresh
            # schedule (futgg_price_sync.py) - read the live current_bin
            # straight from the snapshot rather than the row's own
            # (possibly older) last_price column, no extra request needed.
            live_price = fg.get("current_bin") if fg else None
            is_extinct = fg is None or fg.get("is_tradeable") is False
            m = {
                "name": fg.get("name") if fg else it["player_name"],
                "rating": fg.get("rating") if fg else None,
                "club": fg.get("club") if fg else None,
                "nation": fg.get("nation") if fg else None,
                "version": fg.get("rarity") if fg else it.get("version"),
                "image_url": fg.get("player_image_url") if fg else None,
                "card_bg_image": None, "card_cutout_image": None, "card_cutout_type": None,
                "card_name": fg.get("name") if fg else None,
                "generated_card_url": None, "generated_card_status": None, "generated_card_flagged": None,
            }
        else:
            # The list endpoint must be fast and dependable. Use the last
            # stored price here; the explicit /refresh action is
            # responsible for making the external live-price call and
            # updating these fields.
            live_price = it.get("last_price")
            is_extinct = False
            m = meta_map.get(card_id, {}) if card_id is not None else {}

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
            "source": it.get("source", "futbin"),
            "player_name": it["player_name"],
            "version": it["version"],
            "platform": it["platform"],
            "started_price": it["started_price"],
            "started_at": it["started_at"].isoformat() if it.get("started_at") else None,  # ← safe
            "current_price": int(live_price) if isinstance(live_price, (int, float)) else None,
            "is_extinct": is_extinct,
            "updated_at": it["last_checked"].isoformat() if it.get("last_checked") else None,
            "change": change,
            "change_pct": change_pct,
            "notes": it["notes"],
            "name": m.get("name"),
            "rating": m.get("rating"),
            "club": m.get("club"),
            "nation": m.get("nation"),
            "version": m.get("version"),
            "image_url": m.get("image_url"),
            "card_bg_image": m.get("card_bg_image"),
            "card_cutout_image": m.get("card_cutout_image"),
            "card_cutout_type": m.get("card_cutout_type"),
            "card_name": m.get("card_name"),
            "generated_card_url": m.get("generated_card_url"),
            "generated_card_status": m.get("generated_card_status"),
            "generated_card_flagged": m.get("generated_card_flagged"),
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

    source = payload.source if payload.source in VALID_SOURCES else "futbin"
    plat = _plat(payload.platform)

    is_extinct = False
    if source == "futgg":
        # FUT.GG is console/PC-agnostic (no platform split) - live is
        # whatever futgg_price_sync.py last captured, not a fresh fetch
        # (that already happens on its own tiered schedule). A card
        # discovered but not yet priced is still a valid watch target;
        # start_price/live_price just start out None until it is.
        provider = await _futgg_provider()
        row_data = await provider.get_player(int(payload.card_id))
        live_price = row_data.get("current_bin") if row_data else None
        is_extinct = row_data is None or row_data.get("is_tradeable") is False
    else:
        live = await _fetch_price(int(payload.card_id), plat)
        val = live.get("price")
        live_price = int(val) if isinstance(val, (int, float)) else None
        is_extinct = bool(live.get("isExtinct", False))
    start_price = live_price if isinstance(live_price, (int, float)) else 0

    row = await wdb.fetchrow(
        f"""
        INSERT INTO watchlist (
            user_id, card_id, player_name, version, platform,
            started_price, last_price, last_checked, notes, source
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,NOW(),$8,$9)
        ON CONFLICT (user_id, card_id, platform) DO UPDATE
          SET player_name = EXCLUDED.player_name,
              version     = EXCLUDED.version,
              notes       = EXCLUDED.notes,
              last_price  = EXCLUDED.last_price,
              last_checked= NOW(),
              source      = EXCLUDED.source
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
        source,
    )

    return {
        "ok": True,
        "id": row["id"],
        "start_price": start_price,
        "is_extinct": is_extinct,
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
    pdb = Depends(get_player_db),
):
    uid = _uid_param(request)
    w = await wdb.fetchrow(
        "SELECT * FROM watchlist WHERE id=$1 AND user_id=$2",
        watch_id,
        uid,
    )
    if not w:
        raise HTTPException(404, "Watch item not found")

    is_futgg = w.get("source") == "futgg"

    if is_futgg:
        # A user hitting refresh is a high-intent "I care about this card
        # right now" signal - same rationale as the player-detail page's
        # refresh-on-view bump, so pull the FUT.GG scraper's next pass for
        # this card forward instead of only reading whatever it last had.
        provider = await _futgg_provider()
        try:
            await provider.bump_price_priority(int(w["card_id"]))
        except Exception:
            log.warning("bump_price_priority failed for card_id=%s", w["card_id"], exc_info=True)
        row_data = await provider.get_player(int(w["card_id"]))
        live_price = row_data.get("current_bin") if row_data else None
        is_extinct = row_data is None or row_data.get("is_tradeable") is False
        meta_dict = {
            "name": row_data.get("name") if row_data else None,
            "rating": row_data.get("rating") if row_data else None,
            "club": row_data.get("club") if row_data else None,
            "nation": row_data.get("nation") if row_data else None,
        }
        updated_at = None
    else:
        plat = _plat(w["platform"])
        live = await _fetch_price(int(w["card_id"]), plat)
        val = live.get("price")
        live_price = int(val) if isinstance(val, (int, float)) else None
        is_extinct = bool(live.get("isExtinct", False))
        updated_at = live.get("updatedAt")

        meta = await pdb.fetchrow(
            """
            SELECT card_id, name, rating, club, nation
            FROM fut_players
            WHERE card_id::text = $1
            """,
            str(w["card_id"]),
        )
        meta_dict = dict(meta) if meta else {}

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
            "source": w.get("source", "futbin"),
            "player_name": w["player_name"],
            "version": w["version"],
            "platform": w["platform"],
            "started_price": w["started_price"],
            "started_at": w["started_at"].isoformat() if w["started_at"] else None,  # ← safe
            "current_price": live_price,
            "is_extinct": is_extinct,
            "updated_at": updated_at,
            "change": change,
            "change_pct": change_pct,
            "notes": w["notes"],
            "name": meta_dict.get("name"),
            "rating": meta_dict.get("rating"),
            "club": meta_dict.get("club"),
            "nation": meta_dict.get("nation"),
        },
    }
