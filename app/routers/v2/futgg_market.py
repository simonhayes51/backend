# app/routers/v2/futgg_market.py
"""
FUT.GG-backed v2 market-intelligence endpoints. All under the v2_router's
own /api/v2 prefix (see app/routers/v2/__init__.py) - final paths are
/api/v2/players, /api/v2/players/{id}, /api/v2/players/{id}/prices,
/api/v2/players/{id}/sales, /api/v2/opportunities, /api/v2/trade-finder,
/api/v2/market/freshness.

Deliberately does NOT collide with v1's /api/players or v1's
/api/trade-finder (app/routers/players.py, app/routers/trade_finder.py)
- those stay exactly as they are, backed by fut_players/FUTBIN. This
router is entirely new surface, backed entirely by
app/services/market_data_provider.py + app/services/futgg_intelligence.py,
themselves reading only futgg_players/futgg_bin_history/
futgg_sales_history/futgg_market_snapshot (migrations/038) - never the
legacy fut_players/sales_history/bin_history tables.

Ungated (no entitlement check): this is new provider-neutral surface,
not a replacement for any existing gated feature - mirrors the precedent
already set by GET /api/v2/market/regime (see app/routers/v2/market.py's
own comment on why that route is free). If/when product wants a teaser/
paid split here, follow app/routers/fair_value.py's _teaser() convention
on top of this router rather than inside the provider/intelligence layers.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.db import get_core_pool
from app.services.futgg_intelligence import (
    DEFAULT_MAX_ACCEPTABLE_PRICE_AGE_MINUTES,
    MAX_ACCEPTABLE_PRICE_AGE_MINUTES_BY_TIER,
    CardIntelligence,
    evaluate_card,
)
from app.services.market_data_provider import (
    MAX_RECENT_SALES,
    FutggMarketDataProvider,
    PlayerFilters,
)

router = APIRouter(tags=["v2-futgg-market"])
log = logging.getLogger("futgg_market")

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100


def _is_stale_for_tier(price_age_minutes: Optional[int], price_tier: Optional[str]) -> bool:
    if price_age_minutes is None:
        return True
    threshold = MAX_ACCEPTABLE_PRICE_AGE_MINUTES_BY_TIER.get(price_tier, DEFAULT_MAX_ACCEPTABLE_PRICE_AGE_MINUTES)
    return price_age_minutes > threshold

# Bound on how many snapshot rows /opportunities and /trade-finder ever
# pull from Postgres before scoring in Python - the intelligence layer
# has no SQL equivalent (fair value/confidence/signal are computed in
# Python, not the view), so those two endpoints can't push their
# ROI/confidence/signal filters all the way into the SQL WHERE clause.
# Capping the candidate scan keeps the request bounded regardless of how
# large futgg_market_snapshot grows, at the cost of only ever considering
# the top CANDIDATE_SCAN_LIMIT rows (by sales_count, as a liquidity-first
# proxy for "cards worth scoring at all") per request.
CANDIDATE_SCAN_LIMIT = 500

# The candidate-scan calls below must pass this explicitly: search_players's
# own default order_by ("rating DESC NULLS LAST") is a different ordering
# entirely, and silently falling back to it here would candidate-limit
# /players (intelligence-filtered), /opportunities, and /trade-finder to
# the top-rated 500 cards rather than the top-500-most-liquid cards the
# comment above documents - systematically hiding genuinely liquid,
# profitable mid/low-rated opportunities behind a wall of high-rated but
# possibly illiquid/rarely-traded cards, with no error or symptom other
# than "why does this card never show up as an opportunity".
CANDIDATE_SCAN_ORDER_BY = "sales_count DESC NULLS LAST"


async def get_provider() -> FutggMarketDataProvider:
    """FastAPI dependency - lazily creates (and caches, via
    app.db.get_core_pool()'s own module-level cache) the shared asyncpg
    pool keyed off DATABASE_URL. A plain Depends rather than a module-
    level singleton so tests can override it with a fake provider via
    app.dependency_overrides[get_provider], with no real DB involved."""
    pool = await get_core_pool()
    return FutggMarketDataProvider(pool)


ProviderDep = Depends(get_provider)


def _serialize_intelligence(ci: CardIntelligence) -> Dict[str, Any]:
    return {
        "fair_value": int(ci.fair_value) if ci.fair_value is not None else None,
        "recommended_buy_max": int(ci.recommended_buy_max) if ci.recommended_buy_max is not None else None,
        "recommended_sell_target": int(ci.recommended_sell_target) if ci.recommended_sell_target is not None else None,
        "expected_profit_after_tax": float(ci.expected_profit_after_tax) if ci.expected_profit_after_tax is not None else None,
        "expected_roi": float(ci.expected_roi) if ci.expected_roi is not None else None,
        "liquidity_score": ci.liquidity_score,
        "confidence_score": ci.confidence_score,
        "risk_level": ci.risk_level,
        "signal": ci.signal,
        "signal_reasons": ci.signal_reasons,
        "price_age_minutes": ci.price_age_minutes,
        "sales_sample_size": ci.sales_sample_size,
        "sales_window_span_minutes": ci.sales_window_span_minutes,
    }


def _serialize_player(row: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(row)
    bin_captured_at = out.get("bin_captured_at")
    # Frontend-facing aliases (current_bin_captured_at / price_age_seconds)
    # alongside the raw column names - computed here, not duplicated in SQL,
    # since "now" has to be evaluated at serialization time either way.
    out["current_bin_captured_at"] = bin_captured_at.isoformat() if bin_captured_at else None
    out["price_age_seconds"] = (
        int((datetime.now(timezone.utc) - bin_captured_at).total_seconds())
        if bin_captured_at is not None else None
    )
    # image_url alias: the frontend's card-image rendering (Players/
    # Opportunities/Trade Finder result rows) reads a generic `image_url`
    # field regardless of source - player_image_url is the FUT.GG-specific
    # column name (see futgg_players schema).
    out["image_url"] = out.get("player_image_url")
    for key in ("price_updated_at", "next_price_due_at", "bin_captured_at",
                "latest_sale_at", "sales_window_earliest_at",
                "sales_window_latest_at", "snapshot_computed_at"):
        if out.get(key) is not None:
            out[key] = out[key].isoformat()
    return out


def _item_with_intelligence(row: Dict[str, Any], ci: CardIntelligence) -> Dict[str, Any]:
    """Flattens the CardIntelligence fields onto the player dict (what the
    frontend reads directly, e.g. item.recommended_buy_max) while also
    keeping them nested under "intelligence" for any consumer that prefers
    the grouped shape."""
    intelligence = _serialize_intelligence(ci)
    return {**_serialize_player(row), **intelligence, "intelligence": intelligence}


def _resolve_rating_bounds(
    rating: Optional[int], rating_min: Optional[int], rating_max: Optional[int],
) -> tuple[Optional[int], Optional[int]]:
    """`rating` is a single-value convenience alias (exact match) some
    callers use instead of the rating_min/rating_max pair."""
    if rating is not None:
        return rating, rating
    return rating_min, rating_max


def _filters_from_query(
    search: Optional[str], rating_min: Optional[int], rating_max: Optional[int],
    position: Optional[str], rarity: Optional[str], club: Optional[str],
    league: Optional[str], nation: Optional[str], max_price: Optional[int],
    min_price: Optional[int], max_price_age_minutes: Optional[int],
    tradeable_only: bool = True,
) -> PlayerFilters:
    return PlayerFilters(
        search=search, rating_min=rating_min, rating_max=rating_max,
        position=position, rarity=rarity, club=club, league=league, nation=nation,
        max_price=max_price, min_price=min_price,
        max_price_age_minutes=max_price_age_minutes, tradeable_only=tradeable_only,
    )


@router.get("/players")
async def list_players(
    search: Optional[str] = Query(None, description="Diacritic-insensitive name search"),
    rating: Optional[int] = Query(None, ge=0, le=99, description="Single-value exact-rating alias for rating_min=rating_max"),
    rating_min: Optional[int] = Query(None, ge=0, le=99),
    rating_max: Optional[int] = Query(None, ge=0, le=99),
    position: Optional[str] = None,
    rarity: Optional[str] = None,
    club: Optional[str] = None,
    league: Optional[str] = None,
    nation: Optional[str] = None,
    max_price: Optional[int] = Query(None, ge=0, description="Affordability filter: current BIN <= this"),
    min_price: Optional[int] = Query(None, ge=0),
    max_price_age_minutes: Optional[int] = Query(None, ge=0, description="Freshness filter"),
    max_price_age: Optional[int] = Query(None, ge=0, description="Alias for max_price_age_minutes"),
    risk: Optional[str] = Query(None, description="low|medium|high|avoid - computed, applied post-fetch"),
    min_expected_profit: Optional[float] = Query(None),
    min_profit: Optional[float] = Query(None, description="Alias for min_expected_profit"),
    min_roi: Optional[float] = Query(None, description="Minimum expected net ROI, e.g. 0.05 for 5%"),
    min_confidence: Optional[float] = Query(None, ge=0, le=1),
    min_liquidity: Optional[float] = Query(None, ge=0, le=1),
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    provider: FutggMarketDataProvider = ProviderDep,
) -> Dict[str, Any]:
    rating_min, rating_max = _resolve_rating_bounds(rating, rating_min, rating_max)
    max_price_age_minutes = max_price_age_minutes if max_price_age_minutes is not None else max_price_age
    min_expected_profit = min_expected_profit if min_expected_profit is not None else min_profit
    filters = _filters_from_query(
        search, rating_min, rating_max, position, rarity, club, league, nation,
        max_price, min_price, max_price_age_minutes,
    )

    needs_intelligence = any(
        v is not None for v in (risk, min_expected_profit, min_roi, min_confidence, min_liquidity)
    )

    if not needs_intelligence:
        total = await provider.count_players(filters)
        rows = await provider.search_players(
            filters, limit=page_size, offset=(page - 1) * page_size,
        )
        items = [_serialize_player(r) for r in rows]
        return {"items": items, "page": page, "page_size": page_size, "total": total}

    # Any intelligence-derived filter requires scoring candidates in
    # Python first (see CANDIDATE_SCAN_LIMIT), so pagination happens
    # after scoring+filtering rather than pushing LIMIT/OFFSET into SQL.
    rows = await provider.search_players(filters, limit=CANDIDATE_SCAN_LIMIT, offset=0, order_by=CANDIDATE_SCAN_ORDER_BY)
    scored = _score_and_filter(
        rows, risk=risk, min_expected_profit=min_expected_profit,
        min_roi=min_roi, min_confidence=min_confidence, min_liquidity=min_liquidity,
    )
    total = len(scored)
    window = scored[(page - 1) * page_size: page * page_size]
    items = [_item_with_intelligence(row, ci) for row, ci in window]
    return {"items": items, "page": page, "page_size": page_size, "total": total}


def _score_and_filter(
    rows: List[Dict[str, Any]], *, risk: Optional[str] = None,
    min_expected_profit: Optional[float] = None, min_roi: Optional[float] = None,
    min_confidence: Optional[float] = None, min_liquidity: Optional[float] = None,
    signals: Optional[set] = None,
) -> List[Any]:
    out = []
    for row in rows:
        ci = evaluate_card(row)
        if signals is not None and ci.signal not in signals:
            continue
        if risk is not None and ci.risk_level != risk:
            continue
        if min_expected_profit is not None and (
            ci.expected_profit_after_tax is None or float(ci.expected_profit_after_tax) < min_expected_profit
        ):
            continue
        if min_roi is not None and (ci.expected_roi is None or float(ci.expected_roi) < min_roi):
            continue
        if min_confidence is not None and (ci.confidence_score is None or ci.confidence_score < min_confidence):
            continue
        if min_liquidity is not None and (ci.liquidity_score is None or ci.liquidity_score < min_liquidity):
            continue
        out.append((row, ci))
    return out


@router.get("/players/{card_id}")
async def get_player_detail(
    card_id: int, provider: FutggMarketDataProvider = ProviderDep,
) -> Dict[str, Any]:
    row = await provider.get_player(card_id)
    if row is None:
        raise HTTPException(404, "Card not found in the FUT.GG-backed market layer")

    ci = evaluate_card(row)
    if _is_stale_for_tier(ci.price_age_minutes, row.get("price_tier")):
        # A user actually opening this card is the strongest "someone
        # might act on this" signal short of it already being a surfaced
        # opportunity - re-queue it for the price worker's next pass
        # instead of leaving it to wait out its tier's normal interval.
        # Best-effort: a transient DB hiccup here must never break the
        # page load itself.
        try:
            await provider.bump_price_priority(card_id)
        except Exception:
            log.warning("bump_price_priority failed for card_id=%s", card_id, exc_info=True)
    # Flattened onto the top level (frontend reads e.g. data.current_bin
    # directly) as well as under "player"/"intelligence" for consumers
    # that prefer the grouped shape.
    return {
        "source": "futgg",
        **_item_with_intelligence(row, ci),
        "player": _serialize_player(row),
        "intelligence": _serialize_intelligence(ci),
    }


@router.get("/players/{card_id}/prices")
async def get_player_prices(
    card_id: int,
    period: Optional[str] = Query(None, description="1h|6h|24h|1d|7d|14d|30d - omit for full history"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    provider: FutggMarketDataProvider = ProviderDep,
) -> Dict[str, Any]:
    rows = await provider.get_price_history(card_id, period=period)
    total = len(rows)
    window = rows[(page - 1) * page_size: page * page_size]
    items = [
        {
            "lowest_bin": r["lowest_bin"],
            "price_range_low": r["price_range_low"],
            "price_range_high": r["price_range_high"],
            "source_age_text": r["source_age_text"],
            "captured_at": r["captured_at"].isoformat(),
        }
        for r in window
    ]
    return {"card_id": card_id, "items": items, "page": page, "page_size": page_size, "total": total}


@router.get("/players/{card_id}/sales")
async def get_player_sales(
    card_id: int,
    limit: int = Query(MAX_RECENT_SALES, ge=1, le=MAX_RECENT_SALES),
    provider: FutggMarketDataProvider = ProviderDep,
) -> Dict[str, Any]:
    rows = await provider.get_recent_sales(card_id, limit=limit)
    items = [
        {
            "sold_price": r["sold_price"],
            "ea_tax": r["ea_tax"],
            "net_price": r["net_price"],
            "age_text": r["source_age_text"],
            "approximate_sold_at": r["approximate_sold_at"].isoformat(),
            "approximate": True,  # never present approximate_sold_at as exact
            "source_age_seconds": r["source_age_seconds"],
        }
        for r in rows
    ]
    return {
        "card_id": card_id,
        "items": items,
        "count": len(items),
        "note": "approximate_sold_at is derived from a relative age string (e.g. '4 minutes ago') at scrape time, not an exact EA transaction timestamp.",
    }


@router.get("/opportunities")
async def list_opportunities(
    rating: Optional[int] = Query(None, ge=0, le=99),
    rating_min: Optional[int] = Query(None, ge=0, le=99),
    rating_max: Optional[int] = Query(None, ge=0, le=99),
    position: Optional[str] = None,
    rarity: Optional[str] = None,
    club: Optional[str] = None,
    league: Optional[str] = None,
    nation: Optional[str] = None,
    max_price: Optional[int] = Query(None, ge=0),
    min_price: Optional[int] = Query(None, ge=0),
    max_price_age_minutes: Optional[int] = Query(None, ge=0),
    max_price_age: Optional[int] = Query(None, ge=0, description="Alias for max_price_age_minutes"),
    risk: Optional[str] = Query(None, description="low|medium|high|avoid - exact match"),
    min_profit: Optional[float] = Query(None),
    min_roi: Optional[float] = Query(None),
    min_confidence: Optional[float] = Query(None, ge=0, le=1),
    min_liquidity: Optional[float] = Query(None, ge=0, le=1),
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    provider: FutggMarketDataProvider = ProviderDep,
) -> Dict[str, Any]:
    rating_min, rating_max = _resolve_rating_bounds(rating, rating_min, rating_max)
    max_price_age_minutes = max_price_age_minutes if max_price_age_minutes is not None else max_price_age
    filters = _filters_from_query(
        None, rating_min, rating_max, position, rarity, club, league, nation,
        max_price, min_price, max_price_age_minutes,
    )
    rows = await provider.search_players(filters, limit=CANDIDATE_SCAN_LIMIT, offset=0, order_by=CANDIDATE_SCAN_ORDER_BY)
    scored = _score_and_filter(
        rows, risk=risk, min_expected_profit=min_profit, min_roi=min_roi,
        min_confidence=min_confidence, min_liquidity=min_liquidity,
        signals={"buy", "strong_buy"},
    )
    # Sort by opportunity strength: strong_buy first, then by expected ROI.
    scored.sort(
        key=lambda pair: (
            0 if pair[1].signal == "strong_buy" else 1,
            -(float(pair[1].expected_roi) if pair[1].expected_roi is not None else 0.0),
        )
    )
    total = len(scored)
    window = scored[(page - 1) * page_size: page * page_size]
    items = [_item_with_intelligence(row, ci) for row, ci in window]
    return {"items": items, "page": page, "page_size": page_size, "total": total}


_SORT_ALIASES = {"best": "best_opportunity"}

_TRADE_FINDER_SORTS = {
    "best_opportunity": lambda row_ci: (
        0 if row_ci[1].signal == "strong_buy" else (1 if row_ci[1].signal == "buy" else 2),
        -(row_ci[1].confidence_score or 0.0) * float(row_ci[1].expected_roi or 0),
    ),
    "profit": lambda row_ci: -(float(row_ci[1].expected_profit_after_tax) if row_ci[1].expected_profit_after_tax is not None else 0.0),
    "roi": lambda row_ci: -(float(row_ci[1].expected_roi) if row_ci[1].expected_roi is not None else 0.0),
    "confidence": lambda row_ci: -(row_ci[1].confidence_score or 0.0),
    "liquidity": lambda row_ci: -(row_ci[1].liquidity_score or 0.0),
    "newest": lambda row_ci: row_ci[0].get("price_updated_at") is None,  # placeholder tiebreak, refined below
    "freshest": lambda row_ci: (row_ci[1].price_age_minutes if row_ci[1].price_age_minutes is not None else float("inf")),
}


@router.get("/trade-finder")
async def trade_finder(
    budget: Optional[int] = Query(None, ge=0, description="Max current BIN"),
    min_profit: Optional[float] = Query(None),
    min_roi: Optional[float] = Query(None),
    risk_tolerance: Optional[str] = Query(None, description="low|medium|high - includes that risk level and better"),
    risk: Optional[str] = Query(None, description="Alias for risk_tolerance"),
    min_confidence: Optional[float] = Query(None, ge=0, le=1),
    position: Optional[str] = None,
    rarity: Optional[str] = None,
    rating: Optional[int] = Query(None, ge=0, le=99),
    rating_min: Optional[int] = Query(None, ge=0, le=99),
    rating_max: Optional[int] = Query(None, ge=0, le=99),
    min_liquidity: Optional[float] = Query(None, ge=0, le=1),
    max_price_age_minutes: Optional[int] = Query(None, ge=0),
    max_price_age: Optional[int] = Query(None, ge=0, description="Alias for max_price_age_minutes"),
    sort_by: str = Query("best_opportunity", description="best_opportunity|profit|roi|confidence|liquidity|newest|freshest"),
    sort: Optional[str] = Query(None, description="Alias for sort_by; also accepts 'best' for 'best_opportunity'"),
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    provider: FutggMarketDataProvider = ProviderDep,
) -> Dict[str, Any]:
    if sort is not None:
        sort_by = _SORT_ALIASES.get(sort, sort)
    if sort_by not in _TRADE_FINDER_SORTS:
        raise HTTPException(400, f"sort_by must be one of {sorted(_TRADE_FINDER_SORTS)}")
    risk_tolerance = risk_tolerance if risk_tolerance is not None else risk
    rating_min, rating_max = _resolve_rating_bounds(rating, rating_min, rating_max)
    max_price_age_minutes = max_price_age_minutes if max_price_age_minutes is not None else max_price_age

    filters = _filters_from_query(
        None, rating_min, rating_max, position, rarity, None, None, None,
        budget, None, max_price_age_minutes,
    )
    rows = await provider.search_players(filters, limit=CANDIDATE_SCAN_LIMIT, offset=0, order_by=CANDIDATE_SCAN_ORDER_BY)

    risk_order = {"low": 0, "medium": 1, "high": 2, "avoid": 3}
    max_risk_rank = risk_order.get(risk_tolerance, 2) if risk_tolerance else None

    scored = _score_and_filter(
        rows, min_expected_profit=min_profit, min_roi=min_roi,
        min_confidence=min_confidence, min_liquidity=min_liquidity,
        signals={"buy", "strong_buy"},
    )
    if max_risk_rank is not None:
        scored = [(row, ci) for row, ci in scored if risk_order.get(ci.risk_level, 3) <= max_risk_rank]

    if sort_by == "newest":
        # price_updated_at is nullable (a card can be discovered before its
        # first price sync completes) - `or ""` as the None-fallback used
        # to crash the whole request with a 500 the moment any row in the
        # candidate scan had a real datetime AND any other row had None
        # (`TypeError: '<' not supported between instances of 'str' and
        # 'datetime.datetime'`), since Python won't compare a str sentinel
        # against a datetime. datetime.min (tz-aware, to compare against
        # the tz-aware column) sorts a missing timestamp to the oldest
        # position instead, which is also the semantically correct
        # "least new" ordering for reverse=True.
        scored.sort(
            key=lambda pair: pair[0].get("price_updated_at") or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
    else:
        scored.sort(key=_TRADE_FINDER_SORTS[sort_by])

    total = len(scored)
    window = scored[(page - 1) * page_size: page * page_size]
    items = [_item_with_intelligence(row, ci) for row, ci in window]
    return {"items": items, "page": page, "page_size": page_size, "total": total, "sort_by": sort_by}


@router.get("/market/freshness")
async def market_freshness(
    provider: FutggMarketDataProvider = ProviderDep,
) -> Dict[str, Any]:
    summary = await provider.get_freshness_summary()
    heartbeats = []
    for h in summary["heartbeats"]:
        heartbeats.append({
            "worker": h["worker"],
            "last_run_at": h["last_run_at"].isoformat() if h["last_run_at"] else None,
            "ok": h["ok"],
            "detail": h["detail"],
        })
    return {
        "heartbeats": heartbeats,
        "cards_discovered": summary["cards_discovered"],
        "cards_priced": summary["cards_priced"],
        "cards_no_market": summary["cards_no_market"],
        "cards_untradeable": summary["cards_untradeable"],
        "cards_stale": summary["cards_stale"],
        "stale_by_price_tier": summary["stale_by_price_tier"],
        "latest_source_errors": summary["latest_source_errors"],
    }
