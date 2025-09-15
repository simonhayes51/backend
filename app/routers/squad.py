from fastapi import APIRouter, Request, HTTPException, Query
from typing import Dict, Any, List
import re
import asyncio

router = APIRouter()

# ---- If you already have a scraper in your codebase ----
# e.g. services/prices.py with: async def get_player_price(card_id: int, platform: str) -> int: ...
try:
    from services.prices import get_player_price  # your existing Player-Search scraper
except Exception:
    get_player_price = None

# ---- Lightweight FUT.GG JSON fallback (only used if your scraper import fails) ----
import aiohttp

async def _fallback_price_futgg(card_id: int, platform: str = "ps") -> int | None:
    url = f"https://www.fut.gg/api/fut/player-prices/25/{card_id}"
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout, headers={"Accept":"application/json"}) as sess:
            async with sess.get(url) as r:
                if r.status != 200:
                    return None
                data = await r.json()
                # data format seen previously: {"data":{"currentPrice":{"price": 40000, ...}, ...}}
                price = data.get("data", {}).get("currentPrice", {}).get("price")

                if isinstance(price, int):
                    return price
    except Exception:
        return None
    return None

def _mk_like(q: str) -> List[str]:
    tokens = [t for t in re.split(r"\s+", q.strip()) if t]
    return tokens[:6]

def _position_aliases(token: str) -> List[str]:
    t = token.upper()
    pos = {"GK","LB","LCB","RCB","RB","LWB","RWB","CDM","CM","CAM","LM","RM","LW","RW","ST","CF"}
    return [t] if t in pos else []

@router.get("/search-players")
async def search_players(request: Request, q: str, limit: int = 50) -> Dict[str, Any]:
    pool = request.app.state.pool
    tokens = _mk_like(q)

    where_clauses = []
    params = []
    for tok in tokens:
        where_clauses.append("(name ILIKE '%' || $%d || '%' OR club ILIKE '%' || $%d || '%' OR league ILIKE '%' || $%d || '%' OR nation ILIKE '%' || $%d || '%')" % (len(params)+1, len(params)+1, len(params)+1, len(params)+1))
        params.append(tok)
        # positions
        if _position_aliases(tok):
            where_clauses.append("EXISTS (SELECT 1 FROM unnest(positions) p WHERE p = $%d)" % (len(params)+1))
            params.append(tok.upper())

    where_sql = " AND ".join(where_clauses) if where_clauses else "TRUE"
    sql = f"""
    SELECT id, name, rating, nation, league, club, positions, is_icon, is_hero
    FROM fut_players
    WHERE {where_sql}
    ORDER BY rating DESC, name ASC
    LIMIT {min(max(limit,1),100)}
    """

    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
        players = [{
            "id": r["id"],
            "name": r["name"],
            "rating": r["rating"],
            "nation": r["nation"],
            "league": r["league"],
            "club": r["club"],
            "positions": r["positions"],
            "isIcon": r["is_icon"],
            "isHero": r["is_hero"],
        } for r in rows]
        return {"players": players}

@router.get("/prices")
async def get_prices(request: Request, ids: str = Query(..., description="comma-separated player ids"), platform: str = "ps") -> Dict[str, int]:
    platform = platform.lower()
    if platform not in {"ps","xb","pc"}:
        raise HTTPException(status_code=400, detail="platform must be ps|xb|pc")

    try:
        id_list = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    except Exception:
        raise HTTPException(status_code=400, detail="ids must be comma-separated integers")

    if not id_list:
        return {}

    results: Dict[str, int] = {}

    async def fetch_one(pid: int):
        # Prefer user's scraper
        if get_player_price is not None:
            try:
                val = await get_player_price(pid, platform)
                if isinstance(val, (int, float)):
                    results[str(pid)] = int(val)
                    return
            except Exception:
                pass
        # Fallback to FUT.GG JSON
        val = await _fallback_price_futgg(pid, platform)
        if isinstance(val, int):
            results[str(pid)] = val

    await asyncio.gather(*(fetch_one(pid) for pid in id_list))
    return results
