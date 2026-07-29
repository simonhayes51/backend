# app/services/player_card_data.py
#
# Single source of truth for everything drawn on the generated player card.
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

import asyncpg

from app.futbin_client import fetch_card_layers

_LIVE_CARD_LAYERS_TIMEOUT = 3.0

_SELECT_FIELDS = """
    card_id, name, nickname, card_name, rating, version, position, altposition,
    pace, shooting, passing, dribbling, defending, physicality,
    skill_moves, weak_foot, foot, accelerate_type,
    image_url, card_bg_image, card_cutout_image, card_cutout_type,
    player_url, nation, nation_image, club, club_image, league, league_image,
    generated_card_url, generated_card_key, generated_card_hash,
    generated_card_width, generated_card_height, generated_card_at,
    generated_card_status, generated_card_error
"""


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

    if not row.get("card_bg_image") and row.get("player_url"):
        try:
            layers = await asyncio.wait_for(
                fetch_card_layers(row["player_url"]), timeout=_LIVE_CARD_LAYERS_TIMEOUT
            )
        except Exception:
            layers = None
        if layers:
            row["card_bg_image"] = row.get("card_bg_image") or layers.get("bgImageUrl")
            row["card_cutout_image"] = row.get("card_cutout_image") or layers.get("cutoutImageUrl")
            row["card_cutout_type"] = row.get("card_cutout_type") or layers.get("cutoutType")
            row["card_name"] = row.get("card_name") or layers.get("cardName")

    return row
