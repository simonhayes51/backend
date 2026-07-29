# app/services/player_card_data.py
# Single source of truth for everything drawn on the generated player card.
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

import aiohttp
import asyncpg
from bs4 import BeautifulSoup

from app.futbin_client import HEADERS, REQUEST_TIMEOUT

_LIVE_CARD_LAYERS_TIMEOUT = 5.0

_SELECT_FIELDS = """
    card_id, name, nickname, card_name, rating, version, position, altposition,
    pace, shooting, passing, dribbling, defending, physicality,
    skill_moves, weak_foot, foot, accelerate_type, futbin_rating,
    image_url, card_bg_image, card_cutout_image, card_cutout_type,
    player_url, nation, nation_image, club, club_image, league, league_image,
    generated_card_url, generated_card_key, generated_card_hash,
    generated_card_width, generated_card_height, generated_card_at,
    generated_card_status, generated_card_error
"""


def _best_image_url(image) -> Optional[str]:
    """Prefer FUTBIN's signed 2x srcset asset without modifying its query.

    FUTBIN/Imgix signatures cover the complete query string, so changing w=
    invalidates the URL. The page already supplies valid 2x signed variants;
    use the largest candidate exactly as emitted.
    """
    if image is None:
        return None

    candidates = []
    for candidate in (image.get("srcset") or "").split(","):
        parts = candidate.strip().split()
        if not parts:
            continue
        descriptor = parts[1] if len(parts) > 1 else "1x"
        try:
            score = float(descriptor[:-1]) if descriptor.endswith("x") else float(descriptor.rstrip("w"))
        except ValueError:
            score = 1.0
        candidates.append((score, parts[0]))

    if candidates:
        return max(candidates, key=lambda item: item[0])[1]
    return image.get("src")


async def _fetch_live_card_assets(player_url: str) -> Dict[str, Optional[str]]:
    empty = {
        "bgImageUrl": None,
        "cutoutImageUrl": None,
        "cutoutType": None,
        "cardName": None,
        "nationImageUrl": None,
        "leagueImageUrl": None,
        "clubImageUrl": None,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(player_url, headers=HEADERS, timeout=REQUEST_TIMEOUT) as response:
                if response.status != 200:
                    return empty
                html = await response.text()
    except Exception:
        return empty

    soup = BeautifulSoup(html, "html.parser")
    hero = soup.find("div", class_="playercard-l")
    scope = hero or soup

    background = scope.find("img", class_="playercard-26-bg")
    special_cutout = scope.find("img", class_="playercard-26-special-img")
    base_cutout = scope.find("img", class_="playercard-26-base-img")
    cutout = special_cutout or base_cutout
    name_element = scope.find("div", class_="playercard-26-name")
    info_row = scope.find("div", class_="playercard-26-info-row")

    nation = info_row.find("img", class_="nation") if info_row else None
    league = info_row.find("img", class_="playercard-26-league") if info_row else None
    club = info_row.find("img", class_="playercard-26-club") if info_row else None

    return {
        "bgImageUrl": _best_image_url(background),
        "cutoutImageUrl": _best_image_url(cutout),
        "cutoutType": "special" if special_cutout else ("base" if base_cutout else None),
        "cardName": name_element.get_text(strip=True) if name_element else None,
        "nationImageUrl": _best_image_url(nation),
        "leagueImageUrl": _best_image_url(league),
        "clubImageUrl": _best_image_url(club),
    }


async def fetch_player_card_row(conn: asyncpg.Connection, card_id: str) -> Optional[Dict[str, Any]]:
    row = await conn.fetchrow(
        f"SELECT {_SELECT_FIELDS} FROM fut_players WHERE card_id::text = $1",
        str(card_id),
    )
    return dict(row) if row else None


async def fetch_player_render_data(conn: asyncpg.Connection, card_id: str) -> Optional[Dict[str, Any]]:
    row = await fetch_player_card_row(conn, card_id)
    if row is None:
        return None

    if row.get("player_url"):
        try:
            assets = await asyncio.wait_for(
                _fetch_live_card_assets(row["player_url"]),
                timeout=_LIVE_CARD_LAYERS_TIMEOUT,
            )
        except Exception:
            assets = None

        if assets:
            # Prefer the live page's signed 2x URLs for export quality. Fall
            # back to stored DB URLs when FUTBIN cannot be reached.
            row["card_bg_image"] = assets.get("bgImageUrl") or row.get("card_bg_image")
            row["card_cutout_image"] = assets.get("cutoutImageUrl") or row.get("card_cutout_image")
            row["card_cutout_type"] = assets.get("cutoutType") or row.get("card_cutout_type")
            row["card_name"] = assets.get("cardName") or row.get("card_name")
            row["nation_image"] = assets.get("nationImageUrl") or row.get("nation_image")
            row["league_image"] = assets.get("leagueImageUrl") or row.get("league_image")
            row["club_image"] = assets.get("clubImageUrl") or row.get("club_image")

    return row
