from typing import Any, Dict, List, Optional, Literal
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from pydantic import BaseModel

router = APIRouter(prefix="/api/smart-buy", tags=["smart-buy"])

# --------------------------
# Models (payloads)
# --------------------------
class FeedbackCreate(BaseModel):
    card_id: str
    action: Literal["bought", "ignored", "watchlisted"]
    notes: Optional[str] = None
    actual_buy_price: Optional[int] = None
    actual_sell_price: Optional[int] = None

class PreferencesUpsert(BaseModel):
    default_budget: Optional[int] = 120_000
    risk_tolerance: Optional[Literal["low","moderate","high"]] = "moderate"
    preferred_time_horizon: Optional[Literal["short","swing","long"]] = "short"
    preferred_categories: Optional[List[str]] = []
    excluded_positions: Optional[List[str]] = []
    preferred_leagues: Optional[List[str]] = []
    preferred_nations: Optional[List[str]] = []
    min_rating: Optional[int] = 78
    max_rating: Optional[int] = 95
    min_profit: Optional[int] = 1000
    notifications_enabled: Optional[bool] = True

# --------------------------
# Helpers
# --------------------------
def _risk_to_where(risk: Optional[str]) -> str:
    if not risk:
        return "TRUE"
    # normalize
    r = risk.lower()
    if r in ("low","moderate","high"):
        return "LOWER(s.risk_level) = $risk"
    return "TRUE"

def _horizon_to_hours(h: Optional[str]) -> int:
    if not h:
        return 72  # default 3 days
    h = h.lower()
    if h == "short":
        return 24
    if h in ("swing", "mid"):
        return 72
    if h == "long":
        return 168
    return 72

def _platform_norm(p: Optional[str]) -> str:
    p = (p or "ps").lower()
    if p in ("ps","playstation","console"):
        return "ps"
    if p in ("xbox","xb"):
        return "xbox"
    if p in ("pc","origin"):
        return "pc"
    return "ps"

# --------------------------
# GET /suggestions
# --------------------------
@router.get("/suggestions")
async def list_suggestions(
    request: Request,
    # filters with sensible defaults
    budget: Optional[int] = Query(120_000, ge=0, description="Max current_price"),
    platform: Optional[str] = Query("ps"),
    risk_tolerance: Optional[str] = Query("moderate"),
    time_horizon: Optional[str] = Query("short"),
    min_rating: Optional[int] = Query(0, ge=0, le=99),
    max_rating: Optional[int] = Query(99, ge=0, le=99),
    league: Optional[str] = Query(None, description="free text contains"),
    nation: Optional[str] = Query(None, description="free text contains"),
    position: Optional[str] = Query(None, description="exact match on primary or any alt"),
    q: Optional[str] = Query(None, description="free-text search on player name"),
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """
    Returns enriched Smart Buy suggestions joined with fut_players.
    Data source: **Players DB** via request.app.state.player_pool
    """
    player_pool = getattr(request.app.state, "player_pool", None)
    if player_pool is None:
        raise HTTPException(500, "Player DB not configured")

    plat = _platform_norm(platform)
    horizon_hours = _horizon_to_hours(time_horizon)

    # dynamic WHEREs
    where_clauses = [
        "s.platform = $1",
        "s.current_price <= $2",
        "s.created_at >= NOW() - ($3 || ' hours')::interval",
    ]

    params: List[Any] = [plat, budget, horizon_hours]
    named_map: Dict[str, Any] = {}

    # risk filter
    if risk_tolerance:
        where_clauses.append("LOWER(s.risk_level) = $risk")
        named_map["risk"] = risk_tolerance.lower()

    # rating guard (from fut_players)
    if min_rating is not None:
        where_clauses.append("(fp.rating IS NULL OR fp.rating >= $min_rating)")
        named_map["min_rating"] = min_rating
    if max_rating is not None:
        where_clauses.append("(fp.rating IS NULL OR fp.rating <= $max_rating)")
        named_map["max_rating"] = max_rating

    # league/nation text filters
    if league:
        where_clauses.append("fp.league ILIKE $league")
        named_map["league"] = f"%{league}%"
    if nation:
        where_clauses.append("fp.nation ILIKE $nation")
        named_map["nation"] = f"%{nation}%"

    # position filter (checks primary OR any altposition token)
    if position:
        where_clauses.append("""
        (
          UPPER(fp.position) = $position
          OR (
            COALESCE(fp.altposition, '') <> ''
            AND EXISTS (
              SELECT 1
              FROM regexp_split_to_table(fp.altposition, '[,;/|\\s]+') ap
              WHERE UPPER(TRIM(ap)) = $position
            )
          )
        )
        """)
        named_map["position"] = position.upper()

    # free text on name
    if q:
        where_clauses.append("fp.name ILIKE $name_q")
        named_map["name_q"] = f"%{q}%"

    where_sql = " AND ".join(where_clauses) if where_clauses else "TRUE"

    # NOTE:
    # - s.card_id is TEXT (our seed created it that way)
    # - fut_players.card_id is also TEXT in your DB
    # If your fut_players.card_id were INT, you would use fp.card_id::text = s.card_id
    sql = f"""
    WITH filtered AS (
      SELECT
        s.card_id,
        s.platform,
        s.suggestion_type,
        s.current_price,
        s.target_price,
        s.expected_profit,
        s.risk_level,
        s.confidence_score,
        s.priority_score,
        s.reasoning,
        s.time_to_profit,
        s.market_state,
        s.created_at
      FROM smart_buy_suggestions s
      WHERE {where_sql}
      ORDER BY s.priority_score DESC, s.confidence_score DESC, s.created_at DESC
      LIMIT $4 OFFSET $5
    )
    SELECT
      f.card_id,
      f.platform,
      f.suggestion_type,
      f.current_price,
      f.target_price,
      f.expected_profit,
      f.risk_level,
      f.confidence_score,
      f.priority_score,
      f.reasoning,
      f.time_to_profit,
      f.market_state,
      f.created_at,
      fp.name,
      fp.rating,
      fp.version,
      fp.image_url,
      fp.club,
      fp.league,
      fp.nation,
      fp.position,
      fp.altposition
    FROM filtered f
    LEFT JOIN fut_players fp
      ON fp.card_id = f.card_id
    """

    # count query (for pagination)
    count_sql = f"""
      SELECT COUNT(*) AS c
      FROM smart_buy_suggestions s
      LEFT JOIN fut_players fp ON fp.card_id = s.card_id
      WHERE {where_sql}
    """

    # build final params in order: $1.. positional + named
    # positional
    params_all: List[Any] = [plat, budget, horizon_hours, limit, offset]
    # named (psycopg-style with asyncpg named mapping)
    # asyncpg doesn't support $name directly; we replace them by positions:
    # We'll compile a mapping now:
    replace_order: List[str] = []
    for k in ["risk","min_rating","max_rating","league","nation","position","name_q"]:
        if k in named_map:
            replace_order.append(k)

    def _replace_named(sql_text: str, start_index: int) -> (str, List[Any]):
        out_params: List[Any] = []
        i = start_index
        for k in replace_order:
            sql_text = sql_text.replace(f"${k}", f"${i}")
            out_params.append(named_map[k])
            i += 1
        return sql_text, out_params

    sql_final, named_params = _replace_named(sql, len(params_all) + 1)
    params_all_sql = params_all + named_params

    count_sql_final, count_named_params = _replace_named(count_sql, 3 + 1)  # first 3 are (plat, budget, horizon)
    count_params_all = [plat, budget, horizon_hours] + count_named_params

    async with player_pool.acquire() as conn:
        rows = await conn.fetch(sql_final, *params_all_sql)
        cnt = await conn.fetchval(count_sql_final, *count_params_all)

    items = [dict(r) for r in rows]
    return {"items": items, "count": int(cnt)}

# --------------------------
# POST /feedback
# --------------------------
@router.post("/feedback")
async def submit_feedback(
    request: Request,
    payload: FeedbackCreate,
):
    player_pool = getattr(request.app.state, "player_pool", None)
    if player_pool is None:
        raise HTTPException(500, "Player DB not configured")

    # derive user_id from session if you have it, else use 'global'
    user_id = request.session.get("user_id") or "global"

    async with player_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO smart_buy_feedback
              (user_id, card_id, action, notes, actual_buy_price, actual_sell_price, actual_profit, timestamp)
            VALUES ($1,$2,$3,$4,$5,$6,
              CASE
                WHEN $5 IS NOT NULL AND $6 IS NOT NULL THEN ($6 - $5)
                ELSE NULL
              END,
              NOW())
            """,
            user_id,
            payload.card_id,
            payload.action,
            payload.notes,
            payload.actual_buy_price,
            payload.actual_sell_price,
        )
    return {"ok": True}

# --------------------------
# GET/POST preferences
# --------------------------
@router.get("/preferences")
async def get_preferences(request: Request):
    player_pool = getattr(request.app.state, "player_pool", None)
    if player_pool is None:
        raise HTTPException(500, "Player DB not configured")

    user_id = request.session.get("user_id") or "global"
    async with player_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM smart_buy_preferences WHERE user_id=$1",
            user_id
        )
    return dict(row) if row else {}

@router.post("/preferences")
async def upsert_preferences(request: Request, prefs: PreferencesUpsert):
    player_pool = getattr(request.app.state, "player_pool", None)
    if player_pool is None:
        raise HTTPException(500, "Player DB not configured")

    user_id = request.session.get("user_id") or "global"
    async with player_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO smart_buy_preferences
              (user_id, default_budget, risk_tolerance, preferred_time_horizon,
               preferred_categories, excluded_positions, preferred_leagues, preferred_nations,
               min_rating, max_rating, min_profit, notifications_enabled, created_at, updated_at)
            VALUES
              ($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7::jsonb,$8::jsonb,$9,$10,$11,$12,NOW(),NOW())
            ON CONFLICT (user_id)
            DO UPDATE SET
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
            prefs.default_budget,
            prefs.risk_tolerance,
            prefs.preferred_time_horizon,
            (prefs.preferred_categories or []),
            (prefs.excluded_positions or []),
            (prefs.preferred_leagues or []),
            (prefs.preferred_nations or []),
            prefs.min_rating,
            prefs.max_rating,
            prefs.min_profit,
            prefs.notifications_enabled,
        )
    return {"ok": True}

# --------------------------
# GET /market-intelligence
# --------------------------
@router.get("/market-intelligence")
async def market_intel(request: Request, platform: str = Query("ps")):
    player_pool = getattr(request.app.state, "player_pool", None)
    if player_pool is None:
        raise HTTPException(500, "Player DB not configured")

    plat = _platform_norm(platform)
    async with player_pool.acquire() as conn:
        state = await conn.fetchrow(
            """
            SELECT state, confidence_score, detected_at, indicators
            FROM market_states
            WHERE platform = $1
            ORDER BY detected_at DESC
            LIMIT 1
            """,
            plat,
        )
        cache = await conn.fetchrow(
            """
            SELECT payload, updated_at
            FROM smart_buy_market_cache
            WHERE id = 1
            """
        )

    return {
        "platform": plat,
        "state": dict(state) if state else None,
        "cache": dict(cache) if cache else None,
    }
