from __future__ import annotations

import math
import asyncio
from statistics import pstdev
from typing import Any, Dict, List, Optional, Tuple

import asyncpg
from fastapi import APIRouter, Depends, Query, Request, HTTPException

from app.services.price_history import get_price_history

smart_buy_router = APIRouter(prefix="/smart-buy", tags=["smart-buy"])

# ---------------------------
# Pools
# ---------------------------

async def core_pool(req: Request) -> asyncpg.Pool:
    return req.app.state.pool

async def player_pool(req: Request) -> asyncpg.Pool:
    return req.app.state.player_pool


# ---------------------------
# Utils
# ---------------------------

def _norm_platform(p: str) -> str:
    p = (p or "").lower()
    if p in ("ps", "playstation", "console"): return "ps"
    if p in ("xbox", "xb"): return "xbox"
    if p in ("pc", "origin"): return "pc"
    return "ps"

def _horizon_bucket(h: str) -> str:
    h = (h or "").lower()
    if "flip" in h: return "flip"
    if "short" in h: return "short"
    if "mid" in h or "medium" in h: return "mid"
    if "long" in h: return "long"
    return "short"

def _profit_after_tax(buy: int, sell: int) -> Tuple[int, int]:
    gross = sell - buy
    after_tax = int(round((sell * 0.95) - buy))
    return int(gross), int(after_tax)

def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ---------------------------
# History features
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
    if not prices or len(prices) < 6: return 0.0
    m = sum(prices) / len(prices)
    if m <= 0: return 0.0
    return float(pstdev(prices) / m)

def _lin_slope(prices: list[float]) -> float:
    n = len(prices)
    if n < 3: return 0.0
    xb = (n - 1) / 2.0
    yb = sum(prices) / n
    num = sum((i - xb) * (y - yb) for i, y in enumerate(prices))
    den = sum((i - xb) ** 2 for i in range(n))
    return float(num / den) if den else 0.0

def _liq_proxy(series: list[tuple[int, float]]) -> float:
    if not series: return 0.25
    n = len(series)
    if n < 6: return 0.30
    vals = [v for _, v in series[-min(96, n):]]
    diffs = [abs(vals[i] - vals[i - 1]) for i in range(1, len(vals))]
    if not diffs: return 0.35
    avg = sum(vals) / len(vals)
    if avg <= 0: return 0.35
    tiny_moves = sum(1 for d in diffs if d / avg < 0.01)
    density = min(1.0, len(vals) / 96.0)
    smooth = min(1.0, tiny_moves / max(1, len(diffs)))
    return 0.25 * density + 0.75 * smooth


# ---------------------------
# AI text
# ---------------------------

def _ai_blurb(name: str, rating: Optional[int], risk: str, vol: float, liq: float,
              slope: float, uplift_pct: float, horizon: str, after_tax: int) -> str:
    rtxt = f"{rating}" if rating is not None else "–"
    trend = "uptrend" if slope > 0 else "controlled dip"
    vol_txt = "low" if vol < 0.10 else "moderate" if vol < 0.20 else "high"
    liq_txt = "very liquid" if liq >= 0.55 else "liquid" if liq >= 0.35 else "thin"
    win = "1–6h" if horizon == "flip" else "6–24h" if horizon == "short" else "2–4d" if horizon == "mid" else "4–10d"
    return (
        f"{name} ({rtxt}) — {risk}. Intraday {trend}, volatility {vol_txt} ({vol:.0%}), "
        f"liquidity {liq_txt}. Aim +{int(round(uplift_pct*100))}% → ~{after_tax:,}c after tax. "
        f"Expected window: {win}. "
        f"{'Avoid chasing spikes; scale in.' if vol_txt!='low' else 'Stable tape; good RR.'}"
    )


# ---------------------------
# Light endpoints
# ---------------------------

@smart_buy_router.get("/market-intelligence")
async def market_intelligence(pool: asyncpg.Pool = Depends(core_pool)):
    try:
        row = await pool.fetchrow(
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
    return {"state": "normal", "confidence": 0, "platform": "ps", "detected_at": None}


@smart_buy_router.get("/stats")
async def stats(pool: asyncpg.Pool = Depends(core_pool)):
    try:
        taken = await pool.fetchval("SELECT COALESCE(COUNT(*),0)::int FROM smart_buy_feedback")
    except Exception:
        taken = 0
    return {"suggestions_taken": taken, "success_rate": 0}


@smart_buy_router.post("/feedback")
async def feedback(payload: Dict[str, Any], pool: asyncpg.Pool = Depends(core_pool)):
    try:
        await pool.execute(
            """
            INSERT INTO smart_buy_feedback (user_id, card_id, action, notes, actual_buy_price, actual_sell_price)
            VALUES ($1,$2,$3,$4,$5,$6)
            """,
            payload.get("user_id"),
            str(payload.get("card_id")),
            (payload.get("action") or "taken"),
            payload.get("notes"),
            payload.get("actual_buy_price"),
            payload.get("actual_sell_price"),
        )
        return {"ok": True}
    except Exception as e:
        raise HTTPException(400, f"feedback error: {e}")


# ---------------------------
# Suggestions (with progressive fallback)
# ---------------------------

@smart_buy_router.get("/suggestions")
async def suggestions(
    budget: int = Query(100_000, ge=1_000, le=5_000_000),
    risk_tolerance: str = Query("moderate"),
    time_horizon: str = Query("short_term"),
    platform: str = Query("ps"),
    limit: int = Query(30, ge=1, le=100),
    ppool: asyncpg.Pool = Depends(player_pool),
    cpool: asyncpg.Pool = Depends(core_pool),
):
    plat = _norm_platform(platform)
    risk = risk_tolerance.strip().lower()
    risk_name = "Conservative" if risk.startswith("cons") else "Aggressive" if risk.startswith("agg") else "Moderate"
    horizon = _horizon_bucket(time_horizon)

    # Base uplift by horizon (then risk tweak)
    base = {"flip": 0.030, "short": 0.060, "mid": 0.100, "long": 0.150}[horizon]
    risk_boost = {"Conservative": 0.00, "Moderate": 0.03, "Aggressive": 0.06}[risk_name]
    if risk_name == "Aggressive" and horizon == "flip":
        risk_boost += 0.02  # punchier for quick flip
    uplift = base + risk_boost

    # Risk gates (tight → loose through fallback rounds)
    # Each round relaxes max_vol (higher allowed), min_liq (lower required), slope_gate (more negative allowed),
    # min_rating (lower allowed), min_after_tax_profit (lower requirement).
    BASE = {
        "Conservative": dict(min_rating=86, min_after=3000, max_vol=0.10, min_liq=0.55, slope_gate=0.0),
        "Moderate":     dict(min_rating=82, min_after=1800, max_vol=0.18, min_liq=0.35, slope_gate=-0.10),
        "Aggressive":   dict(min_rating=76, min_after= 700, max_vol=0.35, min_liq=0.15, slope_gate=-0.50),
    }
    base_knobs = BASE[risk_name]

    # Candidate pool from fut_players (price stored as TEXT)
    cand_rows = await ppool.fetch(
        """
        WITH c AS (
          SELECT
            card_id, name, rating, COALESCE(version,'Standard') AS version,
            image_url, club, league, nation, position, altposition,
            price::int AS price_int
          FROM fut_players
          WHERE price ~ '^[0-9]+$'
            AND price::int BETWEEN 300 AND $1
          ORDER BY rating DESC NULLS LAST
          LIMIT 2000
        )
        SELECT * FROM c
        """,
        budget,
    )
    if not cand_rows:
        return {"items": [], "count": 0}

    # Light prefilter
    prelim: List[asyncpg.Record] = [r for r in cand_rows if int(r["price_int"] or 0) > 0]
    prelim = prelim[:320]  # keep concurrency sensible

    # Feature extraction (with synthetic fallback if history is thin)
    async def _feat(card_id: int) -> Dict[str, Any]:
        try:
            hist = await get_price_history(int(card_id), plat, "today")
            series = _series_from_hist(hist)
            vals = [v for _, v in series]
            if len(vals) < 6:
                # synthetic, “safe” features so we *never* drop to zero
                return {"ok": True, "vol": 0.14, "liq": 0.40, "slope": 0.0}
            vol = _pct_volatility(vals)
            slope = _lin_slope(vals[-min(24, len(vals)):]) if vals else 0.0
            liq = _liq_proxy(series)
            return {"ok": True, "vol": float(vol), "slope": float(slope), "liq": float(liq)}
        except Exception:
            # network/glitch → synthetic
            return {"ok": True, "vol": 0.16, "liq": 0.38, "slope": 0.0}

    feats = await asyncio.gather(*(_feat(int(r["card_id"])) for r in prelim))

    # Progressive rounds: widen the gates until we have enough
    rounds = [
        dict(mult_vol=1.00, mult_liq=1.00, slope_add=0.00, min_rating_delta=0,  min_after_delta=0),
        dict(mult_vol=1.20, mult_liq=0.85, slope_add=-0.10, min_rating_delta=-2, min_after_delta=-200),
        dict(mult_vol=1.50, mult_liq=0.70, slope_add=-0.25, min_rating_delta=-4, min_after_delta=-500),
        dict(mult_vol=2.00, mult_liq=0.55, slope_add=-0.40, min_rating_delta=-6, min_after_delta=-900),
        dict(mult_vol=3.50, mult_liq=0.40, slope_add=-0.60, min_rating_delta=-8, min_after_delta=-1200),
    ]

    selected: List[Dict[str, Any]] = []

    def _score_item(cur: int, aft: int, rating: int, liq: float, vol: float, slope: float) -> int:
        return int(max(1, min(99, round(
            rating * 0.55
            + (aft / 1000.0) * 0.85
            + liq * 20.0
            + (10.0 * math.tanh(slope / max(1.0, cur * 0.01)))
            - (vol * 30.0)
            + (1.0 - min(1.0, cur / max(1, budget))) * 8.0
        ))))

    for r in rounds:
        if len(selected) >= limit:
            break

        # compute this round’s gates
        max_vol = base_knobs["max_vol"] * r["mult_vol"]
        min_liq = base_knobs["min_liq"] * r["mult_liq"]
        slope_gate = base_knobs["slope_gate"] + r["slope_add"]
        min_rating = max(60, base_knobs["min_rating"] + r["min_rating_delta"])
        min_after = max(200, base_knobs["min_after"] + r["min_after_delta"])

        batch: List[Dict[str, Any]] = []

        for row, f in zip(prelim, feats):
            cur = int(row["price_int"] or 0)
            rating = int(row["rating"] or 0)
            if rating < min_rating:
                continue

            vol = float(f["vol"]); liq = float(f["liq"]); slope = float(f["slope"])

            # gates
            if vol > max_vol: 
                continue
            if liq < min_liq:
                continue
            if slope < slope_gate:
                continue

            target = int(round(cur * (1.0 + uplift)))
            _, aft = _profit_after_tax(cur, target)
            if aft < min_after:
                continue

            name = row["name"] or f"Card {row['card_id']}"
            prio = _score_item(cur, aft, rating, liq, vol, slope)
            base_conf = 74 if risk_name == "Conservative" else 68 if risk_name == "Moderate" else 62
            conf = int(max(40, min(95, round(base_conf + (liq - vol) * 22.0))))

            ai_text = _ai_blurb(
                name=name, rating=rating, risk=risk_name, vol=vol, liq=liq,
                slope=slope, uplift_pct=uplift, horizon=horizon, after_tax=aft
            )

            batch.append({
                "card_id": str(row["card_id"]),
                "platform": plat,
                "suggestion_type": "buy-flip" if horizon in ("flip", "short") else "buy-dip",
                "current_price": cur,
                "target_price": target,
                "expected_profit": aft,
                "expected_profit_after_tax": aft,
                "risk_level": risk_name,
                "confidence_score": conf,
                "priority_score": prio,
                "reasoning": (
                    f"{risk_name} • vol={vol:.2%}, liq={liq:.0%}, slope={'+' if slope>=0 else ''}{slope:.0f}; "
                    f"target +{int(round(uplift*100))}% for {horizon}."
                ),
                "ai_analysis": ai_text,
                "analysis": ai_text,
                "time_to_profit": "1-6h" if horizon == "flip" else "6-24h" if horizon == "short"
                                  else "2-4d" if horizon == "mid" else "4-10d",
                "market_state": "normal",
                "created_at": _now_iso(),
                "name": name,
                "rating": rating,
                "version": row["version"] or "Standard",
                "image_url": row["image_url"] or None,
                "club": row["club"] or None,
                "league": row["league"] or None,
                "nation": row["nation"] or None,
                "position": row["position"] or None,
                "altposition": row["altposition"] or None,
            })

        # order this round and take what we still need
        if batch:
            batch.sort(key=lambda x: (x["priority_score"], x["confidence_score"], x["expected_profit"]), reverse=True)
            remaining = max(0, limit - len(selected))
            selected.extend(batch[:remaining])

    # final sort + trim (in case multiple rounds overfilled a bit)
    selected.sort(key=lambda x: (x["priority_score"], x["confidence_score"], x["expected_profit"]), reverse=True)
    selected = selected[:limit]

    # persist best-effort
    if selected:
        try:
            await cpool.executemany(
                """
                INSERT INTO smart_buy_suggestions (
                    user_id, card_id, suggestion_type, current_price, target_price,
                    expected_profit, risk_level, confidence_score, priority_score,
                    reasoning, time_to_profit, platform, market_state, created_at
                )
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13, NOW())
                """,
                [
                    (
                        "public",
                        it["card_id"],
                        it["suggestion_type"],
                        it["current_price"],
                        it["target_price"],
                        it["expected_profit_after_tax"],
                        it["risk_level"],
                        it["confidence_score"],
                        it["priority_score"],
                        it["reasoning"],
                        it["time_to_profit"],
                        it["platform"],
                        it["market_state"],
                    )
                    for it in selected
                ],
            )
        except Exception:
            pass

    return {"items": selected, "suggestions": selected, "count": len(selected)}
    router = smart_buy_router
