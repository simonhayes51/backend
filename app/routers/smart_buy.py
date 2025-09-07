# app/routers/smart_buy.py
import math
import time
import random
import logging
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

router = APIRouter(prefix="/smart-buy", tags=["smart-buy"])

# -------------------------------
# Helpers to grab the player DB
# -------------------------------
async def get_player_conn(request: Request):
    pool = getattr(request.app.state, "player_pool", None)
    if pool is None:
        # fall back to primary pool if not split
        pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(500, "Player database pool not available")
    async with pool.acquire() as conn:
        yield conn

# -------------------------------
# Query param normalization
# -------------------------------
RiskTol = Literal["conservative", "moderate", "aggressive", "low", "medium", "high"]
TimeHorizon = Literal["short", "medium", "long"]
Platform = Literal["ps", "xbox", "pc", "console"]

def _norm_platform(p: str) -> str:
    p = (p or "").lower()
    if p in ("ps", "playstation", "console"): return "ps"
    if p in ("xbox", "xb"): return "xbox"
    if p in ("pc", "origin"): return "pc"
    return "ps"

def _norm_risk(r: str) -> str:
    r = (r or "").lower()
    # Accept both the words shown in UI and synonyms
    if r in ("low", "conservative"): return "conservative"
    if r in ("high", "aggressive"): return "aggressive"
    return "moderate"

def _norm_horizon(h: str) -> str:
    h = (h or "").lower()
    if "short" in h: return "short"
    if "long" in h: return "long"
    return "medium"

# -------------------------------
# Pydantic response models
# -------------------------------
class Suggestion(BaseModel):
    card_id: str
    platform: str
    suggestion_type: str
    current_price: Optional[int]
    target_price: Optional[int]
    expected_profit: Optional[int]
    risk_level: str
    confidence_score: int
    priority_score: int
    reasoning: str
    time_to_profit: Optional[str] = None
    market_state: str = "normal"
    created_at: Optional[str] = None

    # enrichment fields (nullable if not in fut_players)
    name: Optional[str] = None
    rating: Optional[int] = None
    version: Optional[str] = None
    image_url: Optional[str] = None
    club: Optional[str] = None
    league: Optional[str] = None
    nation: Optional[str] = None
    position: Optional[str] = None
    altposition: Optional[str] = None


class SuggestionsResponse(BaseModel):
    items: List[Suggestion]
    count: int


# -------------------------------
# Lightweight “market intelligence”
# -------------------------------
@router.get("/market-intelligence")
async def market_intelligence(
    platform: Platform = Query("ps"),
):
    # You can later wire this to a real table (market_states). For now we heuristically return normal.
    plat = _norm_platform(platform)
    return {
        "platform": plat,
        "state": "normal",
        "confidence": 0.0,  # UI can treat 0 as “unknown”
        "updated_at": int(time.time()),
        "notes": "Heuristic default (no signal yet)",
    }


# -------------------------------
# Suggestions — DB-first, with safe TEXT->INT casting
# -------------------------------
@router.get("/suggestions", response_model=SuggestionsResponse)
async def suggestions(
    request: Request,
    budget: int = Query(..., ge=1000, le=10_000_000),
    risk_tolerance: str = Query("moderate"),
    time_horizon: str = Query("short"),
    platform: str = Query("ps"),
    limit: int = Query(20, ge=1, le=50),
    pconn = Depends(get_player_conn),
):
    """
    Produces suggestions by scanning fut_players (price is TEXT) and computing simple
    buy targets. If no rows match, we produce a couple of synthetic examples so the UI
    never shows a hard error.
    """
    plat = _norm_platform(platform)
    risk = _norm_risk(risk_tolerance)
    horizon = _norm_horizon(time_horizon)

    # risk & horizon knobs
    # - target multiplier influences how much upside we ask for
    # - confidence seeds a base score
    if risk == "conservative":
        target_mult = 1.05  # ask for small upside
        base_conf = 60
    elif risk == "aggressive":
        target_mult = 1.12
        base_conf = 72
    else:  # moderate
        target_mult = 1.08
        base_conf = 66

    if horizon == "short":
        ttp = "6-24h"
        pri_boost = 8
    elif horizon == "long":
        ttp = "2-7d"
        pri_boost = 0
    else:
        ttp = "1-3d"
        pri_boost = 4

    # 1) Try to select real candidates from fut_players
    # price is TEXT — keep only all-digits and cast, also ignore zero
    # Add some simple desirability ordering (rating DESC, then price ASC to bias liquidity)
    sql = f"""
        WITH cand AS (
            SELECT
                card_id,
                name,
                rating,
                version,
                image_url,
                club,
                league,
                nation,
                position,
                altposition,
                price::int AS px
            FROM fut_players
            WHERE price ~ '^[0-9]+$'
              AND price::int BETWEEN 1 AND $1
            ORDER BY rating DESC NULLS LAST, price::int ASC, name ASC
            LIMIT $2
        )
        SELECT * FROM cand
    """

    try:
        rows = await pconn.fetch(sql, budget, max(limit * 3, 30))  # over-pick then downselect
    except Exception as e:
        logging.warning("smart-buy query failed: %s", e)
        rows = []

    items: List[Suggestion] = []

    # 2) Build suggestions from DB rows
    for r in rows:
        px = int(r["px"]) if r["px"] is not None else None
        if not px or px <= 0:
            continue
        # cheap, liquid “flip” style for higher prices; “buy-dip” for cheaper
        sug_type = "buy-flip" if px > (budget * 0.6) else "buy-dip"
        tgt = int(math.floor(px * target_mult))
        exp = max(0, tgt - px - int(round(px * 0.05)))  # rough after-tax improvement
        conf = min(95, max(50, base_conf + random.randint(-5, 6)))
        prio = min(99, max(20, conf + pri_boost + (r["rating"] or 0) // 2))

        items.append(Suggestion(
            card_id=str(r["card_id"]),
            platform=plat,
            suggestion_type=sug_type,
            current_price=px,
            target_price=tgt,
            expected_profit=exp,
            risk_level=risk,
            confidence_score=conf,
            priority_score=prio,
            reasoning="Screened by rating & price; upside based on risk/horizon settings",
            time_to_profit=ttp,
            market_state="normal",
            created_at=None,
            name=r.get("name"),
            rating=r.get("rating"),
            version=r.get("version"),
            image_url=r.get("image_url"),
            club=r.get("club"),
            league=r.get("league"),
            nation=r.get("nation"),
            position=r.get("position"),
            altposition=r.get("altposition"),
        ))
        if len(items) >= limit:
            break

    # 3) If we found nothing usable, emit a couple of safe synthetic examples so UI shows something
    if not items:
        synth: List[Dict[str, Any]] = [
            {
                "card_id": "123456",
                "name": None, "rating": None, "version": None, "image_url": None,
                "club": None, "league": None, "nation": None, "position": None, "altposition": None,
                "platform": plat, "suggestion_type": "buy-dip",
                "current_price": 18000, "target_price": 21000, "expected_profit": 2500,
                "risk_level": risk, "confidence_score": 72, "priority_score": 78,
                "reasoning": "Synthetic example (no DB matches under your budget yet)",
                "time_to_profit": ttp, "market_state": "normal",
            },
            {
                "card_id": "654321",
                "name": None, "rating": None, "version": None, "image_url": None,
                "club": None, "league": None, "nation": None, "position": None, "altposition": None,
                "platform": plat, "suggestion_type": "buy-flip",
                "current_price": 95000, "target_price": 105000, "expected_profit": 7000,
                "risk_level": risk, "confidence_score": 68, "priority_score": 74,
                "reasoning": "Synthetic example (no DB matches under your budget yet)",
                "time_to_profit": ttp, "market_state": "normal",
            },
        ]
        items = [Suggestion(**s) for s in synth if s["current_price"] <= budget][:max(1, min(2, limit))]

    return SuggestionsResponse(items=items, count=len(items))


# -------------------------------
# Optional simple stats block
# -------------------------------
@router.get("/stats")
async def smart_buy_stats(pconn=Depends(get_player_conn)):
    """
    Basic availability stats about fut_players price data (TEXT).
    Helps verify the DB side looks sane.
    """
    try:
        total = await pconn.fetchval("SELECT COUNT(*) FROM fut_players")
        numeric = await pconn.fetchval("SELECT COUNT(*) FROM fut_players WHERE price ~ '^[0-9]+$'")
        nonzero = await pconn.fetchval("SELECT COUNT(*) FROM fut_players WHERE price ~ '^[0-9]+$' AND price::int > 0")
    except Exception as e:
        raise HTTPException(500, f"stats error: {e}")

    return {
        "players_total": int(total or 0),
        "with_numeric_price": int(numeric or 0),
        "with_nonzero_price": int(nonzero or 0),
    }


# -------------------------------
# Feedback endpoint (optional)
# -------------------------------
class FeedbackIn(BaseModel):
    card_id: str
    action: Literal["taken", "skipped"]
    notes: Optional[str] = None
    actual_buy_price: Optional[int] = None
    actual_sell_price: Optional[int] = None

@router.post("/feedback")
async def record_feedback(_payload: FeedbackIn):
    # No-op storage here to keep things simple in the shared player DB setup.
    # If you want to persist, point this to your primary pool’s smart_buy_feedback table.
    return {"ok": True}
