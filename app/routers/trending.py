# app/routers/trending.py
from __future__ import annotations

import asyncio
import re
import time
from typing import Literal, List, Optional, Dict, Tuple

import aiohttp
from bs4 import BeautifulSoup
from fastapi import APIRouter, Request, HTTPException, Query
from pydantic import BaseModel

from app.auth.entitlements import compute_entitlements

router = APIRouter(prefix="/api", tags=["trending"])

# ------------------ Config ------------------
MOMENTUM_BASE = "https://www.fut.gg/players/momentum"
REQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": "https://www.fut.gg/",
}
FUTGG_PRICE_URL = "https://www.fut.gg/api/fut/player-prices/26/{card_id}"

# Light in-memory cache for momentum pages
_CACHE: Dict[Tuple[str, int], Tuple[float, str]] = {}
CACHE_TTL = 120  # seconds

_CARD_HREF_RE = re.compile(r"/players/(\d+)/.+-(\d+)/?$", re.IGNORECASE)
_PCT_RE = re.compile(r"([+\-]?\s?\d+(?:\.\d+)?)\s*%")

# ------------------ Models ------------------
class TrendingOut(BaseModel):
    type: Literal["risers", "fallers", "smart"]
    timeframe: Literal["4h", "6h", "24h"]
    items: List[dict]
    limited: bool = False

# ------------------ Helpers ------------------
def _norm_tf(tf: Optional[str]) -> str:
    """Return '6'|'12'|'24' from input like '6' or '6h'."""
    if not tf:
        return "24"
    tf = tf.lower().strip()
    if tf.endswith("h"):
        tf = tf[:-1]
    return tf if tf in {"6", "12", "24"} else "24"

def _human_tf(tf_num: str) -> str:
    """Return '6h'|'12h'|'24h' from '6'|'12'|'24'."""
    return f"{tf_num}h"

async def _fetch_html(session: aiohttp.ClientSession, url: str) -> str:
    try:
        async with session.get(url, headers=REQ_HEADERS) as r:
            if r.status != 200:
                raise HTTPException(status_code=502, detail=f"Upstream {r.status}")
            return await r.text()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Fetch failed: {e}") from e

async def _momentum_page(tf: str, page: int) -> str:
    """Fetch (with cache) a FUT.GG momentum page."""
    now = time.time()
    key = (tf, page)
    hit = _CACHE.get(key)
    if hit and (now - hit[0] < CACHE_TTL):
        return hit[1]

    url = f"{MOMENTUM_BASE}/{tf}/?page={page}"
    timeout = aiohttp.ClientTimeout(total=12)
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        html = await _fetch_html(sess, url)

    _CACHE[key] = (now, html)
    return html

def _parse_last_page_num(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    last = 1
    for a in soup.find_all("a", href=True):
        href = a.get("href") or ""
        if "page=" in href:
            try:
                n = int(href.split("page=", 1)[1].split("&", 1)[0])
                last = max(last, n)
            except Exception:
                continue
        else:
            t = (a.text or "").strip()
            if t.isdigit():
                last = max(last, int(t))
    return last

def _extract_items(html: str) -> List[dict]:
    """Return [{'card_id': int, 'percent': float}, ...] from a momentum page."""
    soup = BeautifulSoup(html, "html.parser")
    found: List[Tuple[str, str]] = []

    # Find links that carry the card id via canonical /players/...-<card_id> path
    for a in soup.find_all("a", href=True):
        m = _CARD_HREF_RE.search(a["href"])
        if not m:
            continue
        # bubble a bit to find text containing %
        node = a
        for _ in range(4):
            if node is None or getattr(node, "name", None) == "body":
                break
            text = node.get_text(" ", strip=True)
            if "%" in text:
                found.append((m.group(2), text))
                break
            node = node.parent

    items: List[dict] = []
    seen = set()
    for card_id, txt in found:
        if card_id in seen:
            continue
        m = _PCT_RE.search(txt)
        if not m:
            continue
        try:
            pct = float(m.group(1).replace(" ", ""))
            items.append({"card_id": int(card_id), "percent": pct})
            seen.add(card_id)
        except Exception:
            continue
    return items

async def _page_items(tf: str, page: int) -> List[dict]:
    html = await _momentum_page(tf, page)
    return _extract_items(html)

async def _get_console_price(card_id: int, platform: str = "ps") -> Optional[int]:
    url = FUTGG_PRICE_URL.format(card_id=card_id)
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(url, headers=REQ_HEADERS) as r:
                if r.status != 200:
                    return None
                data = await r.json()
    except Exception:
        return None

    plat_key = "ps" if platform == "ps" else "xbox"
    try:
        price = data.get("prices", {}).get(plat_key, {}).get("price")
        if isinstance(price, (int, float)) and price > 0:
            return int(price)
    except Exception:
        pass
    return None

async def _enrich_meta(req: Request, rows: List[dict]) -> List[dict]:
    if not rows:
        return []
    ids = [str(x["card_id"]) for x in rows]
    async with req.app.state.player_pool.acquire() as conn:
        dbrows = await conn.fetch(
            """
            SELECT card_id, name, rating, position, league, nation, club, image_url
            FROM fut_players
            WHERE card_id = ANY($1::text[])
            """,
            ids,
        )
    meta = {int(r["card_id"]): dict(r) for r in dbrows}
    out: List[dict] = []
    for r in rows:
        cid = int(r["card_id"])
        m = meta.get(cid, {})
        out.append({
            "card_id": cid,
            "id": str(cid),
            "name": m.get("name") or f"Card {cid}",
            "rating": m.get("rating"),
            "position": m.get("position"),
            "league": m.get("league"),
            "nation": m.get("nation"),
            "club": m.get("club"),
            "image": m.get("image_url"),
            "percent": float(r["percent"]),
        })
    return out

async def _attach_prices(items: List[dict]) -> List[dict]:
    async def one(it: dict) -> dict:
        it["prices"] = {"console": await _get_console_price(int(it["card_id"]), "ps"), "pc": None}
        return it
    results = await asyncio.gather(*(one(i) for i in items), return_exceptions=True)
    out: List[dict] = []
    for r in results:
        if isinstance(r, Exception):
            continue
        out.append(r)
    return out

async def _fetch_trending(kind: Literal["risers", "fallers"], tf: str, limit: int) -> List[dict]:
    if kind == "fallers":
        items = await _page_items(tf, 1)  # biggest fallers early pages
        items.sort(key=lambda x: x["percent"])  # most negative first
        return items[:limit]
    else:
        # for risers, take last page and sort desc
        first_html = await _momentum_page(tf, 1)
        last = _parse_last_page_num(first_html)
        items = await _page_items(tf, last)
        items.sort(key=lambda x: x["percent"], reverse=True)
        return items[:limit]

# ------------------ Route ------------------
@router.get("/trending", response_model=TrendingOut)
async def trending(
    req: Request,
    type_: Literal["risers", "fallers", "smart"] = Query("risers", alias="type"),
    # accept "24" or "24h" (and 6/12 variants) from the FE
    tf_raw: str = Query("24h", alias="tf"),
    limit: int = Query(10, ge=1, le=50),
):
    """
    Dashboard fetch: /api/trending?type=fallers&tf=24  (or tf=24h)
    """
    ent = await compute_entitlements(req)
    limits = ent.get("limits", {}).get("trending", {"timeframes": ["24h"], "limit": 5})
    limited = False

    # Premium gate for Smart
    if type_ == "smart" and not ent.get("is_premium", False):
        raise HTTPException(
            status_code=402,
            detail={
                "error": "payment_required",
                "feature": "smart_trending",
                "message": "Smart Trending is a premium feature.",
                "upgrade_url": "/billing",
            },
        )

    # Normalise timeframe + present in 'xh' form for UI/limits
    tf_num = _norm_tf(tf_raw)        # "24h"/"24" -> "24"
    tf_human = _human_tf(tf_num)     # "24" -> "24h"

    if tf_human not in limits.get("timeframes", ["24h"]):
        tf_num = "24"
        tf_human = "24h"
        limited = True

    max_items = int(limits.get("limit", 5))
    if limit > max_items:
        limit = max_items
        limited = True

    # Smart movers (flip 6h vs 24h)
    if type_ == "smart":
        f6  = await _fetch_trending("fallers", "6",  limit=50)
        r6  = await _fetch_trending("risers",  "6",  limit=50)
        f24 = await _fetch_trending("fallers", "24", limit=50)
        r24 = await _fetch_trending("risers",  "24", limit=50)

        f6m  = {int(x["card_id"]): float(x["percent"]) for x in f6}
        r6m  = {int(x["card_id"]): float(x["percent"]) for x in r6}
        f24m = {int(x["card_id"]): float(x["percent"]) for x in f24}
        r24m = {int(x["card_id"]): float(x["percent"]) for x in r24}

        smart_ids: set[int] = set()
        smart_map: Dict[int, Dict[str, float]] = {}

        # riser 6h, faller 24h
        for cid, p6 in r6m.items():
            if cid in f24m:
                smart_ids.add(cid)
                smart_map[cid] = {"chg6hPct": p6, "chg24hPct": f24m[cid]}

        # faller 6h, riser 24h
        for cid, p6 in f6m.items():
            if cid in r24m:
                smart_ids.add(cid)
                smart_map[cid] = {"chg6hPct": p6, "chg24hPct": r24m[cid]}

        raw = [{"card_id": cid, "percent": smart_map[cid]["chg6hPct"]} for cid in smart_ids]
        enriched = await _enrich_meta(req, raw)
        enriched = await _attach_prices(enriched)
        for e in enriched:
            cid = int(e["card_id"])
            pair = smart_map.get(cid, {})
            e["trend"] = {"chg6hPct": pair.get("chg6hPct"), "chg24hPct": pair.get("chg24hPct")}
        enriched.sort(key=lambda x: abs(x["trend"].get("chg6hPct") or 0), reverse=True)
        return {"type": "smart", "timeframe": tf_human, "items": enriched[:limit], "limited": limited}

    # Simple risers/fallers
    raw = await _fetch_trending(kind=type_, tf=tf_num, limit=limit)
    enriched = await _enrich_meta(req, raw)
    enriched = await _attach_prices(enriched)
    for e in enriched:
        e["trend"] = {"chg24hPct": float(e["percent"]) if tf_num == "24" else None}

    return {"type": type_, "timeframe": tf_human, "items": enriched[:limit], "limited": limited}
