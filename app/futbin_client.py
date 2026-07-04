# app/futbin_client.py
"""
On-demand live price fetch from a single futbin.com player page, used as the
freshest price source (called behind a short TTL cache). This complements
the auto_sync repo's periodic full-catalog crawl, which keeps
fut_players.price_num reasonably current but not second-by-second.

futbin.com has no Cloudflare bot protection (confirmed by testing), unlike
fut.gg, so this is a plain unauthenticated GET + HTML parse - no session,
proxy, or rendering service required.
"""
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import aiohttp

from app.db import get_player_pool

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SBCSolver/1.5)"}
REQUEST_TIMEOUT = 15

# futbin's listing rows only ever showed platform-ps-only / platform-pc-only
# columns (PS and Xbox share one FUT market, so there's no separate xbox
# column) - confirmed by inspecting real page markup during the crawler work.
_PLATFORM_CLASS = {"ps": "platform-ps-only", "pc": "platform-pc-only"}


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


def parse_price(html: str, fb_plat: str) -> Optional[int]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    plat_class = _PLATFORM_CLASS.get(fb_plat, "platform-ps-only")

    # Primary: same price-cell markup confirmed on futbin's listing rows.
    cell = soup.find(class_=re.compile(rf"\b{plat_class}\b"))
    if cell:
        price_div = cell.find("div", class_=re.compile(r"\bprice\b"))
        if price_div:
            val = _num(price_div.get_text(strip=True))
            if val:
                return val

    # Fallback in case the individual player page uses different markup
    # than the listing rows.
    box = (
        soup.find("div", class_=re.compile(r"price[- ]?box", re.I))
        or soup.find("div", class_=re.compile(r"price-box-original-player", re.I))
        or soup
    )
    plat_word = "pc" if fb_plat == "pc" else "ps"
    for tag in box.find_all(string=re.compile(rf"\b{plat_word}\b", re.I)):
        txt = tag.parent.get_text(" ", strip=True)
        m = re.search(r"(\d[\d,\.kK]+)", txt)
        if m:
            val = _num(m.group(1))
            if val:
                return val
    for d in box.find_all("div", class_=re.compile(r"lowest-price", re.I)):
        val = _num(d.get_text(" ", strip=True))
        if val:
            return val

    return None


async def fetch_price_by_url(player_url: str, platform: str) -> Optional[int]:
    """platform is our normalized ps|xbox|pc; futbin has no separate xbox
    column so xbox is mapped onto the shared ps/xbox console market."""
    fb_plat = "pc" if platform == "pc" else "ps"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(player_url, headers=HEADERS, timeout=REQUEST_TIMEOUT) as r:
                if r.status != 200:
                    return None
                html = await r.text()
    except Exception:
        return None
    return parse_price(html, fb_plat)


async def get_player_url(card_id: int) -> Optional[str]:
    pool = await get_player_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT player_url FROM fut_players WHERE card_id::text = $1", str(card_id)
        )


async def fetch_price_by_card_id(card_id: int, platform: str) -> Optional[int]:
    """Convenience wrapper for callers that only have a card_id (e.g.
    watchlist.py's _fetch_price, which lives outside the player DB pool)."""
    player_url = await get_player_url(card_id)
    if not player_url:
        return None
    return await fetch_price_by_url(player_url, platform)


# --- Sales history --------------------------------------------------------
# futbin's price chart on /market is a client-rendered Highcharts SVG, not a
# clean JSON endpoint - not worth reverse-engineering pixel paths for. But
# the same player has a plain server-rendered "Player Sales History" table
# at /sales/{futbin_id}/{slug}?platform=ps|pc with real timestamped sales,
# confirmed against a real page - that's what we use for recent sales,
# price range, and trend instead.
#
# The sales page's slug doesn't match the player page's slug (e.g.
# "aitana-bonmati" vs "aitana-bonmati-conca" for the same card), so it
# can't be derived by substring substitution on player_url - confirmed by
# inspecting a real /market page's "Full History" link, which is the
# reliable source for the correct path.
_SALE_DATE_RE = re.compile(r"[A-Za-z]{3} \d{1,2}, \d{1,2}:\d{2} [AP]M")


async def _resolve_sales_path(player_url: str) -> Optional[str]:
    market_url = player_url.rstrip("/") + "/market"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(market_url, headers=HEADERS, timeout=REQUEST_TIMEOUT) as r:
                if r.status != 200:
                    return None
                html = await r.text()
    except Exception:
        return None

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    a = soup.find("a", class_=re.compile(r"\bmarket-grid-lates-sale-link\b"))
    if not a or not a.get("href"):
        return None
    return a["href"].split("?")[0]


async def _sales_url(player_url: str, platform: str) -> Optional[str]:
    fb_plat = "pc" if platform == "pc" else "ps"
    path = await _resolve_sales_path(player_url)
    if path:
        return f"https://www.futbin.com{path}?platform={fb_plat}"
    # Fallback if the market page lookup fails for any reason - wrong slug
    # text but the same numeric id, which futbin appears to route by
    # regardless of slug.
    if "/player/" in player_url:
        sales_base = player_url.replace("/player/", "/sales/")
        return f"{sales_base}?platform={fb_plat}"
    return None


def _parse_sale_date(date_text: str, now: Optional[datetime] = None) -> Optional[str]:
    if not date_text:
        return None
    now = now or datetime.now(timezone.utc)
    try:
        dt = datetime.strptime(f"{date_text} {now.year}", "%b %d, %I:%M %p %Y")
    except ValueError:
        return None
    dt = dt.replace(tzinfo=timezone.utc)
    if dt > now + timedelta(days=1):
        dt = dt.replace(year=now.year - 1)
    return dt.isoformat()


def parse_sales_history(html: str, limit: int = 20) -> List[Dict[str, Any]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="auctions-table")
    if not table:
        return []
    body = table.find("tbody")
    if not body:
        return []

    sales: List[Dict[str, Any]] = []
    for row in body.find_all("tr"):
        if len(sales) >= limit:
            break
        tds = row.find_all("td")
        if len(tds) < 5:
            continue

        date_div = tds[0].find("div")
        icon = date_div.find("i") if date_div else None
        sold = bool(icon and any("fa-check" in c for c in icon.get("class", [])))
        if not sold:
            continue

        date_span = date_div.find("span", class_="sales-date-time") if date_div else None
        date_text = date_span.get_text(strip=True) if date_span else None
        m = _SALE_DATE_RE.search(date_text or "")
        sold_date = _parse_sale_date(m.group(0)) if m else None

        sold_price = _num(tds[2].get_text(strip=True))
        if not sold_price:
            continue

        sales.append({"soldDate": sold_date, "soldPrice": sold_price})

    return sales


async def fetch_recent_sales(player_url: str, platform: str, limit: int = 20) -> List[Dict[str, Any]]:
    url = await _sales_url(player_url, platform)
    if not url:
        return []
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT) as r:
                if r.status != 200:
                    return []
                html = await r.text()
    except Exception:
        return []
    return parse_sales_history(html, limit=limit)


# --- Card image layers -----------------------------------------------------
# futbin has no server-rendered flat card image - the card seen on the site
# is assembled client-side from a card-template background PNG and a
# separate player cutout PNG (their own "download card" button works by
# rasterizing the live DOM with domtoimage, not by fetching one image).
# Confirmed against a real player page: the background sits behind an
# `<img class="playercard-26-bg">` and the cutout is either
# `playercard-26-special-img` (special/non-base cards) or
# `playercard-26-base-img` (base gold cards) - both plain <img src=...>.
def parse_card_layers(html: str) -> Dict[str, Optional[str]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    bg = soup.find("img", class_=re.compile(r"\bplayercard-26-bg\b"))
    cutout = soup.find("img", class_=re.compile(r"\bplayercard-26-special-img\b")) or soup.find(
        "img", class_=re.compile(r"\bplayercard-26-base-img\b")
    )
    return {
        "bgImageUrl": bg.get("src") if bg else None,
        "cutoutImageUrl": cutout.get("src") if cutout else None,
    }


async def fetch_card_layers(player_url: str) -> Dict[str, Optional[str]]:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(player_url, headers=HEADERS, timeout=REQUEST_TIMEOUT) as r:
                if r.status != 200:
                    return {"bgImageUrl": None, "cutoutImageUrl": None}
                html = await r.text()
    except Exception:
        return {"bgImageUrl": None, "cutoutImageUrl": None}
    return parse_card_layers(html)


async def debug_fetch_player_page(player_url: str) -> Dict[str, Any]:
    """Temporary diagnostic - reports what our own request actually gets
    back from futbin, to tell apart 'parser is wrong' from 'we're getting a
    different page than a browser gets' without needing terminal/curl
    access on either end."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(player_url, headers=HEADERS, timeout=REQUEST_TIMEOUT) as r:
                status = r.status
                html = await r.text()
    except Exception as e:
        return {"player_url": player_url, "error": str(e)}

    idx = html.find("playercard-26-bg")
    return {
        "player_url": player_url,
        "http_status": status,
        "content_length": len(html),
        "has_playercard_26": "playercard-26" in html,
        "playercard_26_bg_count": html.count("playercard-26-bg"),
        "has_price_box": "price-box" in html or "platform-ps-only" in html,
        "sample_around_first_match": html[max(0, idx - 150): idx + 150] if idx != -1 else None,
        "layers_parsed": parse_card_layers(html),
    }
