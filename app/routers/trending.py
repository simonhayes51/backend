# app/routers/trending.py
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Literal, List, Optional, Dict, Tuple, Any

import aiohttp
from bs4 import BeautifulSoup, Tag
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

CACHE_TTL = 120  # seconds
_CACHE: Dict[Tuple[str, int], Tuple[float, str]] = {}

CARD_ID_RE = re.compile(r"/players/(?:\d{2}-)?(?P<id>\d+)(?:[/?#]|$)", re.IGNORECASE)
PCT_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*%")

# ------------------ Models ------------------
class TrendingOut(BaseModel):
    type: Literal["risers", "fallers", "smart"]
    timeframe: Literal["4h", "6h", "12h", "24h"]
    items: List[dict]
    limited: bool = False

# ------------------ Helpers ------------------
def _norm_tf(tf: Optional[str]) -> str:
    """Map alias timeframes to '4'|'6'|'12'|'24'."""
    if not tf:
        return "24"
    tf = tf.strip().lower()
    if tf in {"today", "day", "daily", "24hours", "24hr"}:
        return "24"
    if tf.endswith("h"):
        tf = tf[:-1]
    return tf if tf in {"4", "6", "12", "24"} else "24"

def _human_tf(tf_num: str) -> str:
    return f"{tf_num}h"

async def _fetch_html(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    try:
        async with session.get(url, headers=REQ_HEADERS) as r:
            if r.status != 200:
                logging.warning("Trending: upstream %s returned %s", url, r.status)
                return None
            return await r.text()
    except Exception as e:
        logging.warning("Trending: fetch failed %s: %s", url, e)
        return None

async def _momentum_page(tf: str, page: int) -> Optional[str]:
    now = time.time()
    key = (tf, page)
    hit = _CACHE.get(key)
    if hit and (now - hit[0] < CACHE_TTL):
        return hit[1]

    url = f"{MOMENTUM_BASE}/{tf}/?page={page}"
    timeout = aiohttp.ClientTimeout(total=12)
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        html = await _fetch_html(sess, url)

    if html is not None:
        _CACHE[key] = (now, html)
    return html

def _parse_last_page_num(html: str) -> int:
    if not html:
        return 1
    try:
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
        for a in soup.find_all("a"):
            t = (a.text or "").strip()
            if t.isdigit():
                last = max(last, int(t))
        return last
    except Exception as e:
        logging.warning("Trending: last-page parse error: %s", e)
        return 1

def _closest_card_container(node: Tag) -> Optional[Tag]:
    cur = node
    for _ in range(6):
        if cur is None or not isinstance(cur, Tag):
            break
        classes = " ".join(cur.get("class", []))
        if cur.name in {"article", "li", "div"} and (
            "card" in classes or "player" in classes or "tile" in classes or cur.find("img")
        ):
            return cur
        cur = cur.parent
    return node.parent if isinstance(node.parent, Tag) else None

def _percent_in_container(container: Tag) -> Optional[float]:
    if not container:
        return None
    # visible text
    txt = container.get_text(" ", strip=True) or ""
    m = PCT_RE.search(txt)
    if m:
        try:
            return float(m.group(1))
        except Exception:
            pass
    # attributes on descendants
    for el in container.find_all(True):
        for attr in ("data-change", "data-percent", "title", "aria-label"):
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

# ---------- NEW: parse __NEXT_DATA__ ----------
def _extract_items_from_nextdata(html: str) -> List[dict]:
    """
    FUT.GG is a Next.js app. Most pages embed a big JSON blob in <script id="__NEXT_DATA__">.
    We recursively scan for dicts that look like player cards with an id and a percent/ change field.
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
        s = soup.find("script", id="__NEXT_DATA__")
        if not s or not s.string:
            return []
        data = json.loads(s.string)
    except Exception as e:
        logging.info("Trending: no __NEXT_DATA__ or unparsable: %s", e)
        return []

    results: Dict[int, float] = {}

    def maybe_number(x: Any) -> Optional[float]:
        try:
            if isinstance(x, (int, float)):
                return float(x)
            if isinstance(x, str):
                m = PCT_RE.search(x)
                if m:
                    return float(m.group(1))
        except Exception:
            return None
        return None

    def walk(node: Any):
        # dict: look for card id and percent-ish fields
        if isinstance(node, dict):
            keys = set(k.lower() for k in node.keys())
            # common id keys FUT.GG might use
            id_key = None
            for cand in ("cardid", "playercardid", "id", "playerid"):
                if cand in keys:
                    id_key = cand
                    break
            # common percent/change keys
            pct_key = None
            for cand in (
                "percent", "percentage", "change", "changepercent", "changepercentage",
                "pricechangepercent", "pricechangepercentage", "momentumpercent",
                "pct"
            ):
                if cand in keys:
                    pct_key = cand
                    break
            if id_key and pct_key:
                try:
                    cid_raw = node.get(id_key)
                    cid = int(str(cid_raw))
                    pct_val = maybe_number(node.get(pct_key))
                    if pct_val is not None:
                        results[cid] = pct_val
                except Exception:
                    pass
            # also: nested momentum object
            if "momentum" in keys and isinstance(node.get("momentum"), dict):
                m = node["momentum"]
                pct_val = None
                for cand in ("percent", "percentage", "changePercent", "changePercentage"):
                    if cand in m:
                        pct_val = maybe_number(m.get(cand))
                        break
                cid = None
                for cand in ("cardId", "playerCardId", "id", "playerId"):
                    if cand in node:
                        try:
                            cid = int(str(node[cand]))
                            break
                        except Exception:
                            pass
                if cid and pct_val is not None:
                    results[cid] = pct_val

            # recurse
            for v in node.values():
                walk(v)

        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)

    items = [{"card_id": cid, "percent": pct} for cid, pct in results.items()]
    logging.info(f"[TRENDING][NEXT] Extracted {len(items)} items from __NEXT_DATA__")
    return items

def _extract_items_from_dom(html: str) -> List[dict]:
    """Previous DOM/tile method, kept as a fallback."""
    try:
        soup = BeautifulSoup(html, "html.parser")
        items: List[dict] = []
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

            container = _closest_card_container(a) or a
            pct = _percent_in_container(container)

            snippet = ""
            try:
                snippet = (container.get_text(" ", strip=True) or "")[:120]
            except Exception:
                pass
            logging.info(f"[TRENDING][DOM] href={href} cid={cid} pct={pct} snippet='{snippet}'")

            if pct is None:
                continue

            items.append({"card_id": cid, "percent": pct})
            seen.add(cid)

        logging.info(f"[TRENDING][DOM] Extracted {len(items)} items")
        return items
    except Exception as e:
        logging.warning("Trending: DOM extract error: %s", e)
        return []

def _extract_items(html: str) -> List[dict]:
    # Prefer Next.js data; fallback to DOM
    nx = _extract_items_from_nextdata(html)
    if nx:
        return nx
    return _extract_items_from_dom(html)

async def _page_items(tf: str, page: int) -> List[dict]:
    html = await _momentum_page(tf, page)
    if not html:
        return []
    return _extract_items(html)

async def _get_console_price(card_id: int, platform: str = "ps") -> Optional[int]:
    """Return a price or None. Tries price → lowestBin → LCPrice. PS first, then Xbox."""
    url = FUTGG_PRICE_URL.format(card_id=card_id)
    timeout = aiohttp.ClientTimeout(total=10)
    data = None
    try:
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(url, headers=REQ_HEADERS) as r:
                if r.status != 200:
                    return None
                data = await r.json()
    except Exception:
        return None
    if not isinstance(data, dict):
        return None

    def pick(d: dict) -> Optional[int]:
        if not isinstance(d, dict):
            return None
        for k in ("price", "lowestBin", "LCPrice"):
            v = d.get(k)
            if isinstance(v, (int, float)) and v > 0:
                return int(v)
        return None

    prices = data.get("prices", {}) or {}
    key = "ps" if platform == "ps" else "xbox"
    val = pick(prices.get(key, {}))
    if val is None and platform == "ps":
        val = pick(prices.get("xbox", {}))
    return val

async def _enrich_meta(req: Request, rows: List[dict]) -> List[dict]:
    if not rows:
        return []
    ids = [int(x["card_id"]) for x in rows]

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
        meta = {int(r["card_id"]): dict(r) for r in dbrows}
    except Exception as e:
        logging.warning("Trending: DB enrich failed: %s", e)
        meta = {}

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
    sem = asyncio.Semaphore(16)
    async def one(it: dict) -> dict:
        async with sem:
            try:
                price = await _get_console_price(int(it["card_id"]), "ps")
                it["prices"] = {"console": price, "pc": None}
            except Exception as e:
                logging.warning("Trending: price attach failed for %s: %s", it.get("card_id"), e)
                it["prices"] = {"console": None, "pc": None}
            return it
    results = await asyncio.gather(*(one(i) for i in items), return_exceptions=True)
    out: List[dict] = []
    for r in results:
        if isinstance(r, Exception):
            logging.warning("Trending: gather error: %s", r)
            continue
        out.append(r)
    return out

async def _fetch_trending(kind: Literal["risers", "fallers"], tf: str, limit: int) -> List[dict]:
    """
    Fallers: first page; Risers: last page. If fewer than limit, sample both ends.
    """
    try:
        first_html = await _momentum_page(tf, 1)
        last_page = _parse_last_page_num(first_html or "") if first_html else 1

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
    except Exception as e:
        logging.warning("Trending: fetch_trending failed (%s %sh): %s", kind, tf, e)
        return []

# ------------------ Route ------------------
@router.get("/trending", response_model=TrendingOut)
async def trending(
    req: Request,
    type_raw: str = Query("fallers", alias="type"),
    tf_raw: str = Query("24h", alias="tf"),
    limit: int = Query(10, ge=1, le=50),
    debug: bool = Query(False),
):
    """
    /api/trending?type=fallers&tf=24h
    Tolerates bad inputs: unknown type -> fallers, 'tf=today' -> 24h.
    """
    t = (type_raw or "fallers").lower()
    type_: Literal["risers", "fallers", "smart"] = "fallers" if t not in {"risers", "fallers", "smart"} else t  # type: ignore

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

    tf_num = _norm_tf(tf_raw)
    tf_human = _human_tf(tf_num)

    if tf_human not in limits.get("timeframes", ["24h"]):
        tf_num = "24"
        tf_human = "24h"
        limited = True

    max_items = int(limits.get("limit", 5))
    if limit > max_items:
        limit = max_items
        limited = True

    try:
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

            for cid, p6 in r6m.items():
                if cid in f24m:
                    smart_ids.add(cid)
                    smart_map[cid] = {"chg6hPct": p6, "chg24hPct": f24m[cid]}
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
            if debug:
                for e in enriched:
                    e["__debug"] = {"smart_map": {"6h": e["trend"]["chg6hPct"], "24h": e["trend"]["chg24hPct"]}}
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
            elif tf_num == "6":
                e["trend"] = {"chg6hPct": float(e["percent"])}
            else:
                e["trend"] = {"chg4hPct": float(e["percent"])}
        if debug:
            for e in enriched:
                e["__debug"] = {"percent": e.get("percent")}
        return {"type": type_, "timeframe": tf_human, "items": enriched[:limit], "limited": limited}

    except Exception as e:
        logging.warning("Trending: unhandled error, returning best-effort: %s", e)
        return {"type": type_, "timeframe": tf_human, "items": [], "limited": limited}
