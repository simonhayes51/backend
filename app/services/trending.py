import re
import aiohttp
from bs4 import BeautifulSoup
from typing import Any, Dict, List, Optional, Literal

FUTBIN_MARKET_URL = "https://www.futbin.com/market"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# --- helpers --------------------------------------------------------------

def _parse_price_text(txt: Optional[str]) -> Optional[int]:
    """
    Convert FUTBIN price text like '12,500', '23.5K', '1.2M' -> int coins.
    """
    if not txt:
        return None
    s = txt.strip().lower().replace(",", "")
    try:
        if s.endswith("m"):
            return int(float(s[:-1]) * 1_000_000)
        if s.endswith("k"):
            return int(float(s[:-1]) * 1_000)
        # plain number
        return int(float(s))
    except Exception:
        return None

async def _fetch(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    try:
        async with session.get(url) as resp:
            if resp.status == 200:
                return await resp.text()
            return None
    except Exception:
        return None

async def _get_ps_price(session: aiohttp.ClientSession, player_url: str, expected_rating: str) -> Optional[int]:
    """
    Scrape a FUTBIN player page, returning PS price (coins) for the block that matches the card rating.
    Falls back to the first "lowest-price-1" block.
    """
    html = await _fetch(session, player_url)
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")

    # try rating-matched block first
    blocks = soup.select("div.player-page-price-versions > div")
    for b in blocks:
        rating = b.select_one(".player-rating")
        price = b.select_one("div.price.inline-with-icon.lowest-price-1")
        if rating and price and rating.text.strip() == expected_rating:
            return _parse_price_text(price.text)

    # fallback: first lowest price block
    fallback = soup.select_one("div.price.inline-with-icon.lowest-price-1")
    return _parse_price_text(fallback.text if fallback else None)

def _extract_pid_from_link(link: str) -> Optional[int]:
    # FUTBIN links look like /player/12345-name or /23/player/12345...
    m = re.search(r"/player/(\d+)", link)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None

# --- core scraping (mirrors your Discord cog) -----------------------------

async def _fetch_trending_cards(session: aiohttp.ClientSession, timeframe: Literal["4h", "24h"]) -> List[Dict[str, Any]]:
    tf_map = {
        "24h": "div.market-players-wrapper.market-24-hours.m-row.space-between",
        "4h": "div.market-players-wrapper.market-4-hours.m-row.space-between",
    }
    html = await _fetch(session, FUTBIN_MARKET_URL)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    container = soup.select_one(tf_map[timeframe])
    cards = container.select("a.market-player-card") if container else []

    players: List[Dict[str, Any]] = []
    for card in cards:
        trend_tag = card.select_one(".market-player-change")
        if not trend_tag or "%" not in (trend_tag.text or ""):
            continue

        # parse % and sign
        txt = trend_tag.text.strip().replace("%", "").replace("+", "").replace(",", "")
        try:
            trend = float(txt)
            if "day-change-negative" in (trend_tag.get("class") or []):
                trend = -abs(trend)
        except Exception:
            continue

        name = card.select_one(".playercard-s-25-name")
        rating = card.select_one(".playercard-s-25-rating")
        link = card.get("href")
        if not (name and rating and link):
            continue

        href = f"https://www.futbin.com{link}?platform=ps"
        pid = _extract_pid_from_link(link)  # Optional

        players.append(
            {
                "name": name.text.strip(),
                "rating": int((rating.text or "0").strip() or 0),
                "trend": trend,
                "url": href,
                "pid": pid,
            }
        )
    return players

# --- public functions used by main.py ------------------------------------

async def get_trending_risers(tf: Literal["4h", "24h"]) -> List[Dict[str, Any]]:
    """
    Return top 10 risers with PS price. Fields match the frontend normaliser:
      name, rating, pid, version?, image?, price_ps, price_xb(None), percent
    """
    timeout = aiohttp.ClientTimeout(total=20)
    headers = {"User-Agent": USER_AGENT}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        raw = await _fetch_trending_cards(session, tf)
        out: List[Dict[str, Any]] = []
        for p in raw:
            if p["trend"] <= 0:
                continue
            price_ps = await _get_ps_price(session, p["url"], str(p["rating"]))
            if price_ps is None:
                continue
            out.append(
                {
                    "name": p["name"],
                    "rating": p["rating"],
                    "pid": p.get("pid"),
                    "version": None,
                    "image_url": None,
                    "price_ps": price_ps,
                    "price_xb": None,  # FUTBIN scrape is PS-only here
                    "percent": round(float(p["trend"]), 2),
                }
            )
            if len(out) == 10:
                break
        return out

async def get_trending_fallers(tf: Literal["4h", "24h"]) -> List[Dict[str, Any]]:
    timeout = aiohttp.ClientTimeout(total=20)
    headers = {"User-Agent": USER_AGENT}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        raw = await _fetch_trending_cards(session, tf)
        out: List[Dict[str, Any]] = []
        for p in raw:
            if p["trend"] >= 0:
                continue
            price_ps = await _get_ps_price(session, p["url"], str(p["rating"]))
            if price_ps is None:
                continue
            out.append(
                {
                    "name": p["name"],
                    "rating": p["rating"],
                    "pid": p.get("pid"),
                    "version": None,
                    "image_url": None,
                    "price_ps": price_ps,
                    "price_xb": None,
                    "percent": round(float(p["trend"]), 2),
                }
            )
            if len(out) == 10:
                break
        return out

async def get_trending_smart() -> List[Dict[str, Any]]:
    """
    Smart movers = players whose trend flips between 4h and 24h.
    Return top 10; include percent_4h and percent_24h, plus PS price.
    """
    timeout = aiohttp.ClientTimeout(total=30)
    headers = {"User-Agent": USER_AGENT}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        short = await _fetch_trending_cards(session, "4h")
        long = await _fetch_trending_cards(session, "24h")
        map_4h = {(p["name"], p["rating"]): p for p in short}

        smart: List[Dict[str, Any]] = []
        for p in long:
            key = (p["name"], p["rating"])
            if key not in map_4h:
                continue
            p4 = float(map_4h[key]["trend"])
            p24 = float(p["trend"])
            if (p4 > 0 > p24) or (p4 < 0 < p24):
                price_ps = await _get_ps_price(session, p["url"], str(p["rating"]))
                if price_ps is None:
                    continue
                smart.append(
                    {
                        "name": p["name"],
                        "rating": p["rating"],
                        "pid": p.get("pid"),
                        "version": None,
                        "image_url": None,
                        "price_ps": price_ps,
                        "price_xb": None,
                        "percent_4h": round(p4, 2),
                        "percent_24h": round(p24, 2),
                    }
                )
                if len(smart) == 10:
                    break

        return smart
