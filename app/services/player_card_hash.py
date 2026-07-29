# app/services/player_card_hash.py
#
# Deterministic SHA-256 "does this card need re-rendering" hash. Bump
# PLAYER_CARD_RENDER_VERSION whenever the export layout/template changes
# (new field drawn, repositioned badge, resized canvas, etc.) so every
# previously-generated PNG is treated as stale on the next
# ensure_generated_player_card() call, even though none of the underlying
# player data changed.
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict

# Version 2 switches the PNG route to the dedicated PlayerCardExportArt
# composition and invalidates the original low-quality generated images.
PLAYER_CARD_RENDER_VERSION = 2

# Every field that can change what's drawn on the card. Deliberately
# excludes volatile, non-visible fields (price, price_num, price_updated_at,
# games_played_*, avg_goals_*, top_chem_style_*, generated_card_* state
# itself) - those change constantly and never affect the rendered image, so
# hashing them would defeat caching entirely.
_HASHED_FIELDS = (
    "card_id",
    "name",
    "card_name",
    "rating",
    "version",
    "position",
    "altposition",
    "pace",
    "shooting",
    "passing",
    "dribbling",
    "defending",
    "physicality",
    "skill_moves",
    "weak_foot",
    "foot",
    "accelerate_type",
    "image_url",
    "card_bg_image",
    "card_cutout_image",
    "card_cutout_type",
    "nation",
    "nation_image",
    "club",
    "club_image",
    "league",
    "league_image",
)


def compute_card_render_hash(row: Dict[str, Any]) -> str:
    """row is expected to be (at least) the shape returned by
    fetch_player_render_data() - a plain dict, not an asyncpg.Record."""
    payload = {field: row.get(field) for field in _HASHED_FIELDS}
    payload["render_version"] = PLAYER_CARD_RENDER_VERSION
    # sort_keys makes key ordering irrelevant to the digest; separators
    # strip whitespace so formatting differences can't shift the hash.
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
