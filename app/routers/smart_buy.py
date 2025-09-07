# app/routers/smart_buy.py

from __future__ import annotations
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

router = APIRouter()

# --------- Models ---------
class SmartBuyPreferencesIn(BaseModel):
    default_budget: Optional[int] = 100_000
    risk_tolerance: Literal["low", "moderate", "high"] = "moderate"
    preferred_time_horizon: Literal["very_short", "short", "medium", "long"] = "short"
    preferred_categories: Optional[List[str]] = []
    excluded_positions: Optional[List[str]] = []
    preferred_leagues: Optional[List[str]] = []
    preferred_nations: Optional[List[str]] = []
    min_rating: Optional[int] = 75
    max_rating: Optional[int] = 95
    min_profit: Optional[int] = 1000
    notifications_enabled: Optional[bool] = True

class SmartBuyFeedbackIn(BaseModel):
    card_id: str
    action: Literal["bought", "ignored", "watchlisted"]
    notes: Optional[str] = ""
    actual_buy_price: Optional[int] = None
    actual_sell_price: Optional[int] = None
    actual_profit: Optional[int] = None

# --------- Dependencies ---------
async def _db(request: Request):
    async with request.app.state.pool.acquire() as conn:
        yield conn

async def _player_db(request: Request):
    async with request.app.state.player_pool.acquire() as conn:
        yield conn

def _get_user_id(request: Request) -> Optional[str]:
    return request.session.get("user_id")

# --------- Helpers ---------
def _platform_norm(p: str) -> str:
    p = (p or "").lower()
    if p in ("ps", "playstation", "console"):
        return "ps"
    if p in ("xbox", "xb"):
        return "xbox"
    if p in ("pc", "origin"):
        return "pc"
    return "ps"

def _map_risk(label: Optional[str]) -> Optional[str]:
    s = (label or "").strip().lower()
    if s in ("low", "moderate", "high"):
        return s
    return None

def _map_horizons(label: Optional[str]) -> List[str]:
    s = (label or "").strip().lower()
    if s in ("very_short", "very-short", "0-6h"):
        return ["0-6h"]
    if s in ("short", "short_term", "short-term", "6-24h", "6-48h"):
        return ["6-24h", "6-48h"]
    if s in ("medium", "24-72h", "1-3d"):
        return ["24-72h", "1-3d"]
    if s in ("long", "3-7d", "1-2w"):
        return ["3-7d", "1-2w"]
    return []  # no filter

async def _enrich(player_conn, card_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    if not card_ids:
        return {}
    rows = await player_conn.fetch(
        """
        SELECT card_id, name, rating, version, image_url, club, league, nation, position, altposition
        FROM fut_players
        WHERE card_id = ANY($1::text[])
        """,
        card_ids,
    )
    return {
        str(r["card_id"]): {
            "name": r["name"],
            "rating": r["rating"],
            "version": r["version"],
            "image_url": r["image_url"],
            "club": r["club"],
            "league": r["league"],
            "nation": r["nation"],
            "position": r["position"],
            "altposition": r["altposition"],
        }
        for r in rows
    }

# --------- Suggestions ---------
@router.get("/smart-buy/suggestions")
async def smart_buy_suggestions(
    request: Request,
    conn=Depends(_db),
    player_conn=Depends(_player_db),

    # FE may send snake_case; accept both. The first set reads snake_case; the second reads camelCase aliases.
    budget: Optional[int] = Query(None),
    risk_tolerance: Optional[str] = Query(None, alias="riskTolerance"),
    time_horizon: Optional[str] = Query(None, alias="timeHorizon"),
    platform: str = Query("ps"),
    limit: int = Query(24, ge=1, le=100),
) -> Dict[str, Any]:
    """
    Returns filtered smart-buy suggestions.
    Falls back to user_id='global' when the user has no rows.
    On error: returns 200 with empty payload so FE doesn't show a red error panel.
    """
    try:
        uid = _get_user_id(request)
        plat = _platform_norm(platform)

        risk = _map_risk(risk_tolerance)
        horizons = _map_horizons(time_horizon)

        where = ["platform = $1"]
        params: List[Any] = [plat]

        if isinstance(budget, int) and budget > 0:
            where.append("current_price <= $%d" % (len(params) + 1))
            params.append(budget)

        if risk:
            where.append("risk_level = $%d" % (len(params) + 1))
            params.append(risk)

        if horizons:
            where.append("time_to_profit = ANY($%d)" % (len(params) + 1))
            params.append(horizons)

        # Try user-specific first (only if logged in)
        rows: List[Any] = []
        if uid:
            sql_user = f"""
                SELECT *
                  FROM smart_buy_suggestions
                 WHERE {' AND '.join(where)} AND user_id = $%d
              ORDER BY priority_score DESC, confidence_score DESC, expected_profit DESC, created_at DESC
                 LIMIT $%d
            """ % (len(params) + 1, len(params) + 2)
            rows = await conn.fetch(sql_user, *params, uid, limit)

        # Fallback: global pool
        if not rows:
            sql_global = f"""
                SELECT *
                  FROM smart_buy_suggestions
                 WHERE {' AND '.join(where)} AND user_id = 'global'
              ORDER BY priority_score DESC, confidence_score DESC, expected_profit DESC, created_at DESC
                 LIMIT $%d
            """ % (len(params) + 1)
            rows = await conn.fetch(sql_global, *params, limit)

        if not rows:
            return {"items": [], "count": 0}

        meta = await _enrich(player_conn, [str(r["card_id"]) for r in rows])

        items = []
        for r in rows:
            cid = str(r["card_id"])
            m = meta.get(cid, {})
            items.append(
                {
                    "card_id": cid,
                    "platform": r["platform"],
                    "suggestion_type": r["suggestion_type"],
                    "current_price": int(r["current_price"]) if r["current_price"] is not None else None,
                    "target_price": int(r["target_price"]) if r["target_price"] is not None else None,
                    "expected_profit": int(r["expected_profit"]) if r["expected_profit"] is not None else None,
                    "risk_level": r["risk_level"],
                    "confidence_score": int(r["confidence_score"]),
                    "priority_score": int(r["priority_score"]),
                    "reasoning": r["reasoning"],
                    "time_to_profit": r["time_to_profit"],
                    "market_state": r["market_state"],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                    # meta
                    "name": m.get("name"),
                    "rating": m.get("rating"),
                    "version": m.get("version"),
                    "image_url": m.get("image_url"),
                    "club": m.get("club"),
                    "league": m.get("league"),
                    "nation": m.get("nation"),
                    "position": m.get("position"),
                    "altposition": m.get("altposition"),
                }
            )

        return {"items": items[:limit], "count": len(items[:limit])}

    except Exception as e:
        # Keep FE happy: return 200 + empty payload with a hint
        return {"items": [], "count": 0, "note": "fallback-empty", "error": str(e)}

# --------- Market intelligence ---------
@router.get("/smart-buy/market-state")
async def latest_market_state(request: Request, conn=Depends(_db), platform: str = Query("ps")) -> Dict[str, Any]:
    plat = _platform_norm(platform)
    row = await conn.fetchrow(
        """
        SELECT platform, state, confidence_score, detected_at, indicators
          FROM market_states
         WHERE platform = $1
      ORDER BY detected_at DESC
         LIMIT 1
        """,
        plat,
    )
    if not row:
        return {"platform": plat, "state": "unknown", "confidence_score": 0, "indicators": {}}
    return {
        "platform": row["platform"],
        "state": row["state"],
        "confidence_score": int(row["confidence_score"]),
        "detected_at": row["detected_at"].isoformat() if row["detected_at"] else None,
        "indicators": row["indicators"] or {},
    }

@router.get("/smart-buy/market-cache")
async def smart_buy_market_cache(request: Request, conn=Depends(_db)) -> Dict[str, Any]:
    row = await conn.fetchrow("SELECT payload, updated_at FROM smart_buy_market_cache WHERE id = 1")
    if not row:
        return {"payload": {}, "updated_at": None}
    return {"payload": row["payload"] or {}, "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None}

# Alias the FE expects
@router.get("/smart-buy/market-intelligence")
async def market_intelligence_alias(request: Request, conn=Depends(_db)) -> Dict[str, Any]:
    state = await latest_market_state(request, conn=conn, platform="ps")
    cache = await smart_buy_market_cache(request, conn=conn)
    return {"state": state, "cache": cache}

# --------- Preferences ---------
@router.get("/smart-buy/preferences")
async def get_preferences(request: Request, conn=Depends(_db)) -> Dict[str, Any]:
    uid = _get_user_id(request)
    if not uid:
        return SmartBuyPreferencesIn().dict()

    row = await conn.fetchrow(
        """
        SELECT default_budget, risk_tolerance, preferred_time_horizon, preferred_categories,
               excluded_positions, preferred_leagues, preferred_nations, min_rating, max_rating,
               min_profit, notifications_enabled
          FROM smart_buy_preferences
         WHERE user_id = $1
        """,
        uid,
    )
    if not row:
        return SmartBuyPreferencesIn().dict()

    return {
        "default_budget": row["default_budget"],
        "risk_tolerance": row["risk_tolerance"],
        "preferred_time_horizon": row["preferred_time_horizon"],
        "preferred_categories": row["preferred_categories"] or [],
        "excluded_positions": row["excluded_positions"] or [],
        "preferred_leagues": row["preferred_leagues"] or [],
        "preferred_nations": row["preferred_nations"] or [],
        "min_rating": row["min_rating"],
        "max_rating": row["max_rating"],
        "min_profit": row["min_profit"],
        "notifications_enabled": row["notifications_enabled"],
    }

@router.put("/smart-buy/preferences")
async def upsert_preferences(request: Request, payload: SmartBuyPreferencesIn, conn=Depends(_db)) -> Dict[str, Any]:
    uid = _get_user_id(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Not authenticated")

    await conn.execute(
        """
        INSERT INTO smart_buy_preferences
        (user_id, default_budget, risk_tolerance, preferred_time_horizon,
         preferred_categories, excluded_positions, preferred_leagues, preferred_nations,
         min_rating, max_rating, min_profit, notifications_enabled, created_at, updated_at)
        VALUES
        ($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7::jsonb,$8::jsonb,$9,$10,$11,$12,NOW(),NOW())
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
        payload.default_budget,
        payload.risk_tolerance,
        payload.preferred_time_horizon,
        (payload.preferred_categories or []),
        (payload.excluded_positions or []),
        (payload.preferred_leagues or []),
        (payload.preferred_nations or []),
        payload.min_rating,
        payload.max_rating,
        payload.min_profit,
        payload.notifications_enabled,
    )
    return {"ok": True}

# --------- Feedback ---------
@router.post("/smart-buy/feedback")
async def smart_buy_feedback(request: Request, payload: SmartBuyFeedbackIn, conn=Depends(_db)) -> Dict[str, Any]:
    uid = _get_user_id(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Not authenticated")

    await conn.execute(
        """
        INSERT INTO smart_buy_feedback
        (user_id, card_id, action, notes, actual_buy_price, actual_sell_price, actual_profit, timestamp)
        VALUES ($1,$2,$3,$4,$5,$6,$7,NOW())
        """,
        uid,
        str(payload.card_id),
        payload.action,
        payload.notes or "",
        payload.actual_buy_price,
        payload.actual_sell_price,
        payload.actual_profit,
    )
    return {"ok": True}

# --------- Small stats (optional UI widget) ---------
@router.get("/smart-buy/stats")
async def smart_buy_stats(request: Request, conn=Depends(_db)) -> Dict[str, Any]:
    row = await conn.fetchrow(
        """
        SELECT
            COUNT(*)                         AS total_rows,
            COUNT(*) FILTER (WHERE user_id='global') AS global_rows,
            COUNT(*) FILTER (WHERE user_id<>'global') AS user_rows
        FROM smart_buy_suggestions
        """
    )
    return {
        "total": int(row["total_rows"] or 0),
        "global": int(row["global_rows"] or 0),
        "user": int(row["user_rows"] or 0),
    }
