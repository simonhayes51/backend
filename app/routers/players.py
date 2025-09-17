# app/routers/players.py

from __future__ import annotations

import os
import aiohttp
from typing import AsyncGenerator, Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

# If you already have this service, it's safe to import (it doesn't import main)
from app.services.price_history import get_price_history

router = APIRouter(prefix="/api/players", tags=["players"])

# ------------------------------
# DB dependency (NO import from main.py)
# ------------------------------
async def get_player_db(request: Request) -> AsyncGenerator:
    """
    Use the player pool attached on app.state in main.lifespan.
    Avoids importing from main and prevents circular imports.
    """
    pool = getattr(request.app.state, "player_pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="player_pool not initialized")
    async with pool.acquire() as conn:
        yield conn

# ------------------------------
# Helpers
# ------------------------------
FUTGG_BASE = "https://www.fut.gg/api/fut/player-prices/26"

def _plat(p: str) -> str:
    p = (p or "").lower()
    if p in ("ps", "playstation", "console"):
        return "ps"
    if p in ("xbox", "xb"):
        return "xbox"
    if p in ("pc", "origin"):
        return "pc"
    return "ps"

def _pick_platform_node(current: Dict[str, Any], platform: str) -> Dict[str, Any]:
    # supports both flat and per-platform shapes
    if any(k in current for k in ("ps", "xbox", "pc", "playstation")):
        key_map = {"ps": "ps", "xbox": "xbox", "pc": "pc", "console": "ps"}
        k = key_map.get(platform, "ps")
        node = current.get(k)
        if not node and k == "ps":
            node = current.get("playstation")
        return node or {}
    return current

# ------------------------------
# Endpoints
# ------------------------------

@router.get("/resolve")
async def resolve_player_by_name(
    name: str = Query(..., description="Player name to resolve"),
    conn = Depends(get_player_db),
):
    """
    Resolve a player by name to get their card details
    """
    try:
        # Search for exact match first, then fuzzy match
        row = await conn.fetchrow(
            """
            SELECT card_id, name, rating, version, image_url, club, league, nation, position, altposition, price_num, price
            FROM fut_players 
            WHERE LOWER(name) = LOWER($1)
            ORDER BY rating DESC
            LIMIT 1
            """,
            name.strip()
        )
        
        if not row:
            # Try fuzzy matching
            row = await conn.fetchrow(
                """
                SELECT card_id, name, rating, version, image_url, club, league, nation, position, altposition, price_num, price
                FROM fut_players 
                WHERE LOWER(name) ILIKE LOWER($1)
                ORDER BY rating DESC
                LIMIT 1
                """,
                f"%{name.strip()}%"
            )
        
        if not row:
            raise HTTPException(status_code=404, detail="Player not found")
        
        return {
            "card_id": int(row["card_id"]),
            "name": row["name"],
            "rating": row["rating"],
            "version": row["version"],
            "image_url": row["image_url"],
            "club": row["club"],
            "league": row["league"],
            "nation": row["nation"],
            "position": row["position"],
            "altposition": row["altposition"],
            "price": row["price"],
            "price_num": row["price_num"],
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Resolution failed: {e}")

@router.get("/search")
async def search_players(
    q: str = Query("", description="name or card_id substring"),
    pos: Optional[str] = Query(None, description="exact position code like ST, CAM, CB"),
    limit: int = Query(50, description="max results"),
    conn = Depends(get_player_db),
):
    q = (q or "").strip()
    p = (pos or "").strip().upper() or None

    where = []
    params: List[Any] = []

    if q:
        where.append("(LOWER(name) LIKE LOWER($1) OR card_id::text LIKE $1)")
        params.append(f"%{q}%")

    if p:
        params.append(p)
        idx = len(params)  # position param index
        where.append(
            f"""
            (
              UPPER(position) = ${idx}
              OR (
                COALESCE(altposition, '') <> ''
                AND EXISTS (
                  SELECT 1
                  FROM regexp_split_to_table(altposition, '[,;/|\\s]+') ap
                  WHERE UPPER(TRIM(ap)) = ${idx}
                )
              )
            )
            """
        )

    base_where = " AND ".join(where) if where else "TRUE"

    # Compute the placeholder index for LIMIT
    limit_idx = len(params) + 1

    sql = f"""
        SELECT
          card_id, name, rating, version, image_url, club, league, nation,
          position, altposition, price, price_num
        FROM fut_players
        WHERE {base_where}
        ORDER BY
          CASE WHEN price IS NULL THEN 1 ELSE 0 END,
          rating DESC NULLS LAST,
          name ASC
        LIMIT ${limit_idx}
    """

    params.append(limit)

    rows = await conn.fetch(sql, *params)

    players = [
        {
            "card_id": int(r["card_id"]),
            "name": r["name"],
            "rating": r["rating"],
            "version": r["version"],
            "image_url": r["image_url"],
            "club": r["club"],
            "league": r["league"],
            "nation": r["nation"],
            "position": r["position"],
            "altposition": r["altposition"],
            "price": r["price"],
            "price_num": r["price_num"],
        }
        for r in rows
    ]

    return {"players": players, "data": players}

@router.get("/autocomplete")
async def players_autocomplete(
    q: str = Query("", description="name or card_id substring"),
    pos: Optional[str] = Query(None, description="position filter like ST, CAM, CB"),
    conn = Depends(get_player_db),
):
    """
    Lightweight autocomplete list for UI dropdowns.
    Returns items with {value, label, card_id, name, rating, version, image_url, position}.
    """
    q = (q or "").strip()
    p = (pos or "").strip().upper() or None

    where = []
    params: List[Any] = []

    if q:
        where.append("(LOWER(name) LIKE LOWER($1) OR card_id::text LIKE $1)")
        params.append(f"%{q}%")

    if p:
        params.append(p)
        idx = len(params)
        where.append(
            f"""
            (
              UPPER(position) = ${idx}
              OR (
                COALESCE(altposition, '') <> ''
                AND EXISTS (
                  SELECT 1
                  FROM regexp_split_to_table(altposition, '[,;/|\\s]+') ap
                  WHERE UPPER(TRIM(ap)) = ${idx}
                )
              )
            )
            """
        )

    base_where = " AND ".join(where) if where else "TRUE"
    sql = f"""
        SELECT card_id, name, rating, version, image_url, position
        FROM fut_players
        WHERE {base_where}
        ORDER BY rating DESC NULLS LAST, name ASC
        LIMIT 20
    """
    rows = await conn.fetch(sql, *params)

    items: List[Dict[str, Any]] = []
    for r in rows:
        cid = int(r["card_id"])
        name = r["name"]
        rating = r["rating"]
        ver = r["version"] or ""
        pos_label = r["position"] or ""
        label = f"{name} ({rating}) {ver} {pos_label}".strip()
        items.append({
            "value": cid,          # for <Select/> components
            "label": label,
            "card_id": cid,
            "name": name,
            "rating": rating,
            "version": ver,
            "image_url": r["image_url"],
            "position": pos_label,
        })

    return {"items": items}

@router.get("/{card_id}")
async def get_player(card_id: str, conn = Depends(get_player_db)):
    """
    Return a single player's metadata from fut_players by card_id.
    """
    row = await conn.fetchrow(
        """
        SELECT card_id, name, rating, version, image_url, club, league, nation, position, altposition, price, price_num
        FROM fut_players
        WHERE card_id = $1::text
        """,
        str(card_id),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Player not found")
    d = dict(row)
    d["card_id"] = int(d["card_id"])
    return d

@router.get("/{card_id}/meta")
async def get_player_meta(card_id: str, conn = Depends(get_player_db)):
    """
    Small alias for metadata (same as GET /{card_id}) to keep clients flexible.
    """
    return await get_player(card_id, conn)  # type: ignore[arg-type]

@router.get("/{card_id}/price")
async def get_player_price_route(
    card_id: int,
    platform: str = Query("ps", description="ps|xbox|pc|console"),
    conn = Depends(get_player_db),
):
    """
    Return latest price preferring local data:
      1) fut_candles last close (per platform)
      2) fut_players.price_num snapshot
      3) FUT.GG currentPrice (fallback)
    """
    plat = _plat(platform)

    # 1) Try fut_candles (latest close)
    try:
        row = await conn.fetchrow(
            """
            SELECT close, open_time
            FROM fut_candles
            WHERE player_card_id = $1::text
              AND (platform = $2 OR platform IN ('ps','playstation','console'))
            ORDER BY open_time DESC
            LIMIT 1
            """,
            str(card_id), plat,
        )
        if row and row["close"] is not None:
            return {
                "card_id": card_id,
                "platform": plat,
                "price": int(row["close"]),
                "isExtinct": False,
                "updatedAt": row["open_time"],
                "source": "candles",
            }
    except Exception:
        pass

    # 2) Try fut_players snapshot (what Best Buys uses)
    try:
        row = await conn.fetchrow(
            """
            SELECT price_num, updated_at
            FROM fut_players
            WHERE card_id = $1::text
            """,
            str(card_id),
        )
        if row and row["price_num"] is not None:
            return {
                "card_id": card_id,
                "platform": plat,
                "price": int(row["price_num"]),
                "isExtinct": False,
                "updatedAt": row["updated_at"],
                "source": "players",
            }
    except Exception:
        pass

    # 3) FUT.GG fallback
    try:
        url = f"{FUTGG_BASE}/{card_id}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-GB,en;q=0.9",
            "Referer": "https://www.fut.gg/",
            "Origin": "https://www.fut.gg",
        }
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(url, headers=headers) as r:
                if r.status == 200:
                    js = await r.json()
                    current = (js.get("data") or {}).get("currentPrice") or {}
                    node = _pick_platform_node(current, plat)
                    return {
                        "card_id": card_id,
                        "platform": plat,
                        "price": node.get("price"),
                        "isExtinct": node.get("isExtinct", False),
                        "updatedAt": node.get("priceUpdatedAt") or current.get("priceUpdatedAt"),
                        "source": "futgg",
                    }
    except Exception:
        pass

    return {
        "card_id": card_id,
        "platform": plat,
        "price": None,
        "isExtinct": False,
        "updatedAt": None,
        "source": "none",
    }


@router.get("/{card_id}/history")
async def get_player_history_route(
    card_id: int,
    platform: str = Query("ps", description="ps|xbox|pc|console"),
    tf: str = Query("today", description="today|24h|7d|30d|all"),
    conn = Depends(get_player_db),
):
    """
    Return OHLC candles for a player.
    Order: FUT.GG service first; if empty, fall back to fut_candles.
    """
    plat = _plat(platform)

    # 1) Try your existing FUT.GG-backed service
    try:
        data = await get_price_history(card_id, plat, tf)
        if isinstance(data, list) and data:
            return {
                "card_id": card_id,
                "platform": plat,
                "tf": tf,
                "history": data,
                "source": "futgg",
            }
    except Exception:
        pass  # fall through to DB fallback

    # 2) DB fallback: fut_candles (player_card_id, platform)
    try:
        # pick a sensible window based on tf (defaults keep UI responsive)
        tf_map = {
            "today": "2 days",   # cover quiet days too
            "24h":  "1 day",
            "7d":   "7 days",
            "30d":  "30 days",
            "all":  None,
        }
        window = tf_map.get(tf, "2 days")

        where = [
            "player_card_id = $1::text",
            "(platform = $2 OR platform IN ('ps','playstation','console'))",
        ]
        params = [str(card_id), plat]

        if window:
            where.append("open_time >= (NOW() AT TIME ZONE 'UTC') - INTERVAL $3")
            params.append(window)

        sql = f"""
            SELECT open_time, open, high, low, close
            FROM fut_candles
            WHERE {' AND '.join(where)}
            ORDER BY open_time ASC
            LIMIT 2000
        """

        rows = await conn.fetch(sql, *params)

        candles = [
            {
                "open_time": r["open_time"],
                "open":   (int(r["open"])   if r["open"]   is not None else None),
                "high":   (int(r["high"])   if r["high"]   is not None else None),
                "low":    (int(r["low"])    if r["low"]    is not None else None),
                "close":  (int(r["close"])  if r["close"]  is not None else None),
            }
            for r in rows
        ]

        # If window returned nothing, fallback to last N rows overall
        if not candles:
            rows = await conn.fetch(
                """
                SELECT open_time, open, high, low, close
                FROM fut_candles
                WHERE player_card_id = $1::text
                  AND (platform = $2 OR platform IN ('ps','playstation','console'))
                ORDER BY open_time ASC
                LIMIT 2000
                """,
                str(card_id), plat,
            )
            candles = [
                {
                    "open_time": r["open_time"],
                    "open": int(r["open"]) if r["open"] is not None else None,
                    "high": int(r["high"]) if r["high"] is not None else None,
                    "low":  int(r["low"])  if r["low"]  is not None else None,
                    "close":int(r["close"])if r["close"]is not None else None,
                }
                for r in rows
            ]

        return {
            "card_id": card_id,
            "platform": plat,
            "tf": tf,
            "history": candles,
            "source": "candles",
        }
    except Exception:
        # Never 500; keep CORS headers intact
        return {"card_id": card_id, "platform": plat, "tf": tf, "history": [], "source": "none"}

@router.get("/batch/meta")
async def batch_meta(
    ids: str = Query(..., description="CSV of card_ids"),
    conn = Depends(get_player_db),
):
    """
    Batch metadata fetch for up to ~100 ids at once.
    """
    raw_ids = [x.strip() for x in ids.split(",") if x.strip()]
    if not raw_ids:
        return {"items": []}
    rows = await conn.fetch(
        """
        SELECT card_id, name, rating, version, image_url, club, league, nation, position, altposition, price, price_num
        FROM fut_players
        WHERE card_id = ANY($1::text[])
        """,
        raw_ids,
    )
    out: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["card_id"] = int(d["card_id"])
        out.append(d)
    return {"items": out}
