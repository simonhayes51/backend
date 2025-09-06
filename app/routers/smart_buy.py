# app/routers/smart_buy.py
import math
from typing import Any, Dict, List, Optional, Tuple, Literal

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from pydantic import BaseModel

router = APIRouter(prefix="/smart-buy", tags=["smart-buy"])

# ----------------------------
# Tunables (safe defaults)
# ----------------------------
SUGGESTION_TTL_MINUTES = 90         # suggestions expire quick; UI can refresh
MIN_HISTORY_POINTS_24H = 6          # require at least this many points in 24h window
FALLER_DROP_PCT_MIN    = 5.0        # consider cards that dropped ≥5% in last 24h
RECOVERY_TARGET_PCT    = 0.60       # target 60% mean-reversion back towards 24h avg
MAX_SUGGESTIONS        = 30         # cap per refresh to keep UI snappy

# Risk buckets by 24h volatility (%)
VOL_LOW_MAX   = 3.0
VOL_MOD_MAX   = 8.0
# >8% becomes "high"

# ----------------------------
# Models
# ----------------------------
class SBPreferences(BaseModel):
    default_budget: int = 100_000
    risk_tolerance: Literal["conservative", "moderate", "aggressive"] = "moderate"
    preferred_time_horizon: Literal["quick_flip", "short", "long_term"] = "short"  # UI maps to 6–48h
    preferred_categories: List[str] = []
    excluded_positions: List[str] = []
    preferred_leagues: List[str] = []
    preferred_nations: List[str] = []
    min_rating: int = 75
    max_rating: int = 95
    min_profit: int = 1_000
    notifications_enabled: bool = True

class RefreshPayload(BaseModel):
    budget: Optional[int] = None
    risk: Optional[Literal["conservative","moderate","aggressive"]] = None
    horizon: Optional[Literal["quick_flip","short","long_term"]] = None
    platform: Optional[Literal["ps","xbox","pc"]] = "ps"

class SuggestionFilters(BaseModel):
    budget: Optional[int] = None
    risk: Optional[Literal["low","medium","high"]] = None
    horizon: Optional[Literal["quick_flip","short","long_term"]] = None
    platform: Optional[Literal["ps","xbox","pc"]] = "ps"

# ----------------------------
# Helpers
# ----------------------------
def _risk_from_vol(vol_pct: float) -> str:
    if vol_pct <= VOL_LOW_MAX:
        return "low"
    if vol_pct <= VOL_MOD_MAX:
        return "medium"
    return "high"

def _confidence(drop_pct: float, vol_pct: float) -> int:
    """
    0-100: more confidence for sizable drop with low volatility.
    """
    base = max(0.0, min(1.0, (drop_pct - FALLER_DROP_PCT_MIN) / 10.0))  # 0 to ~1 as drop grows 5->15%
    calm = max(0.0, 1.0 - (vol_pct / 12.0))  # penalize very volatile stuff
    return int(round(100 * (0.55 * base + 0.45 * calm)))

def _priority(expected_profit: int, confidence: int, price: int) -> int:
    """
    1-10: cheap + decent profit + confidence => higher priority
    """
    if price <= 0:
        return 1
    score = (expected_profit / max(10_000, price)) * 5 + (confidence / 100) * 5
    return max(1, min(10, int(round(score))))

async def _get_user_id(request: Request) -> str:
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(401, "Not authenticated")
    return uid

# ----------------------------
# Preferences
# ----------------------------
@router.get("/preferences")
async def get_preferences(request: Request):
    user_id = await _get_user_id(request)
    pool = request.app.state.pool

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT default_budget, risk_tolerance, preferred_time_horizon,
                   preferred_categories, excluded_positions,
                   preferred_leagues, preferred_nations,
                   min_rating, max_rating, min_profit, notifications_enabled
            FROM smart_buy_preferences
            WHERE user_id = $1
            """,
            user_id,
        )
    if row:
        # jsonb fields already in PG json; return as-is
        return dict(row)
    # default
    return SBPreferences().model_dump()

@router.post("/preferences")
async def upsert_preferences(request: Request, prefs: SBPreferences):
    user_id = await _get_user_id(request)
    pool = request.app.state.pool

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO smart_buy_preferences (
                user_id, default_budget, risk_tolerance, preferred_time_horizon,
                preferred_categories, excluded_positions, preferred_leagues, preferred_nations,
                min_rating, max_rating, min_profit, notifications_enabled, created_at, updated_at
            ) VALUES (
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
            prefs.default_budget,
            prefs.risk_tolerance,
            prefs.preferred_time_horizon,
            prefs.preferred_categories,
            prefs.excluded_positions,
            prefs.preferred_leagues,
            prefs.preferred_nations,
            prefs.min_rating,
            prefs.max_rating,
            prefs.min_profit,
            prefs.notifications_enabled,
        )

    return {"ok": True}

# ----------------------------
# Core logic – build suggestions
# ----------------------------
async def _fetch_candidates(request: Request, budget: int, platform: str,
                            min_rating: int, max_rating: int) -> List[Dict[str, Any]]:
    """
    Return cards with live price <= budget and with some 24h history.
    """
    player_pool = request.app.state.player_pool
    async with player_pool.acquire() as pconn:
        rows = await pconn.fetch(
            """
            SELECT card_id::text AS card_id, name, rating, version, image_url, league, nation, club, price
            FROM fut_players
            WHERE price IS NOT NULL
              AND price > 0
              AND price <= $1
              AND rating BETWEEN $2 AND $3
            ORDER BY price DESC
            LIMIT 400
            """,
            budget, min_rating, max_rating
        )
    return [dict(r) for r in rows]

async def _load_history_24h(conn, card_id: str, platform: str) -> List[Tuple[datetime, int]]:
    rows = await conn.fetch(
        """
        SELECT captured_at, price
        FROM fut_prices_history
        WHERE card_id = $1
          AND platform = $2
          AND captured_at >= now() - interval '24 hours'
        ORDER BY captured_at ASC
        """,
        card_id, platform,
    )
    return [(r["captured_at"], int(r["price"])) for r in rows]

def _stats_from_series(series: List[Tuple[datetime,int]]) -> Dict[str, Any]:
    if not series:
        return {}
    prices = [p for _, p in series]
    if len(prices) < 2:
        return {}
    cur = prices[-1]
    avg24 = sum(prices) / len(prices)
    low24 = min(prices)
    high24 = max(prices)
    # simple vol: average absolute step / mean
    diffs = [abs(prices[i]-prices[i-1]) for i in range(1, len(prices))]
    vol_abs = (sum(diffs) / len(diffs)) if diffs else 0.0
    vol_pct = (vol_abs / avg24 * 100.0) if avg24 else 0.0
    drop_pct = ((avg24 - cur) / avg24 * 100.0) if avg24 else 0.0
    return {
        "cur": cur,
        "avg24": int(round(avg24)),
        "low24": low24,
        "high24": high24,
        "vol_pct": round(vol_pct, 2),
        "drop_pct": round(drop_pct, 2),
        "n": len(prices),
    }

async def _build_suggestions_for_user(request: Request, user_id: str,
                                      budget: int, risk_pref: str,
                                      horizon: str, platform: str) -> int:
    """
    Rebuild suggestions: delete previous for user, insert fresh ones.
    """
    pool = request.app.state.pool

    # load user pref constraints for rating/profit floors
    async with pool.acquire() as conn:
        pref_row = await conn.fetchrow(
            """
            SELECT min_rating, max_rating, min_profit
            FROM smart_buy_preferences
            WHERE user_id = $1
            """,
            user_id,
        )
    if pref_row:
        min_rating = pref_row["min_rating"]
        max_rating = pref_row["max_rating"]
        min_profit = pref_row["min_profit"]
    else:
        min_rating = 75
        max_rating = 95
        min_profit = 1000

    # fetch candidate cards (live price within budget)
    candidates = await _fetch_candidates(request, budget, platform, min_rating, max_rating)
    if not candidates:
        # wipe prior suggestions
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM smart_buy_suggestions WHERE user_id=$1", user_id)
        return 0

    expires_at = datetime.now(timezone.utc) + timedelta(minutes=SUGGESTION_TTL_MINUTES)
    inserted = 0

    # we read history and insert in one DB connection for efficiency
    async with pool.acquire() as conn:
        # wipe previous
        await conn.execute("DELETE FROM smart_buy_suggestions WHERE user_id=$1", user_id)

        for card in candidates:
            cid = str(card["card_id"])
            series = await _load_history_24h(conn, cid, platform)
            stats = _stats_from_series(series)
            if not stats or stats["n"] < MIN_HISTORY_POINTS_24H:
                continue

            cur   = stats["cur"]
            avg24 = stats["avg24"]
            drop  = stats["drop_pct"]
            vol   = stats["vol_pct"]

            # Only consider mild fallers with at least X% below 24h mean
            if drop < FALLER_DROP_PCT_MIN:
                continue

            # derive risk vs user's tolerance
            risk_bucket = _risk_from_vol(vol)
            if risk_pref == "conservative" and risk_bucket == "high":
                continue
            if risk_pref == "moderate" and risk_bucket == "high":
                # allow but lower priority later (we can still skip if you want)
                pass

            # expected target = current + fraction of mean reversion towards avg24
            target_price = int(round(cur + (avg24 - cur) * RECOVERY_TARGET_PCT))
            expected_profit = int(round((target_price - cur) * 0.95))  # after 5% EA tax on sale

            if expected_profit < min_profit:
                continue

            confidence = _confidence(drop, vol)
            priority   = _priority(expected_profit, confidence, cur)

            await conn.execute(
                """
                INSERT INTO smart_buy_suggestions (
                    user_id, card_id, suggestion_type, current_price, target_price,
                    expected_profit, risk_level, confidence_score, priority_score,
                    reasoning, time_to_profit, platform, market_state, created_at, expires_at
                ) VALUES (
                    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13, NOW(), $14
                )
                """,
                user_id,
                cid,
                "recovery_play",
                int(cur),
                int(target_price),
                int(expected_profit),
                risk_bucket,
                int(confidence),
                int(priority),
                f"Dropped {drop:.1f}% vs 24h avg with {vol:.1f}% volatility.",
                "6-48h" if horizon in ("quick_flip", "short") else "2-7d",
                platform,
                "normal",
                expires_at,
            )
            inserted += 1
            if inserted >= MAX_SUGGESTIONS:
                break

    return inserted

# ----------------------------
# Endpoints
# ----------------------------
@router.post("/refresh")
async def refresh_suggestions(request: Request, payload: RefreshPayload):
    user_id = await _get_user_id(request)

    # read prefs as defaults
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        pref = await conn.fetchrow(
            """
            SELECT default_budget, risk_tolerance, preferred_time_horizon
            FROM smart_buy_preferences WHERE user_id=$1
            """,
            user_id,
        )

    budget  = payload.budget or (pref["default_budget"] if pref else 100_000)
    risk    = payload.risk or (pref["risk_tolerance"] if pref else "moderate")
    horizon = payload.horizon or (pref["preferred_time_horizon"] if pref else "short")
    platform = (payload.platform or "ps").lower()

    inserted = await _build_suggestions_for_user(request, user_id, int(budget), risk, horizon, platform)
    return {"ok": True, "inserted": inserted}

@router.get("/suggestions")
async def get_suggestions(
    request: Request,
    platform: Literal["ps","xbox","pc"] = "ps",
    max_items: int = Query(30, ge=1, le=100),
    budget: Optional[int] = None,
    risk: Optional[Literal["low","medium","high"]] = None,
    horizon: Optional[Literal["quick_flip","short","long_term"]] = None,
):
    user_id = await _get_user_id(request)
    pool = request.app.state.pool

    clauses = ["user_id = $1", "platform = $2", "(expires_at IS NULL OR expires_at > NOW())"]
    params: List[Any] = [user_id, platform]
    pi = 3

    if budget is not None:
        clauses.append(f"current_price <= ${pi}")
        params.append(budget); pi += 1
    if risk is not None:
        clauses.append(f"risk_level = ${pi}")
        params.append(risk); pi += 1
    if horizon is not None:
        # stored as "6-48h" or "2-7d"; map roughly
        if horizon in ("quick_flip", "short"):
            clauses.append("time_to_profit IN ('6-48h')")
        else:
            clauses.append("time_to_profit IN ('2-7d')")

    sql = f"""
        SELECT id, user_id, card_id, suggestion_type, current_price, target_price,
               expected_profit, risk_level, confidence_score, priority_score, reasoning,
               time_to_profit, platform, market_state, created_at, expires_at
        FROM smart_buy_suggestions
        WHERE {' AND '.join(clauses)}
        ORDER BY priority_score DESC, confidence_score DESC, expected_profit DESC
        LIMIT {max_items}
    """

    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)

    # enrich with player meta for UI
    player_pool = request.app.state.player_pool
    ids = [r["card_id"] for r in rows]
    meta_map: Dict[str, Dict[str, Any]] = {}
    if ids:
        async with player_pool.acquire() as pconn:
            meta_rows = await pconn.fetch(
                """
                SELECT card_id::text AS card_id, name, rating, version, image_url, league, nation, club
                FROM fut_players
                WHERE card_id = ANY($1::text[])
                """,
                ids,
            )
        meta_map = {m["card_id"]: dict(m) for m in meta_rows}

    out = []
    for r in rows:
        m = meta_map.get(r["card_id"], {})
        d = dict(r)
        d["player"] = {
            "name": m.get("name"),
            "rating": m.get("rating"),
            "version": m.get("version"),
            "image_url": m.get("image_url"),
            "league": m.get("league"),
            "nation": m.get("nation"),
            "club": m.get("club"),
        }
        out.append(d)

    return {"items": out}