# app/routers/player_cards.py
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.db import get_player_db, get_player_pool
from app.routers.admin import require_admin
from app.services.player_card_backfill import (
    VALID_CARD_GROUPS,
    get_backfill_status,
    start_backfill,
)
from app.services.player_card_data import fetch_player_render_data
from app.services.player_card_generation import (
    PlayerCardNotFoundError,
    ensure_generated_player_card,
)
from app.services.player_card_token import verify_render_token

internal_router = APIRouter(prefix="/api/internal", tags=["internal-render"])
admin_router = APIRouter(prefix="/api/admin/player-cards", tags=["admin-player-cards"])

_ALT_POSITION_SPLIT_RE = re.compile(r"[,;/|]+|\s+")


def _split_alt_positions(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    return [p for p in _ALT_POSITION_SPLIT_RE.split(raw.strip()) if p]


def _resolved_cutout_type(row: Dict[str, Any]) -> Optional[str]:
    cutout_url = str(row.get("card_cutout_image") or "")
    filename = urlparse(cutout_url).path.rsplit("/", 1)[-1].lower()

    if filename and re.fullmatch(r"\d+\.png", filename):
        return "base"
    if filename.startswith("p") and filename.endswith(".png"):
        return "special"
    return row.get("card_cutout_type")


def _versioned_url(
    url: Optional[str],
    generated_at: Optional[datetime],
) -> Optional[str]:
    if not url or not generated_at:
        return url

    separator = "&" if "?" in url else "?"
    return f"{url}{separator}v={int(generated_at.timestamp() * 1000)}"


@internal_router.get("/render/player-card/{card_id}")
async def get_player_card_render_data(
    card_id: str,
    token: str = Query(...),
    conn=Depends(get_player_db),
) -> Dict[str, Any]:
    if not verify_render_token(card_id, token):
        raise HTTPException(
            status_code=403,
            detail="Invalid or expired render token",
        )

    row = await fetch_player_render_data(conn, card_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail="Player not found",
        )

    return {
        "data": {
            "cardId": row["card_id"],
            "name": row["name"],
            "displayName": (
                row.get("display_name")
                or row.get("nickname")
                or row.get("card_name")
                or row["name"]
            ),
            "rating": row["rating"],
            "position": row["position"],
            "altPositions": _split_alt_positions(row.get("altposition")),
            "versionLabel": row.get("version"),
            "stats": {
                "pace": row.get("pace"),
                "shooting": row.get("shooting"),
                "passing": row.get("passing"),
                "dribbling": row.get("dribbling"),
                "defending": row.get("defending"),
                "physicality": row.get("physicality"),
            },
            "skillMoves": row.get("skill_moves"),
            "weakFoot": row.get("weak_foot"),
            "preferredFoot": row.get("foot"),
            "futbinRating": row.get("futbin_rating"),
            "bgImage": row.get("card_bg_image"),
            "cardColor": row.get("card_color"),
            "cutoutImage": row.get("card_cutout_image"),
            "cutoutType": _resolved_cutout_type(row),
            "fallbackImage": row.get("image_url"),
            "nationImage": row.get("nation_image"),
            "clubImage": row.get("club_image"),
            "leagueImage": row.get("league_image"),
        }
    }


class BackfillRequest(BaseModel):
    mode: str = "missing"
    card_group: str = "all"
    limit: int = 50_000
    concurrency: int = 3
    force: bool = False


@admin_router.post("/backfill")
async def start_player_card_backfill(
    payload: BackfillRequest = BackfillRequest(),
    admin=Depends(require_admin),
):
    if payload.mode not in ("missing", "stale"):
        raise HTTPException(
            status_code=400,
            detail="mode must be 'missing' or 'stale'",
        )

    if payload.card_group not in VALID_CARD_GROUPS:
        raise HTTPException(
            status_code=400,
            detail=f"card_group must be one of: {', '.join(VALID_CARD_GROUPS)}",
        )

    if not (1 <= payload.limit <= 50_000):
        raise HTTPException(
            status_code=400,
            detail="limit must be between 1 and 50000",
        )

    if not (1 <= payload.concurrency <= 4):
        raise HTTPException(
            status_code=400,
            detail="concurrency must be between 1 and 4 (Chromium is heavy)",
        )

    pool = await get_player_pool()

    return await start_backfill(
        pool,
        mode=payload.mode,
        card_group=payload.card_group,
        limit=payload.limit,
        concurrency=payload.concurrency,
        force=payload.force,
    )


@admin_router.get("/backfill/status")
async def player_card_backfill_status(
    admin=Depends(require_admin),
):
    return get_backfill_status()


class GenerateCardRequest(BaseModel):
    force: bool = False


@admin_router.post("/{card_id}/generate")
async def generate_player_card(
    card_id: str,
    payload: GenerateCardRequest = GenerateCardRequest(),
    admin=Depends(require_admin),
):
    pool = await get_player_pool()

    try:
        result = await ensure_generated_player_card(
            pool,
            card_id,
            force=payload.force,
        )
    except PlayerCardNotFoundError:
        raise HTTPException(
            status_code=404,
            detail="Player not found",
        )

    if result.get("status") == "error":
        raise HTTPException(
            status_code=502,
            detail=result.get("error") or "Card generation failed",
        )

    return result


class FlagCardRequest(BaseModel):
    reason: str


@admin_router.post("/{card_id}/flag")
async def flag_player_card(
    card_id: str,
    payload: FlagCardRequest,
    admin=Depends(require_admin),
):
    """Mark a generated card as known-bad (wrong player, face-only crop,
    etc). Does not reset generated_card_status/url itself - the on-demand
    claim query (app/services/player_card_ondemand.py) already treats a
    ready+flagged card as eligible for regeneration on the next request for
    it, same retry mechanism as an 'error' status, so the currently-served
    (bad) image stays up until a real replacement is ready."""
    pool = await get_player_pool()
    row = await pool.fetchrow(
        """
        UPDATE fut_players
           SET generated_card_flagged = TRUE,
               generated_card_flag_reason = $2,
               generated_card_flagged_at = NOW()
         WHERE card_id::text = $1
        RETURNING card_id::text AS card_id, generated_card_flagged,
                  generated_card_flag_reason, generated_card_flagged_at
        """,
        str(card_id), payload.reason,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Player not found")
    return {
        "ok": True,
        "cardId": row["card_id"],
        "flagged": row["generated_card_flagged"],
        "flagReason": row["generated_card_flag_reason"],
        "flaggedAt": row["generated_card_flagged_at"].isoformat() if row["generated_card_flagged_at"] else None,
    }


@admin_router.post("/{card_id}/unflag")
async def unflag_player_card(
    card_id: str,
    admin=Depends(require_admin),
):
    pool = await get_player_pool()
    row = await pool.fetchrow(
        """
        UPDATE fut_players
           SET generated_card_flagged = FALSE,
               generated_card_flag_reason = NULL,
               generated_card_flagged_at = NULL
         WHERE card_id::text = $1
        RETURNING card_id::text AS card_id
        """,
        str(card_id),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Player not found")
    return {"ok": True, "cardId": row["card_id"], "flagged": False}


@admin_router.get("/flagged")
async def list_flagged_player_cards(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    admin=Depends(require_admin),
):
    """Paginated review queue of currently-flagged cards for admin tooling /
    the frontend admin tab."""
    pool = await get_player_pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM fut_players WHERE generated_card_flagged = TRUE"
        )
        rows = await conn.fetch(
            """
            SELECT card_id::text AS card_id, name, generated_card_url,
                   generated_card_status, generated_card_flag_reason,
                   generated_card_flagged_at
              FROM fut_players
             WHERE generated_card_flagged = TRUE
             ORDER BY generated_card_flagged_at DESC NULLS LAST
             LIMIT $1 OFFSET $2
            """,
            limit, offset,
        )
    return {
        "ok": True,
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "cardId": r["card_id"],
                "name": r["name"],
                "imageUrl": r["generated_card_url"],
                "status": r["generated_card_status"],
                "flagReason": r["generated_card_flag_reason"],
                "flaggedAt": r["generated_card_flagged_at"].isoformat() if r["generated_card_flagged_at"] else None,
            }
            for r in rows
        ],
    }


@admin_router.get("/{card_id}/status")
async def get_player_card_status(
    card_id: str,
    admin=Depends(require_admin),
    conn=Depends(get_player_db),
):
    row = await fetch_player_render_data(conn, card_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail="Player not found",
        )

    generated_at = row.get("generated_card_at")

    return {
        "ok": True,
        "cardId": row["card_id"],
        "imageUrl": _versioned_url(
            row.get("generated_card_url"),
            generated_at,
        ),
        "hash": row.get("generated_card_hash"),
        "width": row.get("generated_card_width"),
        "height": row.get("generated_card_height"),
        "status": row.get("generated_card_status"),
        "error": row.get("generated_card_error"),
        "generatedAt": (
            generated_at.isoformat()
            if generated_at
            else None
        ),
    }
