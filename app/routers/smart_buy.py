from __future__ import annotations

import math
import asyncio
from statistics import pstdev
from typing import Any, Dict, List, Optional, Tuple

import asyncpg
from fastapi import APIRouter, Depends, Query, Request

# If your project already exposes these services/helpers, keep the imports.
# We use get_price_history that you already have.
from app.services.price_history import get_price_history

router = APIRouter(prefix="/smart-buy", tags=["smart-buy"])

# ---------------------------
# Infra helpers: pools & time
# ---------------------------

async def get_player_pool(request: Request) -> asyncpg.Pool:
    return request.app.state.player_pool

async def get_core_pool(request: Request) -> asyncpg.Pool:
    # same DB as trades/suggestions tables
    return request.app.state.pool

# ---------------------------
# Small utility helpers
# ---------------------------

def _norm_platform(p: str) -> str:
    p = (p or "").lower()
    if p in ("ps", "playstation", "console"): return "ps"
    if p in ("xbox", "xb"): return "xbox"
    if p in ("pc", "origin"): return "pc"
    return "ps"

def _title_case(s: str) -> str:
    return (s or "").strip().replace("_", " ").title()

def _horizon_bucket(h: str) -> str:
    h = (h or "").lower()
    if "flip" in h: return "flip"
    if "short" in h: return "short"
    if "mid" in h or "medium" in h: return "mid"
    if "long" in h: return "long"
    return "short"

def _profit_after_tax(buy: int, sell: int) -> Tuple[int, int]:
    gross = (sell - buy)
    after_tax = int(round((sell * 0.95) - buy))
    return int(gross), int(after_tax)

def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()

# ---------------------------
# History feature extraction
# ---------------------------

def _series_from_hist(hist: list[dict]) -> list[tuple[int, float]]:
    out = []
    for p in hist or []:
        t = p.get("t") or p.get("ts") or p.get("time")
        v = p.get("price") or p.get("v") or p.get("y")
        if t is not None and v is not None:
            try:
                out.append((int(t), float(v)))
            except Exception:
                continue
    out.sort(key=lambda x: x[0])
    return out

def _pct_volatility(prices: list[float]) -> float:
    """Percent volatility ~ stdev/mean (0..1+)."""
    if not prices or len(prices) < 6:
        return 0.0
    m = sum(prices) / len(prices)
    if m <= 0:
        return 0.0
    return float(pstdev(prices) / m)

def _lin_slope(prices: list[float]) -> float:
    """Linear regression slope on the index axis (price per sample)."""
    n = len(prices)
    if n < 3:
        return 0.0
    xb = (n - 1) / 2.0
    yb = sum(prices) / n
    num = sum((i - xb) * (y - yb) for i, y in enumerate(prices))
    den = sum((i - xb) ** 2 for i in range(n))
    return float(num / den) if den else 0.0

def _liq_proxy(series: list[tuple[int, float]]) -> float:
    """
    Lightweight liquidity proxy from intraday samples:
    - more points -> more recent updates (proxy for activity),
    - higher share of <1% micro-moves -> tighter, easier to trade.
    Returns 0..1 (higher is more liquid).
    """
    if not series:
        return 0.0
    n = len(series)
    if n < 6:
        return 0.15
    vals = [p for _, p in series[-min(96, n):]]  # last ~day of 15-min bars
    diffs = [abs(vals[i] - vals[i - 1]) for i in range(1, len(vals))]
    if not diffs:
        return 0.2
    avg = sum(vals) / len(vals)
    if avg <= 0:
        return 0.2
    tiny_moves = sum(1 for d in diffs if d / avg < 0.01)  # <1% ticks
    density = min(1.0, len(vals) / 96.0)
    smooth = min(1.0, tiny_moves / max(1, len(diffs)))
    return 0.25 * density + 0.75 * smooth

# ---------------------------
# AI blurb builder (per card)
# ---------------------------

def _ai_blurb(name: str, rating: Optional[int], risk: str, vol: float, liq: float, slope: float,
              uplift_pct: float, horizon: str, after_tax: int) -> str:
    rtxt = f"{rating}" if rating is not None else "–"
    trend = "uptrend" if slope > 0 else "dip"
    vol_txt = "low" if vol < 0.10 else "moderate" if vol < 0.20 else "high"
    liq_txt = "very liquid" if liq >= 0.55 else "liquid" if liq >= 0.35 else "thin"
    win = "1–6h" if horizon == "flip" else "6–24h" if horizon == "short" else "2–4d" if horizon == "mid" else "4–10d"
    return (
        f"{name} ({rtxt}) — {risk} profile. Intraday {trend}, volatility {vol_txt} ({vol:.0%}), "
        f"liquidity {liq_txt}. Target +{int(round(uplift_pct*100))}% giving ~{after_tax:,}c after tax. "
        f"Expected window: {win}. "
        f"{'Tight risk; avoid chasing spikes.' if vol_txt!='low' else 'Stable tape; favourable risk-reward.'}"
    )

# ---------------------------
# Public endpoints
# ---------------------------

@router.get("/market-intelligence")
async def market_intelligence(core_pool: asyncpg.Pool = Depends(get_core_pool)):
    """
    Very small placeholder: reads last market_state if present, otherwise 'Normal Trading'.
    This endpoint exists because the UI pings it for the banner.
    """
    try:
        row = await core_pool.fetchrow(
            "SELECT platform, state, confidence_score, detected_at "
            "FROM market_states ORDER BY detected_at DESC LIMIT 1"
        )
        if row:
            return {
                "state": row["state"] or "normal",
                "confidence": int(row["confidence_score"] or 60),
                "platform": row["platform"] or "ps",
                "detected_at": row["detected_at"].isoformat() if row["detected_at"] else None,
            }
    except Exception:
        pass
    # Default fallback
    return {"state": "normal", "confidence": 0, "platform": "ps", "detected_at": None}

@router.get("/stats")
async def smart_buy_stats(core_pool: asyncpg.Pool = Depends(get_core_pool)):
    """
    Tiny stats endpoint the front-end calls; safe defaults if tables are empty.
    """
    try:
        row = await core_pool.fetchrow(
            "SELECT COUNT(*)::int AS cnt FROM smart_buy_feedback"
        )
        taken = row["cnt"] if row else 0
    except Exception:
        taken = 0
    return {"suggestions_taken": taken, "success_rate": 0}

@router.get("/suggestions")
async def suggestions(
    budget: int = Query(100_000, ge=1_000, le=5_000_000),
    risk_tolerance: str = Query("moderate"),         # conservative | moderate | aggressive
    time_horizon: str = Query("short_term"),         # quick_flip | short_term | mid_term | long_term
    platform: str = Query("ps"),
    limit: int = Query(30, ge=1, le=100),
    player_pool: asyncpg.Pool = Depends(get_player_pool),
):
    plat = _norm_platform(platform)
    risk_t = _title_case(risk_tolerance)
    horizon = _horizon_bucket(time_horizon)  # "flip" | "short" | "mid" | "long"

    # -------- Risk knobs (affect WHO shows up) --------
    RISK = {
        "Conservative": {
            "min_rating": 86,
            "min_after_tax_profit": 3000,
            "max_vol": 0.10,
            "min_liq": 0.55,
            "momentum_bias": +0.5,  # want non-negative slope
            "target_boost": 0.00,
        },
        "Moderate": {
            "min_rating": 82,
            "min_after_tax_profit": 1800,
            "max_vol": 0.18,
            "min_liq": 0.35,
            "momentum_bias": +0.2,
            "target_boost": 0.03,
        },
        "Aggressive": {
            "min_rating": 76,
            "min_after_tax_profit": 700,
            "max_vol": 0.35,
            "min_liq": 0.15,
            "momentum_bias": -0.1,  # allow buying dips
            "target_boost": 0.06,
        },
    }
    knobs = RISK.get(risk_t, RISK["Moderate"])

    # -------- Horizon base uplift + risk top-up --------
    base = {"flip": 0.030, "short": 0.060, "mid": 0.100, "long": 0.150}[horizon]
    uplift = base + knobs["target_boost"]

    # -------- Candidate pool from fut_players (price is TEXT) --------
    rows = await player_pool.fetch(
        """
        WITH candidates AS (
          SELECT
            card_id,
            name,
            rating,
            COALESCE(version, 'Standard') AS version,
            image_url, club, league, nation, position, altposition,
            price::int AS price_int
          FROM fut_players
          WHERE price ~ '^[0-9]+$'
            AND price::int BETWEEN 300 AND $1
          ORDER BY rating DESC NULLS LAST
          LIMIT 1400
        )
        SELECT * FROM candidates
        """,
        budget,
    )
    if not rows:
        return {"items": [], "suggestions": [], "count": 0}

    # Quick prefilter: remove zero prices, enforce rating floor
    pre: List[asyncpg.Record] = []
    for r in rows:
        cur = int(r["price_int"] or 0)
        if cur <= 0: 
            continue
        if (r["rating"] or 0) < knobs["min_rating"]:
            continue
        pre.append(r)
    if not pre:
        return {"items": [], "suggestions": [], "count": 0}

    # Heuristic sampling to keep total history calls reasonable
    head = pre[:80]
    tail = pre[80:220]
    sample = head + tail[:40]  # ~120 max

    # -------- Gather intraday history concurrently --------
    async def _hist_task(card_id: int) -> Dict[str, Any]:
        try:
            hist = await get_price_history(int(card_id), plat, "today")
            series = _series_from_hist(hist)
            vals = [v for _, v in series]
            vol = _pct_volatility(vals)
            slope = _lin_slope(vals[-min(24, len(vals)):]) if vals else 0.0  # last ~6h
            liq = _liq_proxy(series)
            return {"ok": True, "vol": vol, "slope": slope, "liq": liq}
        except Exception:
            return {"ok": False, "vol": 0.0, "slope": 0.0, "liq": 0.0}

    hist_res = await asyncio.gather(
        *(_hist_task(int(r["card_id"])) for r in sample),
        return_exceptions=False
    )

    # -------- Build items under risk gates --------
    items: List[Dict[str, Any]] = []
    for r, h in zip(sample, hist_res):
        cur = int(r["price_int"] or 0)
        if cur <= 0 or not h.get("ok", False):
            continue

        vol = float(h["vol"])
        liq = float(h["liq"])
        slope = float(h["slope"])

        # Risk hard gates
        if vol > knobs["max_vol"]:
            continue
        if liq < knobs["min_liq"]:
            continue
        if knobs["momentum_bias"] > 0 and slope < 0:
            # conservative wants non-negative momentum
            continue

        target = int(round(cur * (1.0 + uplift)))
        gross, aft = _profit_after_tax(cur, target)
        if aft < knobs["min_after_tax_profit"]:
            continue

        rating = int(r["rating"] or 0)
        # Priority score: rating + after-tax + liq + momentum - volatility + budget fit
        prio = (
            rating * 0.55
            + (aft / 1000.0) * 0.85
            + liq * 20.0
            + (10.0 * math.tanh(slope / max(1.0, cur * 0.01)))
            - (vol * 30.0)
            + (1.0 - min(1.0, cur / max(1, budget))) * 8.0
        )
        prio = int(max(1, min(99, round(prio))))

        base_conf = 72 if risk_t == "Conservative" else 68 if risk_t == "Moderate" else 62
        conf = int(max(40, min(95, round(base_conf + (liq - vol) * 20.0))))

        name = r["name"] or f"Card {r['card_id']}"
        ai_note = _ai_blurb(
            name=name,
            rating=r["rating"],
            risk=risk_t,
            vol=vol,
            liq=liq,
            slope=slope,
            uplift_pct=uplift,
            horizon=horizon,
            after_tax=aft,
        )

        items.append({
            "card_id": str(r["card_id"]),
            "platform": plat,
            "suggestion_type": "buy-flip" if horizon in ("flip", "short") else "buy-dip",
            "current_price": cur,
            "target_price": target,
            "expected_profit": aft,
            "expected_profit_after_tax": aft,
            "risk_level": risk_t,
            "confidence_score": conf,
            "priority_score": prio,
            "reasoning": (
                f"{risk_t} • vol={vol:.2%}, liq={liq:.0%}, slope={'+' if slope>=0 else ''}{slope:.0f}; "
                f"target +{int(round(uplift*100))}% for {horizon}."
            ),
            "ai_analysis": ai_note,   # <- UI can show this
            "time_to_profit": "1-6h" if horizon == "flip" else
                              "6-24h" if horizon == "short" else
                              "2-4d" if horizon == "mid" else "4-10d",
            "market_state": "normal",
            "created_at": _now_iso(),
            # front-end metadata
            "name": name,
            "rating": r["rating"] or None,
            "version": r["version"] or "Standard",
            "image_url": r["image_url"] or None,
            "club": r["club"] or None,
            "league": r["league"] or None,
            "nation": r["nation"] or None,
            "position": r["position"] or None,
            "altposition": r["altposition"] or None,
            # diagnostics for debugging (optional)
            "diagnostics": {"vol": vol, "liq": liq, "slope": slope, "uplift": uplift},
        })

    # Rank/trim and return
    items.sort(key=lambda x: (x["priority_score"], x["confidence_score"], x["expected_profit"]), reverse=True)
    items = items[:limit]

    return {
        "items": items,
        "suggestions": items,  # keep for legacy front-end prop
        "count": len(items),
        "meta": {
            "platform": plat,
            "risk_tolerance": risk_t,
            "horizon": horizon,
            "budget": budget,
            "generated_at": _now_iso(),
        },
    }
