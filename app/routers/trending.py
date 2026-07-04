# app/routers/trending.py
from __future__ import annotations

import re
import time
from typing import Dict, List, Literal, Optional, Tuple

import aiohttp
from bs4 import BeautifulSoup
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

router = APIRouter(prefix="/api", tags=["trending"])

# ------------------ Config ------------------
# futbin's own "Market Movers" widget (https://www.futbin.com/26/market)
# already computes Top Gainers/Top Losers for 4h and 24h windows, split by
# platform (a "platform-ps-only" block covering the shared PS/Xbox market,
# and a "platform-pc-only" block for PC), with each card's % change and
# current price inline - confirmed against a real dump of that page. This
# replaces the old fut.gg momentum-page scrape (dead now that fut.gg blocks
# scrapers) and needs no per-card price lookups, unlike the old
# implementation, since one page fetch covers every timeframe/direction/
# platform combination this endpoint serves.
MARKET_URL = "https://www.futbin.com/26/market"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SBCSolver/1.5)"}
REQUEST_TIMEOUT = 20

# The page is the full market dashboard (a few MB), not a lean widget
# endpoint, so it's cached rather than fetched per request - movers
# percentages don't need second-by-second freshness anyway.
CACHE_TTL = 600  # seconds
_cache: Dict[str, Tuple[float, str]] = {}

HREF_RE = re.compile(r"/player/(\d+)/([^/\"'?]+)")


def _plat(p: str) -> str:
    p = (p or "").lower()
    if p in ("pc", "origin", "windows"):
        return "pc"
    return "ps"  # ps, xbox, console (and anything else) share the ps/xbox market


def _num(txt: str) -> int:
    if not txt:
        return 0
    t = txt.lower().replace(",", "").strip()
    if t.endswith("m"):
        try:
            return int(float(t[:-1]) * 1_000_000)
        except Exception:
            return 0
    if t.endswith("k"):
        try:
            return int(float(t[:-1]) * 1_000)
        except Exception:
            return 0
    m = re.search(r"\d+(\.\d+)?", t)
    return int(float(m.group(0))) if m else 0


def _norm_tf(tf: Optional[str]) -> str:
    """futbin's widget only offers 4h/24h windows - anything else collapses
    to the nearest of those two so older '6h'/'12h' callers keep working."""
    tf = (tf or "24").lower().strip().rstrip("h")
    return "4" if tf in {"1", "2", "3", "4", "5", "6"} else "24"


async def _fetch_market_html() -> str:
    now = time.time()
    hit = _cache.get("html")
    if hit and (now - hit[0] < CACHE_TTL):
        return hit[1]

    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(MARKET_URL, headers=HEADERS) as r:
            if r.status != 200:
                raise HTTPException(status_code=502, detail=f"futbin market page returned {r.status}")
            html = await r.text()

    _cache["html"] = (now, html)
    return html


def _parse_card(a) -> Optional[dict]:
    href = a.get("href") or ""
    m = HREF_RE.search(href)
    if not m:
        return None
    futbin_id, slug = m.group(1), m.group(2)

    name_el = a.find("div", class_="playercard-s-26-name")
    name = name_el.get_text(strip=True) if name_el else None
    if not name:
        return None

    rating_el = a.find("div", class_="playercard-s-26-rating")
    rating_txt = rating_el.get_text(strip=True) if rating_el else ""
    rating = int(rating_txt) if rating_txt.isdigit() else None

    pos_el = a.find("div", class_="playercard-s-26-position")
    position = pos_el.get_text(strip=True) if pos_el else None

    change_el = a.find("div", class_="day-change-percentage")
    if not change_el:
        return None
    try:
        percent = float(change_el.get_text(strip=True).replace("%", "").strip())
    except ValueError:
        return None
    if "day-change-negative" in (change_el.get("class") or []):
        percent = -abs(percent)

    price_el = a.find("div", class_="platform-price-wrapper-small")
    price = _num(price_el.get_text(strip=True)) if price_el else None

    return {
        "player_url": f"https://www.futbin.com/26/player/{futbin_id}/{slug}",
        "name": name,
        "rating": rating,
        "position": position,
        "percent": percent,
        "price": price,
    }


def _parse_movers(html: str, tf: str, platform: str) -> Tuple[List[dict], List[dict]]:
    """Returns (gainers, losers) for the given tf ('4'|'24') and platform ('ps'|'pc')."""
    soup = BeautifulSoup(html, "html.parser")
    wrapper = soup.find("div", class_=f"market-{tf}-hours")
    if not wrapper:
        return [], []

    plat_class = "platform-pc-only" if platform == "pc" else "platform-ps-only"
    gain_col = wrapper.find("div", class_="market-gain")
    loss_col = wrapper.find("div", class_="market-losers")

    def _rows(col) -> List[dict]:
        if not col:
            return []
        platform_row = col.find("div", class_=plat_class)
        if not platform_row:
            return []
        out = []
        for a in platform_row.find_all("a", class_="market-player-card"):
            item = _parse_card(a)
            if item:
                out.append(item)
        return out

    return _rows(gain_col), _rows(loss_col)


# ------------------ DB enrichment ------------------
async def _enrich(req: Request, rows: List[dict], platform: str) -> List[dict]:
    """Maps futbin's player_url to our own card_id/name/image via fut_players.
    Rows for cards we haven't crawled yet are dropped rather than shown as
    dead watchlist entries."""
    if not rows:
        return []

    urls = [r["player_url"] for r in rows]
    meta_by_url: Dict[str, dict] = {}
    pool = getattr(req.app.state, "player_pool", None) or getattr(req.app.state, "pool", None)
    if pool:
        try:
            async with pool.acquire() as conn:
                db_rows = await conn.fetch(
                    """
                    SELECT card_id, player_url, name, rating, position, club, nation, league, image_url
                    FROM fut_players
                    WHERE player_url = ANY($1::text[])
                    """,
                    urls,
                )
            meta_by_url = {r["player_url"]: dict(r) for r in db_rows}
        except Exception:
            meta_by_url = {}

    out: List[dict] = []
    seen: set[int] = set()
    for r in rows:
        m = meta_by_url.get(r["player_url"])
        if not m:
            continue
        cid = int(m["card_id"])
        if cid in seen:
            continue
        seen.add(cid)
        out.append(
            {
                "card_id": cid,
                "pid": cid,
                "id": str(cid),
                "name": m.get("name") or r["name"],
                "rating": m.get("rating") or r["rating"],
                "position": m.get("position") or r["position"],
                "club": m.get("club"),
                "nation": m.get("nation"),
                "league": m.get("league"),
                "image": m.get("image_url"),
                "percent": r["percent"],
                "platform": platform,
                "price_console": r["price"],
                "prices": {"console": r["price"], "pc": None},
            }
        )
    return out


# ------------------ Smart movers (4h vs 24h divergence) ------------------
async def _percent_map(req: Request, tf: str, platform: str) -> Dict[int, dict]:
    html = await _fetch_market_html()
    gainers, losers = _parse_movers(html, tf, platform)
    enriched = await _enrich(req, gainers + losers, platform)
    return {int(e["card_id"]): e for e in enriched}


async def _build_smart(req: Request, platform: str, limit: int) -> List[dict]:
    short_map = await _percent_map(req, "4", platform)
    long_map = await _percent_map(req, "24", platform)

    out: List[dict] = []
    for cid, short in short_map.items():
        long = long_map.get(cid)
        if not long:
            continue
        # Only interesting if the short-term move disagrees with the longer trend.
        if (short["percent"] > 0) == (long["percent"] > 0):
            continue
        item = dict(short)
        item["trend"] = {"chg4hPct": short["percent"], "chg24hPct": long["percent"]}
        item["percent_4h"] = short["percent"]
        item["percent_24h"] = long["percent"]
        out.append(item)

    out.sort(key=lambda x: abs(x["percent_4h"]), reverse=True)
    return out[:limit]


# ------------------ Response model ------------------
class TrendingOut(BaseModel):
    type: Literal["risers", "fallers", "smart"]
    timeframe: Literal["4h", "24h"]
    items: List[dict]
    limited: bool = False  # compatibility


# ------------------ Route ------------------
@router.get("/trending", response_model=TrendingOut)
async def trending(
    req: Request,
    type_: Literal["risers", "fallers", "smart"] = Query("risers", alias="type"),
    tf_raw: str = Query("24h", alias="tf"),
    limit: int = Query(10, ge=1, le=50),
    platform: str = Query("ps", description="ps|xbox|pc|console"),
):
    tf = _norm_tf(tf_raw)
    plat = _plat(platform)

    if type_ == "smart":
        items = await _build_smart(req, plat, limit)
        return {"type": "smart", "timeframe": f"{tf}h", "items": items, "limited": False}

    html = await _fetch_market_html()
    gainers, losers = _parse_movers(html, tf, plat)
    raw = gainers if type_ == "risers" else losers
    enriched = await _enrich(req, raw, plat)

    return {"type": type_, "timeframe": f"{tf}h", "items": enriched[:limit], "limited": False}
