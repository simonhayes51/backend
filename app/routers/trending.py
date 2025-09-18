# app/routers/trending.py
from __future__ import annotations

import asyncio
import re
import time
from typing import Literal, List, Dict, Any, Optional

import aiohttp
from bs4 import BeautifulSoup
from fastapi import APIRouter, Request, HTTPException, Query
from pydantic import BaseModel

from app.auth.entitlements import compute_entitlements

router = APIRouter(prefix="/api", tags=["trending"])

# -------- Config --------
MOMENTUM_BASE = "https://www.fut.gg/players/momentum"
MOMENTUM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": "https://www.fut.gg/",
}
FUTGG_API_HEADERS = {
    "User-Agent": MOMENTUM_HEADERS["User-Agent"],
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": "https://www.fut.gg/",
    "Origin": "https://www.fut.gg",
}

# Cache + session
_MOMENTUM_CACHE: dict[tuple[str, int], dict] = {}
MOMENTUM_TTL = 120  # seconds
HTTP_SESSION: Optional[aiohttp.ClientSession] = None

# -------- Helpers --------
_CARD_HREF_RE = re.compile(r"/players/(\d+)/.+-(\d+)/?$", re.IGNORECASE)

def _norm_tf(tf: Optional[str]) -> str:
    if not tf:
        return "24"
    tf = tf.lower().strip()
    if tf.endswith("h"):
        tf = tf[:-1]
    return tf if tf in ("6", "12", "24") else "24"

async def _fetch_momentum_page(tf: str, page: int) -> str:
    now = time.time()
    key = (tf, page)
    hit = _MOMENTUM_CACHE.get(key)
    if hit and (now - hit["at"] < MOMENTUM_TTL):
        return hit["html"]

    url = f"{MOMENTUM_BASE}/{tf}/?page={page}"
    timeout = aiohttp.ClientTimeout(total=12)
    sess = HTTP_SESSION or aiohttp.ClientSession(timeout=timeout, headers=MOMENTUM_HEADERS)
    must_close = sess is not HTTP_SESSION
    try:
        async with sess.get(url, headers=MOMENTUM_HEADERS, timeout=timeout) as r:
            if r.status != 200:
                raise HTTPException(status_code=502, detail=f"MOMENTUM {r.status}")
            html = await r.text()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"MOMENTUM fetch failed: {e}") from e
    finally:
        if must_close:
            await sess.close()

    _MOMENTUM_CACHE[key] = {"html": html, "at": now}
    return html

def _parse_last_page_number(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    nums = []
    for a in soup.find_all("a"):
        href = a.get("href") or ""
        if "page=" in href:
            try:
                n = int(href.split("page=", 1)[1].split("&", 1)[0])
                nums.append(n)
            except Exception:
                continue
        else:
            t = a.text.strip()
            if t.isdigit():
                nums.append(int(t))
    return max(nums) if nums else 1

def _extract_items(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    tiles = []
    for a in soup.find_all("a", href=True):
        m = _CARD_HREF_RE.search(a["href"])
        if not m:
            continue
        node = a
        for _ in range(4):
            if node is None or node.name == "body":
                break
            txt = node.get_text(separator=" ", strip=True)
            if "%" in txt:
                tiles.append((m.group(2), txt))
                break
            node = node.parent

    items = []
    pct_re = re.compile(r"([+\-]?\s?\d+(?:\.\d+)?)\s*%")
    seen = set()
    for cid, text in tiles:
        if cid in seen:
            continue
        m = pct_re.search(text)
        if not m:
            continue
        try:
            pct = float(m.group(1).replace(" ", ""))
            items.append({"card_id": int(cid), "percent": pct})
            seen.add(cid)
        except Exception:
            continue
    return items

async def _momentum_page_items(tf: str, page: int) -> tuple[list[dict], str]:
    html = await _fetch_momentum_page(tf, page)
    return _extract_items(html), html

async def _get_console_price(card_id: int, platform: str = "ps") -> Optional[int]:
    url = f"https://www.fut.gg/api/fut/player-prices/26/{card_id}"
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(url, headers=FUTGG_API_HEADERS) as r:
                if r.status != 200:
                    return None
                data = await r.json()
    except Exception:
        return None

    try:
        plat_key = "ps" if platform == "ps" else ("xbox" if platform == "xbox" else "ps")
        price = data.get("prices", {}).get(plat_key, {}).get("price")
        if isinstance(price, (int, float)) and price > 0:
            return int(price)
    except Exception:
        pass
    return None

async def _enrich_with_meta(req: Request, items: list[dict]) -> list[dict]:
    if not items:
        return []
    ids = [str(it["card_id"]) for it in items]
    async with req.app.state.player_pool.acquire() as pconn:
        rows = await pconn.fetch(
            """
            SELECT card_id, name, rating, position, league, nation, club, image_url
            FROM fut_players
            WHERE card_id = ANY($1::text[])
            """,
            ids,
        )
    meta = {int(r["card_id"]): dict(r) for r in rows}
    out = []
    for it in items:
        cid = int(it["card_id"])
        m = meta.get(cid, {})
        out.append(
            {
                "id": str(cid),
                "card_id": cid,
                "percent": float(it["percent"]),
                "name": m.get("name") or f"Card {cid}",
                "rating": m.get("rating"),
                "position": m.get("position"),
                "league": m.get("league"),
                "nation": m.get("nation"),
                "club": m.get("club"),
                "image": m.get("image_url"),
            }
        )
    return out

async def _attach_prices_ps(items: list[dict]) -> list[dict]:
    async def one(it):
        price = await _get_console_price(int(it["card_id"]), "ps")
        it["prices"] = {"console": price, "pc": None}
        return it
    results = await asyncio.gather(*(one(i) for i in items), return_exceptions=True)
    out = []
    for r in results:
        if isinstance(r, Exception):
            continue
        out.append(r)
    return out

async def _fetch_trending_items(kind: str, tf: str, limit: int) -> list[dict]:
    if kind == "fallers":
        items, _ = await _momentum_page_items(tf, 1)
        items.sort(key=lambda x: x["percent"])
        pick = items[:limit]
    else:
        _, html = await _momentum_page_items(tf, 1)
        last = _parse_last_page_number(html)
        items, _ = await _momentum_page_items(tf, last)
        items.sort(key=lambda x: x["percent"], reverse=True)
        pick = items[:limit]
    return pick

# -------- Route --------

class TrendingOut(BaseModel):
    type: Literal["risers", "fallers", "smart"]
    timeframe: Literal["4h", "6h", "24h"]
    items: List[dict]
    limited: bool = False

@router.get("/trending", response_model=TrendingOut)
async def trending(
    req: Request,
    type_: Literal["risers", "fallers", "smart"] = Query("risers", alias="type"),
    timeframe: Literal["4h", "6h", "24h"] = Query("24h", alias="tf"),
    limit: int = Query(10, ge=1, le=50),
):
    """
    Frontend uses: /api/trending?type=fallers&tf=24
    """
    ent = await compute_entitlements(req)
    limits = ent.get("limits", {}).get("trending", {"timeframes": ["24h"], "limit": 5})
    limited = False

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

    if timeframe not in limits.get("timeframes", ["24h"]):
        timeframe = "24h"
        limited = True

    max_items = int(limits.get("limit", 5))
    if limit > max_items:
        limit = max_items
        limited = True

    # Smart movers (flip 6h vs 24h)
    if type_ == "smart":
        tf6, tf24 = "6", "24"
        f6 = await _fetch_trending_items("fallers", tf6, limit=50)
        r6 = await _fetch_trending_items("risers",  tf6, limit=50)
        f24 = await _fetch_trending_items("fallers", tf24, limit=50)
        r24 = await _fetch_trending_items("risers",  tf24, limit=50)

        f6_ids  = {int(x["card_id"]): float(x["percent"]) for x in f6}
        r6_ids  = {int(x["card_id"]): float(x["percent"]) for x in r6}
        f24_ids = {int(x["card_id"]): float(x["percent"]) for x in f24}
        r24_ids = {int(x["card_id"]): float(x["percent"]) for x in r24}

        smart_ids: set[int] = set()
        smart_map: dict[int, dict] = {}

        for cid, p6 in r6_ids.items():
            p24 = f24_ids.get(cid)
            if p24 is not None:
                smart_ids.add(cid)
                smart_map[cid] = {"chg6hPct": p6, "chg24hPct": p24}

        for cid, p6 in f6_ids.items():
            p24 = r24_ids.get(cid)
            if p24 is not None:
                smart_ids.add(cid)
                smart_map[cid] = {"chg6hPct": p6, "chg24hPct": p24}

        items_raw = [{"card_id": cid, "percent": smart_map[cid]["chg6hPct"]} for cid in smart_ids]
        enriched = await _enrich_with_meta(req, items_raw)
        enriched = await _attach_prices_ps(enriched)
        for e in enriched:
            cid = int(e["card_id"])
            pair = smart_map.get(cid, {})
            e["trend"] = {"chg6hPct": pair.get("chg6hPct"), "chg24hPct": pair.get("chg24hPct")}
        enriched.sort(key=lambda x: abs(x["trend"].get("chg6hPct") or 0), reverse=True)
        return {"type": "smart", "timeframe": timeframe, "items": enriched[:limit], "limited": limited}

    # Simple risers/fallers
    tf_norm = _norm_tf(timeframe)
    raw = await _fetch_trending_items(kind=type_, tf=tf_norm, limit=limit)
    enriched = await _enrich_with_meta(req, raw)
    enriched = await _attach_prices_ps(enriched)
    for e in enriched:
        e["trend"] = {
            "chg24hPct": float(e["percent"]) if tf_norm == "24" else None,
            "chg4hPct": None,
        }

    return {"type": type_, "timeframe": timeframe, "items": enriched[:limit], "limited": limited}
