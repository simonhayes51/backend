# app/routers/v2/market.py
#
# Thin wrapper over the already-existing (but previously unrouted)
# fair_value.get_card_fair_values_batch, applying the same teaser/full
# gating split as the single-card /api/market/fair-value/{card_id}
# route by reusing that route's own _teaser() helper. Feeds the v2 Home
# Dashboard's watchlist/movers widgets so they can fetch N cards in one
# request instead of N.
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request

from app.auth.entitlements import compute_entitlements
from app.routers.fair_value import _teaser
from app.services import fair_value as fv

router = APIRouter(tags=["v2-market"])

MAX_BATCH_IDS = 100


@router.get("/market/fair-value/batch")
async def fair_value_batch(ids: str, request: Request) -> Dict[str, Any]:
    try:
        card_ids = [int(x) for x in ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(400, "ids must be a comma-separated list of integers")
    card_ids = card_ids[:MAX_BATCH_IDS]

    pool = getattr(request.app.state, "player_pool", None)
    if pool is None:
        raise HTTPException(503, "player pool not ready")

    rows = await fv.get_card_fair_values_batch(pool, card_ids)
    ent = await compute_entitlements(request)
    unlocked = "fair_value" in ent["features"]

    items: List[Dict[str, Any]] = []
    for row in rows:
        if row.get("data_quality_suspect"):
            # Matches the single-card route's own branch: never show exact
            # numbers OR a verdict for a card we don't trust the data for,
            # regardless of tier.
            items.append({
                "card_id": row["card_id"],
                "name": row["name"],
                "rating": row["rating"],
                "version": row["version"],
                "image_url": row["image_url"],
                "data_quality_suspect": True,
                "message": "We're not confident in this card's market data yet - check back shortly.",
            })
        elif unlocked:
            row = dict(row)
            row["locked"] = False
            items.append(row)
        else:
            items.append(_teaser(row))

    return {"items": items, "count": len(items)}
