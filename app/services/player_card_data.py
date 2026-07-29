# app/services/player_card_data.py
#
# Single source of truth for "everything that appears on the rendered
# player-card PNG" - used by both the render-hash computation (so the hash
# actually reflects what will be drawn) and the internal render route's
# data endpoint (so the frontend draws exactly what the hash was computed
# from). Deliberately a superset of players.py's `/api/players/{card_id}`
# select plus main.py's `/api/fut-player-definition/{card_id}` select
# (foot/skill_moves/weak_foot live only in the latter today).
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

import asyncpg

from app.futbin_client import fetch_card_layers

# Matches app/routers/v2/players.py's _LIVE_CARD_LAYERS_TIMEOUT rationale:
# bounded so a slow/blocked futbin.com response can't hang generation.
_LIVE_CARD_LAYERS_TIMEOUT = 3.0

_SELECT_FIELDS = """
    card_id, name, card_name, rating, version, position, altposition,
    pace, shooting, passing, dribbling, defending, physicality,
    skill_moves, weak_foot, foot, accelerate_type,
    image_url, card_bg_image, card_cutout_image, card_cutout_type,
    player_url, nation, nation_image, club, club_image, league, league_image,
    generated_card_url, generated_card_key, generated_card_hash,
    generated_card_width, generated_card_height, generated_card_at,
    generated_card_status, generated_card_error
"""


async def fetch_player_card_row(conn: asyncpg.Connection, card_id: str) -> Optional[Dict[str, Any]]:
    """Raw fut_players row (including current generated_card_* state), or
    None if the card doesn't exist. No live card-layers fallback here -
    callers that need the render-ready shape should use
    fetch_player_render_data instead."""
    row = await conn.fetchrow(
        f"SELECT {_SELECT_FIELDS} FROM fut_players WHERE card_id::text = $1",
        str(card_id),
    )
    return dict(row) if row else None


async def fetch_player_render_data(conn: asyncpg.Connection, card_id: str) -> Optional[Dict[str, Any]]:
    """Everything the export card needs to draw itself, with the same
    live-fallback-when-not-backfilled behaviour as the v1/v2 Player Page
    routes: card_bg_image/card_cutout_image are usually null (the backfill
    worker was never scheduled - see migration 022's own comment), so fetch
    them live off player_url when missing, bounded to 3s and exception-safe."""
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
