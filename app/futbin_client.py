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
from typing import Optional

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
