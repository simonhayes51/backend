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

_CACHE: Dict[Tuple[str, int], Tuple[float, str]] = {}
CACHE_TTL = 120  # seconds

# ---------- ID & percent parsing ----------
# Prefer explicit "26-<id>" segment; otherwise take the last number after /players/
_26_SEGMENT_RE = re.compile(r"/players/[^?#]*/26-(\d+)(?:[/?#]|$)", re.IGNORECASE)
_LAST_NUM_AFTER_PLAYERS_RE = re.compile(r"/players/[^?#]*?(\d+)(?:[/?#]|$)", re.IGNORECASE)
PCT_RE = re.compile(r"([+\-]?\s?\d+(?:\.\d+)?)\s*%")

def _cid_from_href(href: str) -> Optional[int]:
    """Extract the correct FUT card_id from any /players/... href."""
    if "/players/" not in href:
        return None
    m = _26_SEGMENT_RE.search(href)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            pass
    m = _LAST_NUM_AFTER_PLAYERS_RE.search(href)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            pass
    return None

def _name_hint_from_href(href: str) -> Optional[str]:
    """
    From /players/<lead>-<slug>/<maybe 26-id>/ -> 'Nice Name'
    e.g. /players/256853-malik-tillman/26-50588501/ -> 'Malik Tillman'
    """
    try:
        if "/players/" not in href:
            return None
        path = href.split("/players/", 1)[1].strip("/")
        first_seg = path.split("/", 1)[0]
        # drop any leading digits- prefix e.g. "256853-malik-tillman" -> "malik-tillman"
        slug = first_seg.split("-", 1)[1] if "-" in first_seg and first_seg.split("-", 1)[0].isdigit() else first_seg
        words = [w for w in slug.replace("-", " ").split() if w]
        return " ".join(w.capitalize() for w in words) if words else None
    except Exception:
        return None

def _name_from_context(anchor) -> Optional[str]:
    """
    Try to pull a readable player name from nearby markup
    (e.g., <img alt="Name - 85 - Something">).
    """
    try:
        cur = anchor
        for _ in range(6):
            if not cur:
                break
            img = getattr(cur, "find", lambda *a, **k: None)("img", alt=True)
            if img and isinstance(img.get("alt"), str):
                alt = img["alt"].strip()
                # "Karim Adeyemi - 81 - XYZ" -> "Karim Adeyemi"
                name = alt.split(" - ", 1)[0].strip()
                if name and name.lower() != "momentum":
                    return name
            cur = getattr(cur, "parent", None)
    except Exception:
        pass
    return None

# normalize FUT.GG extras like "Rare 84 OVR" appended in slugs
_NAME_SUFFIX_CLEAN_RE = re.compile(
    r"\s+(?:rare|non[- ]?rare|common)(?:\s+\d+\s*ovr)?$",
    re.IGNORECASE,
)
_TRAILING_OVR_RE = re.compile(r"\s+\d+\s*ovr\b.*$", re.IGNORECASE)

def _normalize_name(n: Optional[str]) -> Optional[str]:
    if not n:
        return n
    s = n.strip()
    s = _NAME_SUFFIX_CLEAN_RE.sub("", s)
    s = _TRAILING_OVR_RE.sub("", s)
    return re.sub(r"\s{2,}", " ", s).strip()

# ------------------ Models ------------------
class TrendingOut(BaseModel):
    type: Literal["risers", "fallers", "smart"]
    timeframe: Literal["6h", "12h", "24h"]
    items: List[dict]
    limited: bool = False

# ------------------ Helpers ------------------
def _norm_tf(tf: Optional[str]) -> str:
    """Return '6'|'12'|'24' from inputs like '6', '6h', 'today'."""
    if not tf:
        return "24"
    tf = tf.lower().strip()
    if tf in {"today", "day", "daily", "24hours", "24hr"}:
        return "24"
    if tf.endswith("h"):
        tf = tf[:-1]
    return tf if tf in {"6", "12", "24"} else "24"

def _human_tf(tf_num: str) -> str:
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

def _nearest_percent_text(node) -> Optional[float]:
    """Find a % near the link: walk up a few ancestors, then scan siblings."""
    cur = node
    for _ in range(5):
        if cur is None:
            break
        txt = cur.get_text(" ", strip=True) if hasattr(cur, "get_text") else ""
        m = PCT_RE.search(txt or "")
        if m:
            try:
                return float(m.group(1).replace(" ", ""))
            except Exception:
                pass
        cur = getattr(cur, "parent", None)
    parent = getattr(node, "parent", None)
    if parent:
        for sib in getattr(parent, "children", []):
            try:
                txt = sib.get_text(" ", strip=True)
                m = PCT_RE.search(txt or "")
                if m:
                    return float(m.group(1).replace(" ", ""))
            except Exception:
                continue
    return None

def _extract_items(html: str) -> List[dict]:
    soup = BeautifulSoup(html, "html.parser")
    items: List[dict] = []
    seen_ids: set[int] = set()  # de-dupe by card_id only

    for a in soup.find_all("a", href=True):
        href = a["href"]
        cid = _cid_from_href(href)
        if not cid:
            continue
        if cid in seen_ids:
            continue  # keep only the first occurrence

        pct = _nearest_percent_text(a)
        if pct is None:
            continue

        # name priority: nearby <img alt="Name - ..."> > slug from href > "Card {cid}"
        name_hint_img = _normalize_name(_name_from_context(a))
        name_hint_slug = _normalize_name(_name_hint_from_href(href))
        name_hint = name_hint_img or (
            name_hint_slug if (name_hint_slug and name_hint_slug.lower() != "momentum") else None
        ) or f"Card {cid}"

        items.append({
            "card_id": cid,
            "percent": pct,
            "name_hint": name_hint,
        })
        seen_ids.add(cid)

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
    key = "ps" if platform == "ps" else "xbox"
    try:
        prices = (data or {}).get("prices", {}).get(key, {}) or {}
        val = prices.get("price") or prices.get("lowestBin") or prices.get("LCPrice")
        return int(val) if isinstance(val, (int, float)) and val > 0 else None
    except Exception:
        return None

async def _enrich_meta(req: Request, rows: List[dict]) -> List[dict]:
    if not rows:
        return []
    ids = [int(x["card_id"]) for x in rows]  # bigint lookup
    try:
        async with req.app.state.player_pool.acquire() as conn:
            dbrows = await conn.fetch(
                """
                SELECT card_id, name, rating, position, league, nation, club, image_url
                FROM fut_players
                WHERE card_id = ANY($1::bigint[])
                """,
                ids,
            )
    except Exception:
        dbrows = []
    meta = {int(r["card_id"]): dict(r) for r in dbrows}
    out: List[dict] = []
    for r in rows:
        cid = int(r["card_id"])
        m = meta.get(cid, {})
        name = m.get("name") or r.get("name_hint") or f"Card {cid}"
        out.append({
            "card_id": cid,
            "id": str(cid),
            "name": name,
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
    return [r for r in results if not isinstance(r, Exception)]

async def _fetch_trending(kind: Literal["risers", "fallers"], tf: str, limit: int) -> List[dict]:
    """
    Strategy:
    - Fallers: first page (lowest % first); if not enough, also sample last page and merge.
    - Risers : last page (highest % first); if not enough, also sample first page and merge.
    """
    first_html = await _momentum_page(tf, 1)
    last_page = _parse_last_page_num(first_html)

    if kind == "fallers":
        base = await _page_items(tf, 1)
        base.sort(key=lambda x: x["percent"])
        out = base[:limit]
        if len(out) < limit and last_page > 1:
            tail = await _page_items(tf, last_page)
            tail.sort(key=lambda x: x["percent"])
            out = (base + tail)[:limit]
    else:
        base = await _page_items(tf, last_page)
        base.sort(key=lambda x: x["percent"], reverse=True)
        out = base[:limit]
        if len(out) < limit and last_page > 1:
            head = await _page_items(tf, 1)
            head.sort(key=lambda x: x["percent"], reverse=True)
            out = (base + head)[:limit]
    return out

# ------------------ Route ------------------
class _TrendingOut(BaseModel):
    type: Literal["risers", "fallers", "smart"]
    timeframe: Literal["6h", "12h", "24h"]
    items: List[dict]
    limited: bool = False

@router.get("/trending", response_model=_TrendingOut)
async def trending(
    req: Request,
    type_: Literal["risers", "fallers", "smart"] = Query("risers", alias="type"),
    tf_raw: str = Query("24h", alias="tf"),
    limit: int = Query(10, ge=1, le=50),
    debug: bool = Query(False),
):
    """
    Example: /api/trending?type=fallers&tf=24  (or tf=24h)
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

    tf_num = _norm_tf(tf_raw)
    tf_human = _human_tf(tf_num)

    if tf_human not in limits.get("timeframes", ["24h", "12h", "6h"]):
        tf_num = "24"
        tf_human = "24h"
        limited = True

    max_items = int(limits.get("limit", 5))
    if limit > max_items:
        limit = max_items
        limited = True

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

        # Up on 6h, down on 24h
        for cid, p6 in r6m.items():
            if cid in f24m:
                smart_ids.add(cid)
                smart_map[cid] = {"chg6hPct": p6, "chg24hPct": f24m[cid]}
        # Down on 6h, up on 24h
        for cid, p6 in f6m.items():
            if cid in r24m:
                smart_ids.add(cid)
                smart_map[cid] = {"chg6hPct": p6, "chg24hPct": r24m[cid]}

        raw = [{"card_id": cid, "percent": smart_map[cid]["chg6hPct"], "name_hint": None} for cid in smart_ids]
        enriched = await _enrich_meta(req, raw)
        enriched = await _attach_prices(enriched)
        for e in enriched:
            cid = int(e["card_id"])
            pair = smart_map.get(cid, {})
            e["trend"] = {"chg6hPct": pair.get("chg6hPct"), "chg24hPct": pair.get("chg24hPct")}
        enriched.sort(key=lambda x: abs(x["trend"].get("chg6hPct") or 0), reverse=True)
        if debug:
            for e in enriched:
                e["__debug"] = {"smart_map": smart_map.get(int(e["card_id"]))}
        return {"type": "smart", "timeframe": tf_human, "items": enriched[:limit], "limited": limited}

    # Simple risers/fallers
    raw = await _fetch_trending(kind=type_, tf=tf_num, limit=limit)
    enriched = await _enrich_meta(req, raw)
    enriched = await _attach_prices(enriched)
    for e in enriched:
        if tf_num == "24":
            e["trend"] = {"chg24hPct": float(e["percent"])}
        elif tf_num == "12":
            e["trend"] = {"chg12hPct": float(e["percent"])}
        else:
            e["trend"] = {"chg6hPct": float(e["percent"])}
    if debug:
        for e in enriched:
            e["__debug"] = {"percent": e.get("percent")}

    return {"type": type_, "timeframe": tf_human, "items": enriched[:limit], "limited": limited}
