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

# ---------- parsing ----------
# ONLY accept the real FUT item id segment: /26-<card_id>/
_26_SEGMENT_RE = re.compile(r"/players/[^?#]*/26-(\d+)(?:[/?#]|$)", re.IGNORECASE)
PCT_RE = re.compile(r"([+\-]?\s?\d+(?:\.\d+)?)\s*%")

def _cid_from_href(href: str) -> Optional[int]:
    """
    Extract ONLY the FUT item card_id from FUT.GG player urls.
    We intentionally DO NOT fall back to "last number after /players/" because that
    often returns the base player id (causes dupes + N/A price).
    """
    if "/players/" not in (href or ""):
        return None
    m = _26_SEGMENT_RE.search(href)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None

def _name_hint_from_href(href: str) -> Optional[str]:
    """
    From /players/<lead>-<slug>/26-<id>/ -> 'Nice Name'
    e.g. /players/256853-malik-tillman/26-50588501/ -> 'Malik Tillman'
    """
    try:
        if "/players/" not in (href or ""):
            return None
        path = href.split("/players/", 1)[1].strip("/")
        first_seg = path.split("/", 1)[0]
        # drop leading digits- prefix
        slug = (
            first_seg.split("-", 1)[1]
            if "-" in first_seg and first_seg.split("-", 1)[0].isdigit()
            else first_seg
        )
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

# ------------------ Response model ------------------
class TrendingOut(BaseModel):
    type: Literal["risers", "fallers", "smart"]
    timeframe: Literal["6h", "12h", "24h"]
    items: List[dict]
    limited: bool = False  # compatibility

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

def _dedupe_final(items: List[dict]) -> List[dict]:
    """Final safety-net dedupe by card_id."""
    seen: set[int] = set()
    out: List[dict] = []
    for it in items:
        try:
            cid = int(it.get("card_id") or it.get("pid") or 0)
        except Exception:
            continue
        if not cid or cid in seen:
            continue
        seen.add(cid)
        out.append(it)
    return out

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
    """
    Find a % near the link: walk up a few ancestors, then scan siblings.
    This works even when the % isn't inside the <a> itself.
    """
    cur = node
    for _ in range(6):
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
    """
    Extract from ONLY real player card links (ones that contain /26-<card_id>/).
    Avoids picking up base player IDs, nav links, etc.
    """
    soup = BeautifulSoup(html, "html.parser")
    items: List[dict] = []
    seen_ids: set[int] = set()

    for a in soup.find_all("a", href=True):
        href = a.get("href") or ""
        if "26-" not in href:
            continue

        cid = _cid_from_href(href)
        if not cid or cid in seen_ids:
            continue

        pct = _nearest_percent_text(a)
        if pct is None:
            continue

        name_hint_img = _normalize_name(_name_from_context(a))
        name_hint_slug = _normalize_name(_name_hint_from_href(href))
        name_hint = name_hint_img or (
            name_hint_slug if (name_hint_slug and name_hint_slug.lower() != "momentum") else None
        ) or f"Card {cid}"

        items.append({"card_id": cid, "pid": cid, "percent": pct, "name_hint": name_hint})
        seen_ids.add(cid)

    return items

async def _page_items(tf: str, page: int) -> List[dict]:
    html = await _momentum_page(tf, page)
    return _extract_items(html)

# ------------------ Prices ------------------
async def _get_console_price(card_id: int, platform: str = "ps") -> Optional[int]:
    """
    FUT.GG shape:
      { "data": { "currentPrice": { "platform": "ps5", "price": 15000, ... } } }
    """
    url = FUTGG_PRICE_URL.format(card_id=card_id)
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(url, headers={**REQ_HEADERS, "Accept": "application/json"}) as r:
                if r.status != 200:
                    return None
                payload = await r.json()
    except Exception:
        return None

    cp = ((payload or {}).get("data") or {}).get("currentPrice") or {}
    try:
        price = int(cp.get("price"))
        return price if price > 0 else None
    except Exception:
        return None

async def _enrich_meta(req: Request, rows: List[dict]) -> List[dict]:
    """Optional DB enrichment. If fut_players isn't available, still return usable payload."""
    if not rows:
        return []

    ids = [int(x["card_id"]) for x in rows]
    meta: Dict[int, dict] = {}

    try:
        pool = getattr(req.app.state, "player_pool", None) or getattr(req.app.state, "pool", None)
        if pool:
            async with pool.acquire() as conn:
                dbrows = await conn.fetch(
                    """
                    SELECT card_id, name, rating, position, league, nation, club, image_url
                    FROM fut_players
                    WHERE card_id = ANY($1::bigint[])
                    """,
                    ids,
                )
            meta = {int(r["card_id"]): dict(r) for r in dbrows}
    except Exception:
        meta = {}

    out: List[dict] = []
    for r in rows:
        cid = int(r["card_id"])
        m = meta.get(cid, {})
        name = m.get("name") or r.get("name_hint") or f"Card {cid}"
        out.append(
            {
                "card_id": cid,
                "pid": cid,
                "id": str(cid),
                "name": name,
                "rating": m.get("rating"),
                "position": m.get("position"),
                "league": m.get("league"),
                "nation": m.get("nation"),
                "club": m.get("club"),
                "image": m.get("image_url"),
                "percent": float(r["percent"]),
            }
        )
    return out

async def _attach_prices(items: List[dict], platform: str = "ps") -> List[dict]:
    """Adds platform + price_console + nested prices.console."""
    async def one(it: dict) -> dict:
        p = await _get_console_price(int(it["card_id"]), platform)
        it["platform"] = platform
        it["price_console"] = p
        it["prices"] = {"console": p, "pc": None}
        return it

    results = await asyncio.gather(*(one(i) for i in items), return_exceptions=True)
    return [r for r in results if not isinstance(r, Exception)]

# ------------------ Page selection logic ------------------
async def _fetch_trending(kind: Literal["risers", "fallers"], tf: str, limit: int) -> List[dict]:
    """
    Fallers: take from page 1
    Risers : walk backwards from last page until we have {limit}
    This fixes cases where the last page only has 1 usable tile.
    """
    first_html = await _momentum_page(tf, 1)
    last_page = _parse_last_page_num(first_html)

    if kind == "fallers":
        rows = await _page_items(tf, 1)
        rows = _dedupe_final(rows)
        return rows[:limit]

    # RISERS: walk backwards
    collected: List[dict] = []
    seen: set[int] = set()

    page = last_page
    while page >= 1 and len(collected) < limit:
        rows = await _page_items(tf, page)
        for r in rows:
            try:
                cid = int(r.get("card_id") or 0)
            except Exception:
                continue
            if not cid or cid in seen:
                continue
            seen.add(cid)
            collected.append(r)
            if len(collected) >= limit:
                break
        page -= 1

    return collected[:limit]

# ------------------ Route ------------------
@router.get("/trending", response_model=TrendingOut)
async def trending(
    req: Request,
    type_: Literal["risers", "fallers", "smart"] = Query("risers", alias="type"),
    tf_raw: str = Query("24h", alias="tf"),
    limit: int = Query(10, ge=1, le=50),
    debug: bool = Query(False),
):
    tf_num = _norm_tf(tf_raw)
    tf_human = _human_tf(tf_num)

    # ---- SMART ----
    if type_ == "smart":
        f6  = await _fetch_trending("fallers", "6",  limit=50)
        r6  = await _fetch_trending("risers",  "6",  limit=50)
        f24 = await _fetch_trending("fallers", "24", limit=50)
        r24 = await _fetch_trending("risers",  "24", limit=50)

        f6m  = {int(x["card_id"]): float(x["percent"]) for x in f6}
        r6m  = {int(x["card_id"]): float(x["percent"]) for x in r6}
        f24m = {int(x["card_id"]): float(x["percent"]) for x in f24}
        r24m = {int(x["card_id"]): float(x["percent"]) for x in r24}

        smart_map: Dict[int, Dict[str, float]] = {}

        # Up on 6h, down on 24h
        for cid, p6 in r6m.items():
            if cid in f24m:
                smart_map[cid] = {"chg6hPct": p6, "chg24hPct": f24m[cid]}
        # Down on 6h, up on 24h
        for cid, p6 in f6m.items():
            if cid in r24m:
                smart_map[cid] = {"chg6hPct": p6, "chg24hPct": r24m[cid]}

        raw = [{"card_id": cid, "pid": cid, "percent": smart_map[cid]["chg6hPct"], "name_hint": None} for cid in smart_map.keys()]

        enriched = await _enrich_meta(req, raw)
        enriched = await _attach_prices(enriched, platform="ps")
        enriched = _dedupe_final(enriched)

        for e in enriched:
            cid = int(e["card_id"])
            pair = smart_map.get(cid, {})
            e["trend"] = {"chg6hPct": pair.get("chg6hPct"), "chg24hPct": pair.get("chg24hPct")}
            e["percent_6h"] = pair.get("chg6hPct")
            e["percent_24h"] = pair.get("chg24hPct")

        enriched.sort(key=lambda x: abs(x.get("percent_6h") or 0), reverse=True)

        if debug:
            for e in enriched:
                e["__debug"] = {"smart_map": smart_map.get(int(e["card_id"]))}

        return {"type": "smart", "timeframe": tf_human, "items": enriched[:limit], "limited": False}

    # ---- RISERS / FALLERS ----
    raw = await _fetch_trending(kind=type_, tf=tf_num, limit=limit)
    enriched = await _enrich_meta(req, raw)
    enriched = await _attach_prices(enriched, platform="ps")
    enriched = _dedupe_final(enriched)

    if debug:
        for e in enriched:
            e["__debug"] = {"percent": e.get("percent")}

    return {"type": type_, "timeframe": tf_human, "items": enriched[:limit], "limited": False}