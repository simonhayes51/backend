# app/services/player_card_data.py
# Single source of truth for everything drawn on the generated player card.
from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import aiohttp
import asyncpg
from bs4 import BeautifulSoup

from app.futbin_client import HEADERS, REQUEST_TIMEOUT

_LIVE_CARD_LAYERS_TIMEOUT = 5.0

_CARD_COLOR_RE = re.compile(
    r"--cardColor\s*:\s*([^;\"']+)",
    re.IGNORECASE,
)

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
            score = (
                float(descriptor[:-1])
                if descriptor.endswith("x")
                else float(descriptor.rstrip("w"))
            )
        except ValueError:
            score = 1.0

        candidates.append((score, parts[0]))

    if candidates:
        return max(candidates, key=lambda item: item[0])[1]

    return image.get("src")


def _extract_card_color(element) -> Optional[str]:
    """Extract FUTBIN's inline --cardColor CSS variable."""

    if element is None:
        return None

    style = element.get("style") or ""
    match = _CARD_COLOR_RE.search(style)

    if not match:
        return None

    value = match.group(1).strip()
    return value or None


def _infer_cutout_type(
    url: Optional[str],
    parsed_type: Optional[str],
) -> Optional[str]:
    """Infer FUTBIN's two player-image models from the asset filename.

    Full-card special cutouts use names such as ``p50579499.png``. Base/icon
    portraits use a plain numeric player id such as ``1183.png``. Some icon
    pages include both image elements in the hero markup, so choosing merely
    by which class BeautifulSoup finds first can incorrectly mark a numeric
    portrait as ``special`` (Cannavaro is a confirmed example).
    """
    if not url:
        return parsed_type

    filename = urlparse(url).path.rsplit("/", 1)[-1].lower()
    stem = filename.rsplit(".", 1)[0]

    if re.fullmatch(r"p\d+", stem):
        return "special"

    if re.fullmatch(r"\d+", stem):
        return "base"

    return parsed_type


async def _fetch_live_card_assets(
    player_url: str,
) -> Dict[str, Optional[str]]:
    empty = {
        "bgImageUrl": None,
        "cutoutImageUrl": None,
        "cutoutType": None,
        "cardName": None,
        "cardColor": None,
        "nationImageUrl": None,
        "leagueImageUrl": None,
        "clubImageUrl": None,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                player_url,
                headers=HEADERS,
                timeout=REQUEST_TIMEOUT,
            ) as response:
                if response.status != 200:
                    return empty

                html = await response.text()

    except Exception:
        return empty

    soup = BeautifulSoup(html, "html.parser")
    hero = soup.find("div", class_="playercard-l")
    scope = hero or soup

    background = scope.find("img", class_="playercard-26-bg")
    special_cutout = scope.find(
        "img",
        class_="playercard-26-special-img",
    )
    base_cutout = scope.find(
        "img",
        class_="playercard-26-base-img",
    )

    # Prefer the base portrait when both nodes exist. Hidden/alternate special
    # nodes can be present on icon pages; the numeric filename then confirms
    # which rendering model is actually required.
    cutout = base_cutout or special_cutout
    parsed_type = (
        "base"
        if base_cutout
        else ("special" if special_cutout else None)
    )
    cutout_url = _best_image_url(cutout)

    name_element = scope.find(
        "div",
        class_="playercard-26-name",
    )
    info_row = scope.find(
        "div",
        class_="playercard-26-info-row",
    )

    nation = (
        info_row.find("img", class_="nation")
        if info_row
        else None
    )
    league = (
        info_row.find("img", class_="playercard-26-league")
        if info_row
        else None
    )
    club = (
        info_row.find("img", class_="playercard-26-club")
        if info_row
        else None
    )

    return {
        "bgImageUrl": _best_image_url(background),
        "cutoutImageUrl": cutout_url,
        "cutoutType": _infer_cutout_type(
            cutout_url,
            parsed_type,
        ),
        "cardName": (
            name_element.get_text(strip=True)
            if name_element
            else None
        ),
        "cardColor": _extract_card_color(hero),
        "nationImageUrl": _best_image_url(nation),
        "leagueImageUrl": _best_image_url(league),
        "clubImageUrl": _best_image_url(club),
    }


async def fetch_player_card_row(
    conn: asyncpg.Connection,
    card_id: str,
) -> Optional[Dict[str, Any]]:
    row = await conn.fetchrow(
        f"""
        SELECT {_SELECT_FIELDS}
        FROM fut_players
        WHERE card_id::text = $1
        """,
        str(card_id),
    )

    return dict(row) if row else None


async def fetch_player_render_data(
    conn: asyncpg.Connection,
    card_id: str,
) -> Optional[Dict[str, Any]]:
    row = await fetch_player_card_row(conn, card_id)

    if row is None:
        return None

    # This is a live-only render value and is not stored in fut_players.
    row["card_color"] = None

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
            row["card_bg_image"] = (
                assets.get("bgImageUrl")
                or row.get("card_bg_image")
            )
            row["card_cutout_image"] = (
                assets.get("cutoutImageUrl")
                or row.get("card_cutout_image")
            )
            row["card_cutout_type"] = (
                assets.get("cutoutType")
                or row.get("card_cutout_type")
            )
            row["card_name"] = (
                assets.get("cardName")
                or row.get("card_name")
            )
            row["card_color"] = assets.get("cardColor")
            row["nation_image"] = (
                assets.get("nationImageUrl")
                or row.get("nation_image")
            )
            row["league_image"] = (
                assets.get("leagueImageUrl")
                or row.get("league_image")
            )
            row["club_image"] = (
                assets.get("clubImageUrl")
                or row.get("club_image")
            )

    # Apply the same deterministic inference to stored fallback data. This
    # prevents one stale card_cutout_type value from defeating the correct
    # rendering model when the live FUTBIN request times out.
    row["card_cutout_type"] = _infer_cutout_type(
        row.get("card_cutout_image"),
        row.get("card_cutout_type"),
    )

    return row
