# app/routers/smart_buy.py
from typing import Optional, List, Dict, Any, Literal
from datetime import datetime, timezone

import asyncpg
from fastapi import APIRouter, Request, HTTPException, Query
from pydantic import BaseModel

router = APIRouter(prefix="/smart-buy", tags=["Smart Buy"])

# ----------------- Helpers -----------------
def _require_user(request: Request) -> str:
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return uid


# ----------------- Models -----------------
class PreferencesUpdate(BaseModel):
    default_budget: Optional[int] = None
    risk_tolerance: Optional[Literal["conservative", "moderate", "aggressive"]] = None
    preferred_time_horizon: Optional[Literal["quick_flip", "short", "long_term"]] = None
    preferred_categories: Optional[List[str]] = None
    excluded_positions: Optional[List[str]] = None
    preferred_leagues: Optional[List[str]] = None
    preferred_nations: Optional[List[str]] = None
    min_rating: Optional[int] = None
    max_rating: Optional[int] = None
    min_profit: Optional[int] = None
    notifications_enabled: Optional[bool] = None


class FeedbackCreate(BaseModel):
    card_id: str
    action: Literal["bought", "ignored", "watchlisted"]
    notes: Optional[str] = None
    actual_buy_price: Optional[int] = None
    actual_sell_price: Optional[int] = None
    actual_profit: Optional[int] = None
    suggestion_id: Optional[int] = None


# ----------------- Health -----------------
@router.get("/health")
async def health(request: Request):
    pool: asyncpg.Pool = request.app.state.pool
    try:
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


# ----------------- Preferences -----------------
@router.get("/preferences/me")
async def get_my_preferences(request: Request):
    uid = _require_user(request)
    pool: asyncpg.Pool = request.app.state.pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT user_id, default_budget, risk_tolerance, preferred_time_horizon,
                   preferred_categories, excluded_positions, preferred_leagues, preferred_nations,
                   min_rating, max_rating, min_profit, notifications_enabled, created_at, updated_at
            FROM smart_buy_preferences
            WHERE user_id = $1
            """,
            uid,
        )
    if not row:
        return {
            "user_id": uid,
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
            "created_at": None,
            "updated_at": None,
        }
    d = dict(row)
    for key in ("preferred_categories", "excluded_positions", "preferred_leagues", "preferred_nations"):
        d[key] = d.get(key) or []
    return d


@router.put("/preferences/me")
async def update_my_preferences(payload: PreferencesUpdate, request: Request):
    uid = _require_user(request)
    pool: asyncpg.Pool = request.app.state.pool
    async with pool.acquire() as conn:
        cur = await conn.fetchrow(
            """
            SELECT default_budget, risk_tolerance, preferred_time_horizon,
                   preferred_categories, excluded_positions, preferred_leagues, preferred_nations,
                   min_rating, max_rating, min_profit, notifications_enabled
            FROM smart_buy_preferences
            WHERE user_id = $1
            """,
            uid,
        )
        base: Dict[str, Any] = {
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
        if cur:
            base.update({k: cur[k] for k in base.keys()})
        changes = {k: v for k, v in payload.model_dump(exclude_none=True).items()}
        base.update(changes)

        await conn.execute(
            """
            INSERT INTO smart_buy_preferences (
                user_id, default_budget, risk_tolerance, preferred_time_horizon,
                preferred_categories, excluded_positions, preferred_leagues, preferred_nations,
                min_rating, max_rating, min_profit, notifications_enabled, created_at, updated_at
            ) VALUES (
                $1,$2,$3,$4,$5::jsonb,$6::jsonb,$7::jsonb,$8::jsonb,$9,$10,$11,$12,NOW(),NOW()
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
            uid,
            base["default_budget"],
            base["risk_tolerance"],
            base["preferred_time_horizon"],
            base["preferred_categories"],
            base["excluded_positions"],
            base["preferred_leagues"],
            base["preferred_nations"],
            base["min_rating"],
            base["max_rating"],
            base["min_profit"],
            base["notifications_enabled"],
        )
    return {"ok": True, "preferences": base}


# ----------------- Suggestions -----------------
@router.get("/suggestions")
async def list_suggestions(
    request: Request,
    platform: Optional[Literal["ps", "xbox", "pc"]] = Query(None),
    suggestion_type: Optional[str] = Query(None),
    risk_level: Optional[Literal["low", "medium", "high"]] = Query(None),
    min_confidence: Optional[int] = Query(None, ge=0, le=100),
    only_active: bool = Query(True),
    limit: int = Query(50, ge=1, le=200),
    mine: bool = Query(False),
):
    """Return cached Smart Buy suggestions filtered by query."""
    uid = request.session.get("user_id") if mine else None
    pool: asyncpg.Pool = request.app.state.pool

    where = []
    params: List[Any] = []
    if platform:
        where.append(f"platform = ${len(params)+1}"); params.append(platform)
    if suggestion_type:
        where.append(f"suggestion_type = ${len(params)+1}"); params.append(suggestion_type)
    if risk_level:
        where.append(f"risk_level = ${len(params)+1}"); params.append(risk_level)
    if min_confidence is not None:
        where.append(f"confidence_score >= ${len(params)+1}"); params.append(int(min_confidence))
    if only_active:
        where.append("(expires_at IS NULL OR expires_at > NOW())")
    if uid:
        where.append(f"user_id = ${len(params)+1}"); params.append(uid)

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    sql = f"""
        SELECT id, user_id, card_id, suggestion_type, current_price, target_price, expected_profit,
               risk_level, confidence_score, priority_score, reasoning, time_to_profit, platform,
               market_state, created_at, expires_at
        FROM smart_buy_suggestions
        {clause}
        ORDER BY priority_score DESC, confidence_score DESC, created_at DESC
        LIMIT ${len(params)+1}
    """
    params.append(limit)

    pool: asyncpg.Pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return {"items": [dict(r) for r in rows]}


@router.post("/suggestions/refresh")
async def refresh_suggestions_stub():
    """
    UI compatibility stub. In the future you can trigger a background refresh here.
    For now, always returns ok=true so the frontend doesn't show an error.
    """
    return {"ok": True}


# ----------------- Feedback -----------------
@router.post("/feedback")
async def create_feedback(payload: FeedbackCreate, request: Request):
    uid = _require_user(request)
    pool: asyncpg.Pool = request.app.state.pool
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO smart_buy_feedback (
                user_id, card_id, action, notes, actual_buy_price, actual_sell_price,
                actual_profit, timestamp, suggestion_id
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,NOW(),$8)
            """,
            uid,
            payload.card_id,
            payload.action,
            payload.notes,
            payload.actual_buy_price,
            payload.actual_sell_price,
            payload.actual_profit,
            payload.suggestion_id,
        )
    return {"ok": True}


# ----------------- Market state & events -----------------
@router.get("/market/state")
async def latest_market_state(
    request: Request,
    platform: Optional[Literal["ps", "xbox", "pc"]] = Query(None)
):
    pool: asyncpg.Pool = request.app.state.pool
    async with pool.acquire() as conn:
        if platform:
            row = await conn.fetchrow(
                """
                SELECT id, platform, state, confidence_score, detected_at, indicators
                FROM market_states
                WHERE platform = $1
                ORDER BY detected_at DESC
                LIMIT 1
                """,
                platform,
            )
        else:
            row = await conn.fetchrow(
                """
                SELECT id, platform, state, confidence_score, detected_at, indicators
                FROM market_states
                ORDER BY detected_at DESC
                LIMIT 1
                """
            )
    if not row:
        return {"state": None}
    return dict(row)


@router.get("/events/upcoming")
async def upcoming_events(request: Request, limit: int = Query(10, ge=1, le=100)):
    pool: asyncpg.Pool = request.app.state.pool
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(
                """
                SELECT id, name, event_type, expected_date, actual_date, impact_level,
                       affected_categories, description, created_at
                FROM market_events
                WHERE expected_date IS NULL OR expected_date >= NOW()
                ORDER BY COALESCE(expected_date, NOW()) ASC
                LIMIT $1
                """,
                limit,
            )
        except Exception:
            rows = []
    return {"items": [dict(r) for r in rows]}


# ----------------- Combined intelligence (UI expects this) -----------------
@router.get("/market-intelligence")
async def market_intelligence(
    request: Request,
    platform: Optional[Literal["ps", "xbox", "pc"]] = Query(None),
    limit: int = Query(5, ge=1, le=20),
):
    """
    Compatibility endpoint expected by the UI.
    Bundles latest market state + upcoming events.
    """
    pool: asyncpg.Pool = request.app.state.pool
    async with pool.acquire() as conn:
        # latest market state
        if platform:
            state_row = await conn.fetchrow(
                """
                SELECT id, platform, state, confidence_score, detected_at, indicators
                FROM market_states
                WHERE platform = $1
                ORDER BY detected_at DESC
                LIMIT 1
                """,
                platform,
            )
        else:
            state_row = await conn.fetchrow(
                """
                SELECT id, platform, state, confidence_score, detected_at, indicators
                FROM market_states
                ORDER BY detected_at DESC
                LIMIT 1
                """
            )

        # upcoming events (best effort)
        try:
            events = await conn.fetch(
                """
                SELECT id, name, event_type, expected_date, actual_date, impact_level,
                       affected_categories, description, created_at
                FROM market_events
                WHERE expected_date IS NULL OR expected_date >= NOW()
                ORDER BY COALESCE(expected_date, NOW()) ASC
                LIMIT $1
                """,
                limit,
            )
        except Exception:
            events = []

    return {
        "state": dict(state_row) if state_row else None,
        "events": [dict(e) for e in events],
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }


# ----------------- Maintenance -----------------
@router.post("/maintenance/cleanup-expired")
async def cleanup_expired(request: Request):
    pool: asyncpg.Pool = request.app.state.pool
    async with pool.acquire() as conn:
        try:
            await conn.execute("SELECT cleanup_expired_suggestions()")
        except Exception:
            # Fallback if the function doesn't exist
            await conn.execute(
                "DELETE FROM smart_buy_suggestions WHERE expires_at IS NOT NULL AND expires_at < NOW() - INTERVAL '1 day'"
            )
    return {"ok": True, "ran": "cleanup_expired_suggestions"}