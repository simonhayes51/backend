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
from app.services.player_card_ondemand import ensure_cards_requested

router = APIRouter(tags=["v2-dashboard"])

# Plain, FUT-trader-facing labels rather than stock-market jargon
# (bullish/bearish/illiquid) - this product's audience trades FUT cards,
# not stocks, and isn't assumed to know those terms.
_STATE_LABEL = {"bullish": "Good Time to Buy", "bearish": "Prices Dropping", "illiquid": "Slow Trading", "normal": "Steady Market"}

# Recommendation Engine V1.2 (recommendation_engine_v2.py) writes a real
# status enum - BUY/WAIT/SELL/AVOID/INSUFFICIENT_DATA - straight onto
# `status`. This is now the source of truth; the legacy lowercase
# `recommendation` column (still written, see _legacy_recommendation())
# is kept only for callers that haven't migrated off the deprecated
# shape yet.
_STATUS_LABEL = {"BUY": "BUY", "WAIT": "WAIT", "SELL": "SELL", "AVOID": "AVOID", "INSUFFICIENT_DATA": "INSUFFICIENT_DATA"}

# Per-strategy nominal holding period, shown once a card actually
# qualifies for that strategy (see strategy_config.py) - never a single
# global "holding period" figure. First match in this order wins when a
# card qualifies for more than one.
_STRATEGY_HOLDING_LABEL = {
    "quick_flip": "~24h", "swing_trade": "~48h", "low_risk": "Flexible",
    "lazy_buyer": "Flexible", "sbc": "Flexible", "long_hold": "~7d",
}

_FACTOR_LABEL = {
    "discount_vs_fair_value": lambda v: f"Trading {float(v):.1f}% below its real 24h median.",
    "liquidity_sales_per_hour": lambda v: f"{float(v):.1f} sales/hour liquidity.",
    "trend_falling": lambda v: "Price is in a confirmed downward trend, not a discount.",
}


def _holding_period_label(qualified_strategies: List[str]) -> str:
    for name in _STRATEGY_HOLDING_LABEL:
        if name in (qualified_strategies or []):
            return _STRATEGY_HOLDING_LABEL[name]
    return "Unavailable"


def _risk_label(score_risk: Optional[float]) -> str:
    if score_risk is None:
        return "Unknown"
    v = float(score_risk)
    if v < 0.33:
        return "Low"
    if v < 0.66:
        return "Medium"
    return "High"


def _reasoning_text(status: Optional[str], qualified_strategies: List[str], failed_gate_reasons: List[str], held_decision_reasons: List[str]) -> str:
    """Short, honest reasoning derived from the real V1.2 decision
    fields - reasoning/market_drivers on the recommendations row itself
    are legacy rule_v1 columns the V1.2 engine never populates (see
    recommendation_engine_v2.py's _persist()), so a blank string there
    would silently look like a bug rather than "no engine wrote this
    column." Never claims more than the reason codes actually say."""
    if status == "BUY":
        names = ", ".join(s.replace("_", " ") for s in qualified_strategies) or "a strategy"
        return f"Qualifies for: {names}."
    if status == "SELL":
        names = "; ".join(r.replace("_", " ").lower() for r in held_decision_reasons)
        return names or "Selling now looks better than continuing to hold."
    if status == "AVOID":
        return "The likely outcome is a net loss after EA's sale tax."
    if status == "INSUFFICIENT_DATA":
        if failed_gate_reasons:
            return "Missing: " + ", ".join(r.replace("_", " ").lower() for r in failed_gate_reasons) + "."
        return "Not enough live market data yet."
    if status == "WAIT":
        return "Doesn't clear any strategy's threshold yet."
    return ""


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
        return f"Tracked cards are selling for {avg_discount:.1f}% less than usual, and only {falling_pct:.0f}% are still dropping in price - good time to pick some up."
    if state == "bearish":
        return f"{falling_pct:.0f}% of tracked cards are still dropping in price right now - a cheap price might drop even more before it's worth buying."
    if state == "illiquid":
        return f"Cards are only selling about {avg_liquidity:.2f} times an hour on average - it'll take longer than usual to buy or sell."
    return "Prices and sales are behaving normally across tracked cards right now."


def _to_recommendation(d: Dict[str, Any], scores: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """Reshapes a recommendations_latest row (+ optional card_scores) into
    the frontend's Recommendation contract. current_bin/fair_value_24h/
    sales_24h/sales_7d aren't columns on recommendations/
    recommendations_latest themselves - every query building `d` LEFT
    JOINs fair_value_mv alongside fut_players so these come through as
    plain keys, live-current rather than a stale scored-at-the-time
    snapshot.

    `status` (BUY/WAIT/SELL/AVOID/INSUFFICIENT_DATA) is the real V1.2
    decision; `d` may lack it entirely for synthetic rows that were never
    evaluated (e.g. the "biggest movers" cards below, which are a raw
    price-activity signal, not an AI verdict) - `has_evaluation` guards
    every field that would otherwise fabricate an opinion out of absent
    data."""
    computed_at = d.get("computed_at")
    status = d.get("status")
    has_evaluation = status is not None
    qualified_strategies = d.get("qualified_strategies") or []
    failed_gate_reasons = d.get("failed_gate_reasons") or []
    held_decision_reasons = d.get("held_decision_reasons") or []

    def pct(field: str) -> Optional[float]:
        v = d.get(field)
        return float(v) * 100 if v is not None else None

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
            "generatedCardUrl": d.get("generated_card_url"),
            "generatedCardStatus": d.get("generated_card_status"),
            "generatedCardFlagged": d.get("generated_card_flagged"),
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
        "status": status,
        "recommendation": _STATUS_LABEL.get(status, "WAIT") if has_evaluation else None,
        "confidence": float(d.get("confidence") or 0),
        # Deprecated alias kept for callers still on the pre-V1.2 shape -
        # always the after-tax likely-case ROI now (see
        # recommendation_engine_v2.py's _persist()), never the old
        # pre-tax discount-as-profit figure. New callers should read
        # netRoi.likely / netRoi.conservative instead.
        "expectedRoi": pct("likely_net_roi"),
        "netRoi": {
            "conservative": pct("conservative_net_roi"),
            "likely": pct("likely_net_roi"),
            "bullish": pct("bullish_net_roi"),
            "potential": pct("potential_net_roi"),
        },
        # Null with an explicit source string until a validated ML model
        # is promoted - never a fabricated/heuristic prediction (see
        # migration 024 + trading_math.py's module docstring).
        "expectedNetRoi": pct("expected_net_roi"),
        "expectedNetRoiSource": d.get("expected_net_roi_source"),
        "entryPrice": d.get("entry_price"),
        "breakEvenPrice": d.get("break_even_sale_price"),
        "holdingPeriod": _holding_period_label(qualified_strategies) if has_evaluation else "Unavailable",
        "risk": _risk_label(d.get("score_risk")),
        "qualifiedStrategies": qualified_strategies,
        "failedGateReasons": failed_gate_reasons,
        "currentBin": d.get("current_bin"),
        "fairValue": d.get("fair_value_24h"),
        "sales24h": d.get("sales_24h"),
        "sales7d": d.get("sales_7d"),
        "dataQuality": "SUSPECT" if d.get("data_quality_suspect") else ("GOOD" if d.get("sales_24h") else "LIMITED"),
        "updatedAt": computed_at.isoformat() if hasattr(computed_at, "isoformat") else computed_at,
        "reasoning": _reasoning_text(status, qualified_strategies, failed_gate_reasons, held_decision_reasons) if has_evaluation else "",
        "marketDrivers": _drivers_to_strings(d.get("market_drivers") or []),
        "historicalSimilarEvents": d.get("similar_events") or [],
        "isHeld": bool(d.get("is_held")),
        "heldDecision": d.get("held_decision"),
        "heldDecisionReasons": held_decision_reasons,
        "purchasePrice": d.get("purchase_price"),
        "scores": scores or {},
        "modelVersion": d.get("engine_version"),
    }


def _decode_rec_row(r) -> Dict[str, Any]:
    d = dict(r)
    for key in (
        "market_drivers", "similar_events", "inputs",
        "qualified_strategies", "strategy_results", "failed_gate_reasons", "held_decision_reasons",
    ):
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

    # --- Recommendation feeds (gated, with a free preview) ---
    # Free/Pro users used to get an all-empty feed with nothing to show
    # the AI actually works - the one thing a free tier needs to prove
    # before anyone upgrades. FREE_PREVIEW_COUNT real BUY calls are now
    # always included (never a fabricated example); PREVIEW_LOCKED_COUNT
    # exists purely so the frontend can render "N more, upgrade to see
    # them" instead of the feed just looking short.
    FREE_PREVIEW_COUNT = 1
    todays_opportunities: List[Dict[str, Any]] = []
    high_confidence: List[Dict[str, Any]] = []
    cards_to_avoid: List[Dict[str, Any]] = []
    recent_predictions: List[Dict[str, Any]] = []
    preview_locked_count = 0

    async with player_pool.acquire() as conn:
        buy_rows = await conn.fetch(
            """
            SELECT r.*, p.name, p.rating, p.version, p.position, p.image_url,
                   p.card_bg_image, p.card_cutout_image, p.card_cutout_type, p.card_name,
                   p.generated_card_url, p.generated_card_status, p.generated_card_flagged,
                   p.pace, p.shooting, p.passing, p.dribbling, p.defending, p.physicality,
                   p.nation_image, p.league_image, p.club_image,
                   fv.current_bin, fv.fair_value_24h, fv.sales_24h, fv.sales_7d, fv.data_quality_suspect
            FROM recommendations_latest r
            LEFT JOIN fut_players p ON p.card_id = r.card_id
            LEFT JOIN fair_value_mv fv ON fv.card_id = r.card_id
            WHERE r.status = 'BUY'
            ORDER BY r.confidence DESC NULLS LAST LIMIT 8
            """
        )
        avoid_rows = await conn.fetch(
            """
            SELECT r.*, p.name, p.rating, p.version, p.position, p.image_url,
                   p.card_bg_image, p.card_cutout_image, p.card_cutout_type, p.card_name,
                   p.generated_card_url, p.generated_card_status, p.generated_card_flagged,
                   p.pace, p.shooting, p.passing, p.dribbling, p.defending, p.physicality,
                   p.nation_image, p.league_image, p.club_image,
                   fv.current_bin, fv.fair_value_24h, fv.sales_24h, fv.sales_7d, fv.data_quality_suspect
            FROM recommendations_latest r
            LEFT JOIN fut_players p ON p.card_id = r.card_id
            LEFT JOIN fair_value_mv fv ON fv.card_id = r.card_id
            WHERE r.status = 'AVOID'
            ORDER BY r.computed_at DESC LIMIT 6
            """
        )
        recent_rows = await conn.fetch(
            """
            SELECT r.*, p.name, p.rating, p.version, p.position, p.image_url,
                   p.card_bg_image, p.card_cutout_image, p.card_cutout_type, p.card_name,
                   p.generated_card_url, p.generated_card_status, p.generated_card_flagged,
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
    all_opportunities = [_to_recommendation(d, scores_map.get(d["card_id"])) for d in buys]
    all_high_confidence = [
        _to_recommendation(d, scores_map.get(d["card_id"])) for d in buys if float(d.get("confidence") or 0) >= 70
    ]

    if opportunities_unlocked:
        todays_opportunities = all_opportunities
        high_confidence = all_high_confidence
        cards_to_avoid = [_to_recommendation(_decode_rec_row(r), scores_map.get(r["card_id"])) for r in avoid_rows]
        recent_predictions = [_to_recommendation(_decode_rec_row(r), scores_map.get(r["card_id"])) for r in recent_rows]
    else:
        # Free/Pro preview: a real BUY call, not a fabricated example, so
        # the free tier can actually see the product work before paying
        # for the full feed - see `locked.previewCount` below for what's
        # still behind the paywall.
        todays_opportunities = all_opportunities[:FREE_PREVIEW_COUNT]
        preview_locked_count = max(0, len(all_opportunities) - FREE_PREVIEW_COUNT)

    needs_card = [
        str(r["card_id"]) for r in [*buy_rows, *avoid_rows, *recent_rows]
        if r["generated_card_status"] != "ready" or r["generated_card_flagged"]
    ]
    if needs_card:
        await ensure_cards_requested(player_pool, list(dict.fromkeys(needs_card)))

    # --- Biggest movers (ungated - same 3 slots dashboard.py's /stats already computes) ---
    async with player_pool.acquire() as conn:
        movers_row = await conn.fetchrow(
            """
            SELECT
                (SELECT row_to_json(m) FROM (
                    SELECT card_id, name, rating, version, position, volatility_24h AS metric,
                           image_url, card_bg_image, card_cutout_image, card_cutout_type, card_name,
                           generated_card_url, generated_card_status, generated_card_flagged
                    FROM fair_value_mv WHERE volatility_24h IS NOT NULL ORDER BY volatility_24h DESC LIMIT 1
                ) m) AS largest_mover,
                (SELECT row_to_json(m) FROM (
                    SELECT card_id, name, rating, version, position, sales_24h AS metric,
                           image_url, card_bg_image, card_cutout_image, card_cutout_type, card_name,
                           generated_card_url, generated_card_status, generated_card_flagged
                    FROM fair_value_mv WHERE sales_24h IS NOT NULL ORDER BY sales_24h DESC LIMIT 1
                ) m) AS most_traded,
                (SELECT row_to_json(m) FROM (
                    SELECT card_id, name, rating, version, position, sales_per_hour_24h AS metric,
                           image_url, card_bg_image, card_cutout_image, card_cutout_type, card_name,
                           generated_card_url, generated_card_status, generated_card_flagged
                    FROM fair_value_mv WHERE sales_per_hour_24h IS NOT NULL ORDER BY sales_per_hour_24h DESC LIMIT 1
                ) m) AS highest_liquidity
            """
        )
    mover_cards = []
    movers_needing_cards: List[str] = []
    for key in ("largest_mover", "most_traded", "highest_liquidity"):
        raw = movers_row[key]
        if not raw:
            continue
        m = json.loads(raw) if isinstance(raw, str) else raw
        if m.get("generated_card_status") != "ready" or m.get("generated_card_flagged"):
            movers_needing_cards.append(str(m["card_id"]))
        mover_cards.append(_to_recommendation({
            "card_id": m["card_id"], "name": m["name"], "rating": m["rating"], "version": m["version"],
            "position": m.get("position"),
            "image_url": m["image_url"], "card_bg_image": m.get("card_bg_image"),
            "card_cutout_image": m.get("card_cutout_image"), "card_cutout_type": m.get("card_cutout_type"),
            "card_name": m.get("card_name"),
            "generated_card_url": m.get("generated_card_url"),
            "generated_card_status": m.get("generated_card_status"),
            "generated_card_flagged": m.get("generated_card_flagged"),
            # Movers are a price-activity signal, not an AI verdict - no
            # "status" key means _to_recommendation()'s has_evaluation
            # guard renders recommendation=None rather than inventing a
            # BUY/AVOID call a mover didn't earn.
        }))
    if movers_needing_cards:
        await ensure_cards_requested(player_pool, list(dict.fromkeys(movers_needing_cards)))

    # --- Watchlist alerts: real query against alerts_log, populated by
    # main.py's own live alert loop (_alerts_poll_loop/_eval_alerts_for_pair
    # against the watchlist_alerts table) - NOT app/services/
    # watchlist_engine.py, which is separate, unrelated dead code against
    # a differently-named watchlist_items table that nothing ever writes
    # to (see that module's own header comment). ---
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
        "locked": {
            "opportunityFeed": not opportunities_unlocked,
            "previewCount": preview_locked_count,
        },
    }
