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

# ---------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------
MOMENTUM_BASE = "https://www.fut.gg/players/momentum"
REQ_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": "https://www.fut.gg/",
}
FUTGG_PRICE_URL = "https://www.fut.gg/api/fut/player-prices/26/{card_id}"

_CACHE: Dict[Tuple[str, int], Tuple[float, str]] = {}
CACHE_TTL = 120  # seconds

# ---------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------
_26_SEGMENT_RE = re.compile(r"/26-(\d+)")
PCT_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*%")

# ---------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------
class TrendingOut(BaseModel):
    type: Literal["risers", "fallers"]
    timeframe: Literal["6h", "12h", "24h"]
    items: List[dict]
    limited: bool = False

# ---------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------
def _norm_tf(tf: Optional[str]) -> str:
    if not tf:
        return "24"
    tf = tf.lower().replace("h", "")
    return tf if tf in {"6", "12", "24"} else "24"

def _human_tf(tf: str) -> str:
    return f"{tf}h"

def _dedupe_by_card_id(items: List[dict]) -> List[dict]:
    seen = set()
    out = []
    for it in items:
        cid = int(it["card_id"])
        if cid in seen:
            continue
        seen.add(cid)
        out.append(it)
    return out

# ---------------------------------------------------------------------
# Scraping helpers
# ---------------------------------------------------------------------
async def _fetch_html(url: str) -> str:
    timeout = aiohttp.ClientTimeout(total=12)
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        async with sess.get(url, headers=REQ_HEADERS) as r:
            if r.status != 200:
                raise HTTPException(502, f"Upstream {r.status}")
            return await r.text()

async def _momentum_page(tf: str, page: int) -> str:
    now = time.time()
    key = (tf, page)
    if key in _CACHE and now - _CACHE[key][0] < CACHE_TTL:
        return _CACHE[key][1]

    html = await _fetch_html(f"{MOMENTUM_BASE}/{tf}/?page={page}")
    _CACHE[key] = (now, html)
    return html

def _parse_last_page(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    last = 1
    for a in soup.find_all("a", href=True):
        if "page=" in a["href"]:
            try:
                last = max(last, int(a["href"].split("page=")[1].split("&")[0]))
            except Exception:
                pass
    return last

def _extract_items(html: str) -> List[dict]:
    soup = BeautifulSoup(html, "html.parser")
    items = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = _26_SEGMENT_RE.search(href)
        if not m:
            continue

        cid = int(m.group(1))
        txt = a.get_text(" ", strip=True)
        pct_match = PCT_RE.search(txt)
        if not pct_match:
            continue

        pct = float(pct_match.group(1))
        items.append({
            "card_id": cid,
            "percent": pct,
        })

    return _dedupe_by_card_id(items)

async def _page_items(tf: str, page: int) -> List[dict]:
    return _extract_items(await _momentum_page(tf, page))

# ---------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------
async def _get_console_price(card_id: int) -> Optional[int]:
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(
                FUTGG_PRICE_URL.format(card_id=card_id),
                headers=REQ_HEADERS,
            ) as r:
                if r.status != 200:
                    return None
                data = await r.json()
        prices = data.get("prices", {}).get("ps", {})
        return int(prices.get("price") or prices.get("lowestBin") or 0) or None
    except Exception:
        return None

async def _attach_prices(items: List[dict]) -> List[dict]:
    async def enrich(it):
        it["platform"] = "ps"
        it["price_console"] = await _get_console_price(int(it["card_id"]))
        return it

    results = await asyncio.gather(*(enrich(i) for i in items))
    return results

# ---------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------
async def _fetch_trending(kind: Literal["risers", "fallers"], tf: str, limit: int) -> List[dict]:
    first_html = await _momentum_page(tf, 1)
    last_page = _parse_last_page(first_html)

    if kind == "fallers":
        return (await _page_items(tf, 1))[:limit]

    return (await _page_items(tf, last_page))[:limit]

# ---------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------
@router.get("/trending", response_model=TrendingOut)
async def trending(
    req: Request,
    type_: Literal["risers", "fallers"] = Query("fallers", alias="type"),
    tf_raw: str = Query("24h", alias="tf"),
    limit: int = Query(10, ge=1, le=20),
):
    ent = await compute_entitlements(req)
    limits = ent.get("limits", {}).get("trending", {"limit": 10})

    if limit > limits.get("limit", 10):
        limit = limits["limit"]

    tf = _norm_tf(tf_raw)
    tf_human = _human_tf(tf)

    raw = await _fetch_trending(type_, tf, limit)
    enriched = await _attach_prices(raw)

    return {
        "type": type_,
        "timeframe": tf_human,
        "items": enriched,
        "limited": False,
    }