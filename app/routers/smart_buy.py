# app/routers/smart_buy.py
import json
from typing import Any, Dict, List, Optional, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from pydantic import BaseModel

router = APIRouter(tags=["Smart Buy"])

# -----------------------
# Helpers / Dependencies
# -----------------------

def _get_current_user(request: Request) -> str:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_id

async def _db(request: Request):
    """Yield a connection from the main pool attached in main.lifespan()."""
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(500, "Database pool not initialized")
    async with pool.acquire() as conn:
        yield conn


# -----------------------
# Models
# -----------------------

class SuggestionOut(BaseModel):
    id: int
    user_id: str
    card_id: str
    suggestion_type: str
    current_price: int
    target_price: int
    expected_profit: int
    risk_level: str
    confidence_score: int
    priority_score: int
    reasoning: str
    time_to_profit: Optional[str] = None
    platform: str
    market_state: str
    created_at: Optional[str] = None
    expires_at: Optional[str] = None

class FeedbackIn(BaseModel):
    card_id: str
    action: Literal["bought", "ignored", "watchlisted"]
    notes: Optional[str] = None
    actual_buy_price: Optional[int] = None
    actual_sell_price: Optional[int] = None
    actual_profit: Optional[int] = None

class PreferencesIn(BaseModel):
    default_budget: Optional[int] = 100000
    risk_tolerance: Optional[Literal["low", "moderate", "high"]] = "moderate"
    preferred_time_horizon: Optional[Literal["short", "medium", "long"]] = "short"
    preferred_categories: Optional[List[str]] = []
    excluded_positions: Optional[List[str]] = []
    preferred_leagues: Optional[List[str]] = []
    preferred_nations: Optional[List[str]] = []
    min_rating: Optional[int] = 75
    max_rating: Optional[int] = 95
    min_profit: Optional[int] = 1000
    notifications_enabled: Optional[bool] = True


# -----------------------
# Suggestions
# -----------------------

@router.get("/smart-buy/suggestions", response_model=Dict[str, Any])
async def list_suggestions(
    request: Request,
    user_id: str = Depends(_get_current_user),
    conn = Depends(_db),
    platform: Optional[Literal["ps", "xbox", "pc"]] = Query(None),
    limit: int = Query(20, ge=1, le=100),
):
    """
    Return latest Smart Buy suggestions for the current user.
    If `platform` is provided, filter on it.
    """
    if platform:
        rows = await conn.fetch(
            """
            SELECT *
            FROM smart_buy_suggestions
            WHERE user_id = $1 AND platform = $2
            ORDER BY created_at DESC
            LIMIT $3
            """,
            user_id, platform, limit
        )
    else:
        rows = await conn.fetch(
            """
            SELECT *
            FROM smart_buy_suggestions
            WHERE user_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            user_id, limit
        )

    items = [dict(r) for r in rows]
    return {"items": items, "count": len(items)}


# -----------------------
# Feedback
# -----------------------

@router.post("/smart-buy/feedback", response_model=Dict[str, Any])
async def create_feedback(
    payload: FeedbackIn,
    request: Request,
    user_id: str = Depends(_get_current_user),
    conn = Depends(_db),
):
    """
    Store user feedback about a suggestion / action taken.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO smart_buy_feedback (
            user_id, card_id, action, notes, actual_buy_price, actual_sell_price, actual_profit, timestamp
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7, NOW())
        RETURNING id, user_id, card_id, action, notes, actual_buy_price, actual_sell_price, actual_profit, timestamp
        """,
        user_id,
        payload.card_id,
        payload.action,
        payload.notes,
        payload.actual_buy_price,
        payload.actual_sell_price,
        payload.actual_profit,
    )
    return {"ok": True, "feedback": dict(row)}


# -----------------------
# Preferences
# -----------------------

@router.get("/smart-buy/preferences", response_model=Dict[str, Any])
async def get_preferences(
    request: Request,
    user_id: str = Depends(_get_current_user),
    conn = Depends(_db),
):
    row = await conn.fetchrow(
        """
        SELECT user_id, default_budget, risk_tolerance, preferred_time_horizon, preferred_categories, excluded_positions,
               preferred_leagues, preferred_nations, min_rating, max_rating, min_profit, notifications_enabled,
               created_at, updated_at
        FROM smart_buy_preferences
        WHERE user_id = $1
        """,
        user_id
    )
    if not row:
        # Return sensible defaults (match table defaults from main.py)
        return {
            "user_id": user_id,
            "default_budget": 100000,
            "risk_tolerance": "moderate",
            "preferred_time_horizon": "short",
            "preferred_categories": [],
            "excluded_positions": [],
            "preferred_leagues": [],
            "preferred_nations": [],
            "min_rating": 75,
            "max_rating": 95,
            "min_profit": 1000,
            "notifications_enabled": True,
        }
    out = dict(row)
    # Ensure JSONB fields are native lists
    for k in ("preferred_categories", "excluded_positions", "preferred_leagues", "preferred_nations"):
        v = out.get(k)
        if isinstance(v, str):
            try:
                out[k] = json.loads(v)
            except Exception:
                pass
    return out


@router.post("/smart-buy/preferences", response_model=Dict[str, Any])
async def upsert_preferences(
    payload: PreferencesIn,
    request: Request,
    user_id: str = Depends(_get_current_user),
    conn = Depends(_db),
):
    """
    Upsert user preferences.
    """
    await conn.execute(
        """
        INSERT INTO smart_buy_preferences (
            user_id, default_budget, risk_tolerance, preferred_time_horizon,
            preferred_categories, excluded_positions, preferred_leagues, preferred_nations,
            min_rating, max_rating, min_profit, notifications_enabled, created_at, updated_at
        )
        VALUES (
            $1,$2,$3,$4,$5::jsonb,$6::jsonb,$7::jsonb,$8::jsonb,$9,$10,$11,$12, NOW(), NOW()
        )
        ON CONFLICT (user_id) DO UPDATE SET
            default_budget = EXCLUDED.default_budget,
            risk_tolerance = EXCLUDED.risk_tolerance,
            preferred_time_horizon = EXCLUDED.preferred_time_horizon,
            preferred_categories = EXCLUDED.preferred_categories,
            excluded_positions = EXCLUDED.excluded_positions,
            preferred_leagues = EXCLUDED.preferred_leagues,
            preferred_nations = EXCLUDED.preferred_nations,
            min_rating = EXCLUDED.min_rating,
            max_rating = EXCLUDED.max_rating,
            min_profit = EXCLUDED.min_profit,
            notifications_enabled = EXCLUDED.notifications_enabled,
            updated_at = NOW()
        """,
        user_id,
        payload.default_budget,
        payload.risk_tolerance,
        payload.preferred_time_horizon,
        json.dumps(payload.preferred_categories or []),
        json.dumps(payload.excluded_positions or []),
        json.dumps(payload.preferred_leagues or []),
        json.dumps(payload.preferred_nations or []),
        payload.min_rating,
        payload.max_rating,
        payload.min_profit,
        payload.notifications_enabled,
    )
    return {"message": "Preferences saved"}


# -----------------------
# Market state / cache
# -----------------------

@router.get("/smart-buy/market/state", response_model=Dict[str, Any])
async def latest_market_state(
    request: Request,
    conn = Depends(_db),
    platform: Literal["ps", "xbox", "pc"] = Query("ps"),
):
    row = await conn.fetchrow(
        """
        SELECT platform, state, confidence_score, detected_at, indicators
        FROM market_states
        WHERE platform = $1
        ORDER BY detected_at DESC
        LIMIT 1
        """,
        platform
    )
    if not row:
        return {"platform": platform, "state": None, "confidence_score": None, "detected_at": None, "indicators": None}
    out = dict(row)
    return out


@router.get("/smart-buy/market/cache", response_model=Dict[str, Any])
async def smart_buy_market_cache(
    request: Request,
    conn = Depends(_db),
):
    """
    Return the latest precomputed Smart Buy market cache blob (if used).
    """
    row = await conn.fetchrow(
        """
        SELECT payload, updated_at
        FROM smart_buy_market_cache
        WHERE id = 1
        """
    )
    if not row:
        return {"payload": None, "updated_at": None}
    return {"payload": row["payload"], "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None}