# app/routers/trending.py
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Literal, List, Optional, Dict, Tuple

import aiohttp
from bs4 import BeautifulSoup, Tag
from fastapi import APIRouter, Request, HTTPException, Query
from pydantic import BaseModel

from app.auth.entitlements import compute_entitlements

router = APIRouter(prefix="/api", tags=["trending"])

MOMENTUM_BASE = "https://www.fut.gg/players/momentum"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": "https://www.fut.gg/",
}
FUTGG_PRICE_URL = "https://www.fut.gg/api/fut/player-prices/26/{card_id}"

CACHE_TTL = 120
_CACHE: Dict[str, Tuple[float, str]] = {}

CARD_ID_RE = re.compile(r"/players/(?:\d{2}-)?(?P<id>\d+)(?:[/?#]|$)", re.IGNORECASE)
PCT_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*%")

class TrendingOut(BaseModel):
    type: Literal["risers", "fallers"]
    timeframe: Literal["24h"]
    items: List[dict]
    limited: bool = False

# ------------- fetch helpers ----------------
async def _fetch(url: str, timeout_total: int = 12) -> Optional[str]:
    key = f"GET:{url}"
    now = time.time()
    hit = _CACHE.get(key)
    if hit and (now - hit[0] < CACHE_TTL):
        return hit[1]
    try:
        timeout = aiohttp.ClientTimeout(total=timeout_total)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(url, headers=HEADERS) as r:
                html = await r.text()
                if r.status != 200:
                    logging.warning("Trending: %s -> %s", url, r.status)
                _CACHE[key] = (now, html)
                return html
    except Exception as e:
        logging.warning("Trending fetch failed %s: %s", url, e)
        return None

def _tile_percent(container: Tag) -> Optional[float]:
    if not container:
        return None
    txt = container.get_text(" ", strip=True) or ""
    m = PCT_RE.search(txt)
    if m:
        try:
            return float(m.group(1))
        except Exception:
            return None
    # light attribute fallback
    for el in container.find_all(True):
        for attr in ("data-change", "title", "aria-label"):
            val = el.get(attr)
            if not val:
                continue
            m = PCT_RE.search(val)
            if m:
                try:
                    return float(m.group(1))
                except Exception:
                    continue
    return None

def _closest_container(a: Tag) -> Tag:
    cur = a
    for _ in range(6):
        if not isinstance(cur, Tag):
            break
        classes = " ".join(cur.get("class", []))
        if cur.name in {"div", "li", "article"} and (cur.find("img") or "player" in classes or "card" in classes):
            return cur
        cur = cur.parent  # type: ignore
    return a

def _extract_ids_with_pct(html: str) -> List[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out: List[dict] = []
    seen: set[int] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/players/" not in href:
            continue
        m = CARD_ID_RE.search(href)
        if not m:
            continue
        try:
            cid = int(m.group("id"))
        except Exception:
            continue
        if cid in seen:
            continue
        cont = _closest_container(a)
        pct = _tile_percent(cont)
        item = {"card_id": cid}
        if pct is not None:
            item["percent"] = pct
        out.append(item)
        seen.add(cid)
    return out

def _parse_last_page(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    last = 1
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "page=" in href:
            try:
                n = int(href.split("page=", 1)[1].split("&", 1)[0])
                last = max(last, n)
            except Exception:
                pass
        else:
            t = (a.text or "").strip()
            if t.isdigit():
                last = max(last, int(t))
    return last

# ------------- DB + price ----------------
async def _enrich_meta(req: Request, items: List[dict]) -> List[dict]:
    if not items:
        return []
    ids = [int(x["card_id"]) for x in items]
    meta: Dict[int, dict] = {}
    try:
        async with req.app.state.player_pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT card_id, name, rating, position, league, nation, club, image_url
                   FROM fut_players WHERE card_id = ANY($1::bigint[])""",
                ids,
            )
        meta = {int(r["card_id"]): dict(r) for r in rows}
    except Exception as e:
        logging.warning("Trending: meta fetch failed: %s", e)

    out: List[dict] = []
    for it in items:
        cid = int(it["card_id"])
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
            **({"percent": it["percent"]} if "percent" in it else {}),
        })
    return out

async def _get_console_price(card_id: int) -> Optional[int]:
    url = FUTGG_PRICE_URL.format(card_id=card_id)
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(url, headers=HEADERS) as r:
                if r.status != 200:
                    return None
                data = await r.json()
    except Exception:
        return None
    def pick(d: dict) -> Optional[int]:
        if not isinstance(d, dict):
            return None
        for k in ("price", "lowestBin", "LCPrice"):
            v = d.get(k)
            if isinstance(v, (int, float)) and v > 0:
                return int(v)
        return None
    prices = (data or {}).get("prices", {})
    return pick(prices.get("ps")) or pick(prices.get("xbox"))

async def _attach_prices(items: List[dict]) -> List[dict]:
    sem = asyncio.Semaphore(16)
    async def one(it: dict) -> dict:
        async with sem:
            it.setdefault("prices", {})
            it["prices"]["console"] = await _get_console_price(int(it["card_id"]))
            it["prices"]["pc"] = None
            return it
    return list(await asyncio.gather(*(one(i) for i in items)))

# ------------- core fetch ----------------
async def _top10_from_page1() -> List[dict]:
    html = await _fetch(MOMENTUM_BASE)
    if not html:
        return []
    items = _extract_ids_with_pct(html)
    return items[:10]

async def _last10_from_last_page() -> List[dict]:
    first = await _fetch(MOMENTUM_BASE)
    if not first:
        return []
    last_n = _parse_last_page(first)
    last_html = await _fetch(f"{MOMENTUM_BASE}/?page={last_n}")
    if not last_html:
        return []
    items = _extract_ids_with_pct(last_html)
    # take the LAST 10 from that page, preserving order
    return items[-10:] if len(items) >= 10 else items

# ------------- route ----------------
@router.get("/trending", response_model=TrendingOut)
async def trending(
    req: Request,
    type_: Literal["risers", "fallers"] = Query("fallers", alias="type"),
    limit: int = Query(10, ge=1, le=10),
):
    """
    EXACT spec:
    - fallers: first 10 from page 1 of /players/momentum/
    - risers : last 10 from the final page of /players/momentum/?page=N
    """
    # entitlements still honoured (limit/timeframe is fixed to 24h now)
    ent = await compute_entitlements(req)
    if not ent:
        pass  # keep lightweight; add gating if you want

    raw: List[dict] = await (_top10_from_page1() if type_ == "fallers" else _last10_from_last_page())
    enriched = await _enrich_meta(req, raw)
    enriched = await _attach_prices(enriched)

    # Add trend key if percent present
    for e in enriched:
        if "percent" in e and e["percent"] is not None:
            e["trend"] = {"chg24hPct": float(e["percent"])}
        else:
            e["trend"] = {"chg24hPct": None}

    return {"type": type_, "timeframe": "24h", "items": enriched[:limit], "limited": False}
