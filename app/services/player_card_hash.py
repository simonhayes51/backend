# app/services/player_card_hash.py
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict

PLAYER_CARD_RENDER_VERSION = 6

_HASHED_FIELDS = (
    "card_id",
    "name",
    "nickname",
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
    "futbin_rating",
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
    payload = {field: row.get(field) for field in _HASHED_FIELDS}
    payload["render_version"] = PLAYER_CARD_RENDER_VERSION
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
