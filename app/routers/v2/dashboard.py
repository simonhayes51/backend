# app/routers/v2/dashboard.py
#
# Single aggregated Home Dashboard endpoint - mirrors app/routers/v2/
# players.py's player_summary() aggregation pattern (one call instead of
# N separate hook fetches), built against the exact response contract
# the current frontend redesign expects (marketRegime, todaysOpportunities,
# highConfidenceInvestments, cardsToAvoid, biggestMovers,
# recentAiPredictions, watchlistAlerts, latestMarketEvents,
# latestSbcImpact).
#
# Gating is preserved, not dropped: opportunity_feed still gates
# todaysOpportunities/highConfidenceInvestments/cardsToAvoid/
# recentAiPredictions exactly as it already does on the separate feed
# endpoints (app/routers/v2/recommendations.py) - this endpoint adds one
# `locked` block to the response (additive beyond the frontend's
# originally-sketched contract) so the UI can render the same rich
# layout with an upsell for those sections instead of silently returning
# empty arrays that would look like "no signals today."
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request

from app.auth.entitlements import compute_entitlements
from app.db import get_core_pool, get_player_pool, get_watchlist_db

router = APIRouter(tags=["v2-dashboard"])

_STATE_LABEL = {"bullish": "Bullish", "bearish": "Bearish", "illiquid": "Illiquid", "normal": "Normal"}

# recommendation_engine.py's RuleV1Strategy only ever assigns
# recommendation in {"buy", "hold", "avoid"} - "hold" really does mean
# "no strong edge either way, wait" (see its own reasoning text), so
# WAIT is an accurate rename, not a stronger claim than the data
# supports. There is no real "sell" signal (avoid means "don't buy
# this", not "liquidate what you own") - frontend callers that want a
# SELL slot fall back to AVOID when none exists, same as the reference
# design's own pickRecommendation() fallback chain.
_ACTION_LABEL = {"buy": "BUY", "hold": "WAIT", "avoid": "AVOID"}

_FACTOR_LABEL = {
    "discount_vs_fair_value": lambda v: f"Trading {float(v):.1f}% below its real 24h median.",
    "liquidity_sales_per_hour": lambda v: f"{float(v):.1f} sales/hour liquidity.",
    "trend_falling": lambda v: "Price is in a confirmed downward trend, not a discount.",
}


def _drivers_to_strings(drivers: List[Dict[str, Any]]) -> List[str]:
    out = []
    for d in drivers or []:
        factor = d.get("factor", "")
        if factor.startswith("market_event_"):
            out.append(f"Related market event: {d.get('title') or d.get('event_id')}")
        elif factor in _FACTOR_LABEL:
            out.append(_FACTOR_LABEL[factor](d.get("value")))
        else:
            out.append(factor)
    return out


def _regime_summary(state: str, indicators: Dict[str, Any]) -> str:
    falling_pct = indicators.get("falling_pct") or 0
    avg_discount = indicators.get("avg_discount_pct") or 0
    avg_liquidity = indicators.get("avg_liquidity") or 0
    if state == "bullish":
        return f"Tracked cards are trading an average {avg_discount:.1f}% below real value with only {falling_pct:.0f}% in a falling trend - buyers have the edge."
    if state == "bearish":
        return f"{falling_pct:.0f}% of tracked cards are in a confirmed downward trend right now - treat discounts as falling knives, not steals."
    if state == "illiquid":
        return f"Average liquidity is only {avg_liquidity:.2f} sales/hour across tracked cards - moves take longer to confirm."
    return "Prices and liquidity are broadly in line with recent norms across tracked cards."


def _to_recommendation(d: Dict[str, Any], scores: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """Reshapes a recommendations_latest row (+ optional card_scores) into
    the frontend's Recommendation contract. current_bin/fair_value_24h/
    sales_24h/sales_7d aren't columns on recommendations/
    recommendations_latest themselves - every query building `d` LEFT
    JOINs fair_value_mv alongside fut_players so these come through as
    plain keys, live-current rather than a stale scored-at-the-time
    snapshot."""
    computed_at = d.get("computed_at")
    return {
        "cardId": d.get("card_id"),
        "player": {
            "name": d.get("name") or f"Card {d.get('card_id')}",
            "rating": d.get("rating"),
            "version": d.get("version"),
            "position": d.get("position"),
            "imageUrl": d.get("image_url"),
            "cardBgImage": d.get("card_bg_image"),
            "cardCutoutImage": d.get("card_cutout_image"),
            "cardCutoutType": d.get("card_cutout_type"),
            "cardName": d.get("card_name"),
            # Same bg+cutout+overlay rendering PlayerCardArt already uses
            # on the Player Search/Compare pages - stats/nation-league-club
            # images are only present here so that component's full (non-
            # compact) overlay has real numbers instead of "-" placeholders.
            "stats": {
                "pace": d.get("pace"),
                "shooting": d.get("shooting"),
                "passing": d.get("passing"),
                "dribbling": d.get("dribbling"),
                "defending": d.get("defending"),
                "physicality": d.get("physicality"),
            },
            "nationImage": d.get("nation_image"),
            "leagueImage": d.get("league_image"),
            "clubImage": d.get("club_image"),
        },
        "recommendation": _ACTION_LABEL.get(d.get("recommendation"), "WAIT"),
        "confidence": float(d.get("confidence") or 0),
        "expectedRoi": float(d["expected_roi_pct"]) if d.get("expected_roi_pct") is not None else None,
        "holdingPeriod": f"{d['holding_period_days']}d" if d.get("holding_period_days") else "Unavailable",
        "risk": (d.get("risk_rating") or "medium").capitalize(),
        "currentBin": d.get("current_bin"),
        "fairValue": d.get("fair_value_24h"),
        "sales24h": d.get("sales_24h"),
        "sales7d": d.get("sales_7d"),
        "dataQuality": "SUSPECT" if d.get("data_quality_suspect") else ("GOOD" if d.get("sales_24h") else "LIMITED"),
        "updatedAt": computed_at.isoformat() if hasattr(computed_at, "isoformat") else computed_at,
        "reasoning": d.get("reasoning") or "",
        "marketDrivers": _drivers_to_strings(d.get("market_drivers") or []),
        "historicalSimilarEvents": d.get("similar_events") or [],
        "scores": scores or {},
        "modelVersion": d.get("engine_version"),
    }


def _decode_rec_row(r) -> Dict[str, Any]:
    d = dict(r)
    for key in ("market_drivers", "similar_events", "inputs"):
        if isinstance(d.get(key), str):
            d[key] = json.loads(d[key])
    return d


async def _scores_by_card(conn, card_ids: List[int]) -> Dict[int, Dict[str, float]]:
    if not card_ids:
        return {}
    rows = await conn.fetch(
        "SELECT card_id, score_type, value FROM card_scores_latest WHERE card_id = ANY($1::bigint[]) AND platform = 'ps'",
        card_ids,
    )
    out: Dict[int, Dict[str, float]] = {}
    for r in rows:
        out.setdefault(r["card_id"], {})[r["score_type"]] = float(r["value"])
    return out


@router.get("/dashboard")
async def get_dashboard(request: Request) -> Dict[str, Any]:
    player_pool = await get_player_pool()
    core_pool = await get_core_pool()
    ent = await compute_entitlements(request)
    opportunities_unlocked = "opportunity_feed" in ent["features"]

    # --- Market regime (ungated, same data as GET /api/v2/market/regime) ---
    async with core_pool.acquire() as conn:
        regime_row = await conn.fetchrow(
            "SELECT state, confidence_score, indicators FROM market_states WHERE platform = 'ps' ORDER BY detected_at DESC LIMIT 1"
        )
    if regime_row:
        indicators = regime_row["indicators"]
        if isinstance(indicators, str):
            indicators = json.loads(indicators)
        indicators = indicators or {}
        market_regime = {
            "label": _STATE_LABEL.get(regime_row["state"], regime_row["state"]),
            "confidence": regime_row["confidence_score"],
            "summary": _regime_summary(regime_row["state"], indicators),
            "dataQuality": "GOOD",
            "metrics": {
                "liquidCards": indicators.get("total_cards", 0),
                "avgVolatility": indicators.get("avg_liquidity", 0),
                "avgValueGap": indicators.get("avg_discount_pct", 0),
            },
        }
    else:
        market_regime = {
            "label": "Unknown",
            "confidence": 0,
            "summary": "Not enough live market data has been scored yet.",
            "dataQuality": "LIMITED",
            "metrics": {"liquidCards": 0, "avgVolatility": 0, "avgValueGap": 0},
        }

    # --- Recommendation feeds (gated) ---
    todays_opportunities: List[Dict[str, Any]] = []
    high_confidence: List[Dict[str, Any]] = []
    cards_to_avoid: List[Dict[str, Any]] = []
    recent_predictions: List[Dict[str, Any]] = []

    if opportunities_unlocked:
        async with player_pool.acquire() as conn:
            buy_rows = await conn.fetch(
                """
                SELECT r.*, p.name, p.rating, p.version, p.position, p.image_url,
                       p.card_bg_image, p.card_cutout_image, p.card_cutout_type, p.card_name,
                       p.pace, p.shooting, p.passing, p.dribbling, p.defending, p.physicality,
                       p.nation_image, p.league_image, p.club_image,
                       fv.current_bin, fv.fair_value_24h, fv.sales_24h, fv.sales_7d, fv.data_quality_suspect
                FROM recommendations_latest r
                LEFT JOIN fut_players p ON p.card_id = r.card_id
                LEFT JOIN fair_value_mv fv ON fv.card_id = r.card_id
                WHERE r.recommendation = 'buy'
                ORDER BY r.confidence DESC LIMIT 8
                """
            )
            avoid_rows = await conn.fetch(
                """
                SELECT r.*, p.name, p.rating, p.version, p.position, p.image_url,
                       p.card_bg_image, p.card_cutout_image, p.card_cutout_type, p.card_name,
                       p.pace, p.shooting, p.passing, p.dribbling, p.defending, p.physicality,
                       p.nation_image, p.league_image, p.club_image,
                       fv.current_bin, fv.fair_value_24h, fv.sales_24h, fv.sales_7d, fv.data_quality_suspect
                FROM recommendations_latest r
                LEFT JOIN fut_players p ON p.card_id = r.card_id
                LEFT JOIN fair_value_mv fv ON fv.card_id = r.card_id
                WHERE r.recommendation = 'avoid'
                ORDER BY r.computed_at DESC LIMIT 6
                """
            )
            recent_rows = await conn.fetch(
                """
                SELECT r.*, p.name, p.rating, p.version, p.position, p.image_url,
                       p.card_bg_image, p.card_cutout_image, p.card_cutout_type, p.card_name,
                       p.pace, p.shooting, p.passing, p.dribbling, p.defending, p.physicality,
                       p.nation_image, p.league_image, p.club_image,
                       fv.current_bin, fv.fair_value_24h, fv.sales_24h, fv.sales_7d, fv.data_quality_suspect
                FROM recommendations r
                LEFT JOIN fut_players p ON p.card_id = r.card_id
                LEFT JOIN fair_value_mv fv ON fv.card_id = r.card_id
                ORDER BY r.computed_at DESC LIMIT 8
                """
            )
            all_ids = list({r["card_id"] for r in [*buy_rows, *avoid_rows, *recent_rows]})
            scores_map = await _scores_by_card(conn, all_ids)

        buys = [_decode_rec_row(r) for r in buy_rows]
        todays_opportunities = [_to_recommendation(d, scores_map.get(d["card_id"])) for d in buys]
        high_confidence = [
            _to_recommendation(d, scores_map.get(d["card_id"])) for d in buys if float(d.get("confidence") or 0) >= 70
        ]
        cards_to_avoid = [_to_recommendation(_decode_rec_row(r), scores_map.get(r["card_id"])) for r in avoid_rows]
        recent_predictions = [_to_recommendation(_decode_rec_row(r), scores_map.get(r["card_id"])) for r in recent_rows]

    # --- Biggest movers (ungated - same 3 slots dashboard.py's /stats already computes) ---
    async with player_pool.acquire() as conn:
        movers_row = await conn.fetchrow(
            """
            SELECT
                (SELECT row_to_json(m) FROM (
                    SELECT card_id, name, rating, version, position, volatility_24h AS metric,
                           image_url, card_bg_image, card_cutout_image, card_cutout_type, card_name
                    FROM fair_value_mv WHERE volatility_24h IS NOT NULL ORDER BY volatility_24h DESC LIMIT 1
                ) m) AS largest_mover,
                (SELECT row_to_json(m) FROM (
                    SELECT card_id, name, rating, version, position, sales_24h AS metric,
                           image_url, card_bg_image, card_cutout_image, card_cutout_type, card_name
                    FROM fair_value_mv WHERE sales_24h IS NOT NULL ORDER BY sales_24h DESC LIMIT 1
                ) m) AS most_traded,
                (SELECT row_to_json(m) FROM (
                    SELECT card_id, name, rating, version, position, sales_per_hour_24h AS metric,
                           image_url, card_bg_image, card_cutout_image, card_cutout_type, card_name
                    FROM fair_value_mv WHERE sales_per_hour_24h IS NOT NULL ORDER BY sales_per_hour_24h DESC LIMIT 1
                ) m) AS highest_liquidity
            """
        )
    mover_cards = []
    for key in ("largest_mover", "most_traded", "highest_liquidity"):
        raw = movers_row[key]
        if not raw:
            continue
        m = json.loads(raw) if isinstance(raw, str) else raw
        mover_cards.append(_to_recommendation({
            "card_id": m["card_id"], "name": m["name"], "rating": m["rating"], "version": m["version"],
            "position": m.get("position"),
            "image_url": m["image_url"], "card_bg_image": m.get("card_bg_image"),
            "card_cutout_image": m.get("card_cutout_image"), "card_cutout_type": m.get("card_cutout_type"),
            "card_name": m.get("card_name"),
            # Movers are a price-activity signal, not an AI verdict - only
            # attach a real recommendation if this exact card has one;
            # never invent a BUY/AVOID call a mover didn't earn.
            "recommendation": None, "confidence": 0,
        }))

    # --- Watchlist alerts: real query against alerts_log. Its writer
    # (app/services/watchlist_engine.py) is confirmed dead code (never
    # invoked anywhere in this repo), so this is honestly almost always
    # empty today - querying for real rather than hardcoding [] means it
    # starts working the moment that engine is ever wired up, with zero
    # further change here. ---
    watchlist_alerts: List[Dict[str, Any]] = []
    uid = request.session.get("user_id")
    if uid:
        try:
            async for wdb in get_watchlist_db():
                rows = await wdb.fetch(
                    "SELECT direction, pct, price, sent_at FROM alerts_log WHERE user_id = $1 ORDER BY sent_at DESC LIMIT 5",
                    str(uid),
                )
                for r in rows:
                    watchlist_alerts.append({
                        "severity": "high" if abs(float(r["pct"])) >= 15 else "medium",
                        "title": f"{'Price rising' if r['direction'] == 'rise' else 'Price falling'} {abs(float(r['pct'])):.1f}%",
                        "message": f"Now {int(r['price']):,} coins",
                    })
                break
        except Exception:
            watchlist_alerts = []

    # --- Latest market events + SBC impact ---
    async with player_pool.acquire() as conn:
        event_rows = await conn.fetch(
            "SELECT id, kind, source, title, starts_at, ends_at, first_seen_at FROM market_events ORDER BY first_seen_at DESC LIMIT 6"
        )
        impact_rows = await conn.fetch(
            """
            SELECT e.id AS event_id, e.title, e.fingerprint,
                   AVG(i.price_change_pct) AS avg_impact, COUNT(*) AS n
            FROM event_market_impact i
            JOIN market_events e ON e.id = i.event_id
            WHERE e.kind = 'sbc'
            GROUP BY e.id, e.title, e.fingerprint
            ORDER BY MAX(i.computed_at) DESC
            LIMIT 5
            """
        )

    latest_market_events = [
        {
            "id": r["id"], "kind": r["kind"], "source": r["source"], "title": r["title"],
            "startsAt": r["starts_at"].isoformat() if r["starts_at"] else None,
            "endsAt": r["ends_at"].isoformat() if r["ends_at"] else None,
        }
        for r in event_rows
    ]
    latest_sbc_impact = [
        {
            "eventId": r["event_id"],
            "title": r["title"],
            "fingerprints": list(r["fingerprint"] or []),
            "estimatedMarketImpact": round(float(r["avg_impact"] or 0), 1),
            # Real, data-derived proxy (more measured cards = more
            # confidence), not an arbitrary number - capped at 95 since a
            # handful of card measurements is never "certain."
            "confidence": min(95, int(r["n"]) * 20),
        }
        for r in impact_rows
    ]

    return {
        "marketRegime": market_regime,
        "todaysOpportunities": todays_opportunities,
        "highConfidenceInvestments": high_confidence,
        "cardsToAvoid": cards_to_avoid,
        "biggestMovers": mover_cards,
        "recentAiPredictions": recent_predictions,
        "watchlistAlerts": watchlist_alerts,
        "latestMarketEvents": latest_market_events,
        "latestSbcImpact": latest_sbc_impact,
        "locked": {"opportunityFeed": not opportunities_unlocked},
    }
