# app/routers/smart_buy.py
from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request

router = APIRouter(prefix="/smart-buy", tags=["smart-buy"])

# ---- helpers ---------------------------------------------------------------

def _ea_tax(sell_price: int) -> int:
    # EA tax ~5%
    return int(round(sell_price * 0.05))

def _platform_norm(p: Optional[str]) -> str:
    p = (p or "").lower()
    if p in ("ps", "playstation", "console"):  # console == ps in our data
        return "ps"
    if p in ("xbox", "xb"):
        return "xbox"
    if p in ("pc", "origin"):
        return "pc"
    return "ps"

async def _player_pool(req: Request):
    pp = getattr(req.app.state, "player_pool", None)
    if pp is None:
        raise HTTPException(500, "player_pool is not available (startup order?)")
    return pp

async def _main_pool(req: Request):
    pool = getattr(req.app.state, "pool", None)
    if pool is None:
        raise HTTPException(500, "pool is not available (startup order?)")
    return pool

# ---- tiny “state” heuristic ------------------------------------------------

def _heuristic_market_state(now: datetime) -> Tuple[str, int]:
    """
    Silly heuristic so the UI has something to show:
    - Return ('normal', 60..85) most of the time
    - Slightly elevated confidence in EU evening hours
    """
    hour_london = now.astimezone(timezone.utc).hour  # we don't need exact tz here
    base = 65
    if 17 <= hour_london <= 22:  # evening trading “livelier”
        base += 10
    return ("normal", max(50, min(90, base + random.randint(-5, 8))))

# ---- endpoints -------------------------------------------------------------

@router.get("/market-intelligence")
async def market_intelligence(req: Request):
    """
    Very light 'market intelligence' so the tile renders.
    Replace with your real signal when ready.
    """
    state, conf = _heuristic_market_state(datetime.now(timezone.utc))
    return {
        "market_state": state,
        "confidence": conf,
        "indicators": {
            "volatility": random.randint(35, 65),
            "liquidity": random.randint(55, 85),
            "momentum_24h": random.choice(["flat", "mild_up", "mild_down"]),
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/stats")
async def smart_buy_stats(req: Request, pool=Depends(_main_pool)):
    """
    Minimal stats so the front-end can paint something.
    """
    try:
        # trades table may live on main pool only
        row = await pool.fetchrow("""
            SELECT 
              COUNT(*)                           AS trades_logged,
              COALESCE(SUM(profit), 0)           AS total_profit,
              COALESCE(SUM(ea_tax), 0)           AS total_tax
            FROM trades
        """)
        return {
            "trades_logged": int(row["trades_logged"] or 0),
            "total_profit": int(row["total_profit"] or 0),
            "total_tax": int(row["total_tax"] or 0),
        }
    except Exception:
        # Don’t fail the page if stats cannot be read
        return {"trades_logged": 0, "total_profit": 0, "total_tax": 0}


@router.get("/suggestions")
async def smart_buy_suggestions(
    req: Request,
    budget: int = Query(100_000, ge=1, le=20_000_000),
    risk_tolerance: str = Query("moderate", regex="^(conservative|moderate|aggressive)$"),
    time_horizon: str = Query("short", regex="^(quick|short|mid|long)$"),
    platform: str = Query("ps"),
    limit: int = Query(12, ge=1, le=24),
    player_pool=Depends(_player_pool),
):
    """
    Heuristic suggestion generator that **always** returns up to `limit` rows
    as long as the fut_players table has eligible items.

    Key design choices to prevent empty lists:
    - We *never* drop an item solely because expected profit <= 0.
    - We compute a target and show profit as-is (can be small/negative).
    - We sample from a broad pool, then trim to `limit`.
    """
    plat = _platform_norm(platform)
    risk = risk_tolerance.lower()

    # Pick a target percent lift based on risk
    # (used only to generate a mock target price)
    if risk == "conservative":
        pct_min, pct_max = 2.0, 5.0
    elif risk == "aggressive":
        pct_min, pct_max = 6.0, 12.0
    else:  # moderate
        pct_min, pct_max = 3.5, 8.0

    # Horizon label
    horizon_label = {
        "quick": "1-6h",
        "short": "6-48h",
        "mid": "2-7d",
        "long": "1-3w",
    }.get(time_horizon, "6-48h")

    # 1) Pull a reasonably large candidate set from fut_players on the *player DB*
    # NOTE: fut_players.price is TEXT; keep only numeric entries and >0,
    #       then under budget.
    # We fetch a larger pool (e.g., 300) then do a light shuffle.
    fetch_cap = min(500, max(limit * 10, 300))
    rows = await player_pool.fetch(
        """
        SELECT
          card_id, name, rating, version, image_url,
          club, league, nation, position, altposition,
          price
        FROM fut_players
        WHERE price ~ '^[0-9]+$'
          AND price::int BETWEEN 1 AND $1
        ORDER BY rating DESC NULLS LAST, name ASC
        LIMIT $2
        """,
        budget,
        fetch_cap,
    )

    if not rows:
        return {"items": [], "count": 0}

    # 2) Light shuffle so we don’t always get the same cards
    rows = list(rows)
    random.shuffle(rows)

    items: List[Dict[str, Any]] = []

    for r in rows:
        # Numeric price guaranteed by WHERE clause; still guard
        try:
            px = int(r["price"])
        except Exception:
            continue
        if px <= 0:
            continue

        # Synthesize a target and show profit-after-tax (can be <= 0)
        target_pct = random.uniform(pct_min, pct_max)
        target_price = int(round(px * (1.0 + target_pct / 100.0)))
        tax = _ea_tax(target_price)
        exp_profit_after_tax = (target_price - px) - tax

        # Scores: keep them bounded so the UI looks sane
        rating = int(r["rating"] or 0)
        profit_score = (exp_profit_after_tax / max(1, px)) if exp_profit_after_tax > 0 else 0.0
        priority = max(1, min(100, int(round(50 + 0.35 * rating + 30 * profit_score))))
        confidence = max(1, min(100, int(round(55 + 0.25 * rating + 15 * (min(12_000, px) / 12_000.0)))))

        items.append({
            "card_id": str(r["card_id"]),
            "platform": plat,
            "suggestion_type": "buy-flip" if target_pct >= 6 else "buy-dip",
            "current_price": px,
            "target_price": target_price,
            "expected_profit": exp_profit_after_tax,  # may be small or negative
            "risk_level": risk,
            "confidence_score": confidence,
            "priority_score": priority,
            "reasoning": (
                "Candidate under budget; target synthesized from risk range. "
                "Profit shown after 5% tax — may be small or negative."
            ),
            "time_to_profit": horizon_label,
            "market_state": "normal",
            "created_at": datetime.now(timezone.utc).isoformat(),

            # Enrichment from fut_players (already in this DB)
            "name": r["name"],
            "rating": rating,
            "version": r["version"],
            "image_url": r["image_url"],
            "club": r["club"],
            "league": r["league"],
            "nation": r["nation"],
            "position": r["position"],
            "altposition": r["altposition"],
        })

        if len(items) >= limit:
            break

    # If somehow we still didn’t reach `limit` (e.g., lots of zeros),
    # just return what we have; never 404/empty unless there are truly no rows.
    return {"items": items, "count": len(items)}
