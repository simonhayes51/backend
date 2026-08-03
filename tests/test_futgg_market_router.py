# tests/test_futgg_market_router.py
#
# HTTP-layer tests for app/routers/v2/futgg_market.py, using FastAPI's
# dependency_overrides to swap in a fake FutggMarketDataProvider instead
# of a real database connection - same "no DB needed" bar
# tests/test_v2_health_router.py already set for v2 routers, extended
# here to a router that actually depends on data (via get_provider()'s
# override point).
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.v2.futgg_market import get_provider, router as futgg_router
from app.services.market_data_provider import PlayerFilters

AS_OF = datetime.now(timezone.utc)


def _row(**overrides) -> Dict[str, Any]:
    defaults = dict(
        source_card_id=1, source_player_id=100, source_slug="messi",
        source_url="http://x/1", game_year=26, name="Lionel Messi",
        rating=91, primary_position="RW", alternate_positions=["ST"],
        rarity="gold_rare", squad=None, price_tier="gold_rare",
        club="Inter Miami", league="MLS", nation="Argentina",
        club_source_id=1, league_source_id=1, nation_source_id=1,
        player_image_url=None, card_design_image_url=None, club_image_url=None,
        league_image_url=None, nation_image_url=None,
        is_active=True, is_tradeable=True, last_price_status="success",
        metadata_warnings=[], price_updated_at=AS_OF, next_price_due_at=None,
        current_bin=50000, price_range_low=49000, price_range_high=51000,
        bin_source_age_text="4 minutes ago", bin_captured_at=AS_OF - timedelta(minutes=4),
        sales_count=30, sales_median=52000, sales_trimmed_mean=52000,
        sales_low=48000, sales_high=56000, sales_stddev=800,
        latest_sale_price=52500, latest_sale_at=AS_OF - timedelta(minutes=2),
        sales_window_earliest_at=AS_OF - timedelta(hours=6),
        sales_window_latest_at=AS_OF - timedelta(minutes=2),
        sales_window_span_minutes=358.0, sales_dispersion_ratio=0.015,
        snapshot_computed_at=AS_OF,
    )
    defaults.update(overrides)
    return defaults


class FakeProvider:
    def __init__(self, rows: Optional[List[Dict[str, Any]]] = None):
        self.rows = rows if rows is not None else [_row()]
        # Records the order_by every search_players() call was actually
        # made with, so tests can assert on candidate-scan ordering
        # without needing a real Postgres EXPLAIN.
        self.search_calls: List[Dict[str, Any]] = []

    async def get_player(self, card_id: int):
        for r in self.rows:
            if r["source_card_id"] == card_id:
                return r
        return None

    async def get_current_price(self, card_id: int, platform=None):
        row = await self.get_player(card_id)
        if row is None or row.get("current_bin") is None:
            return None
        return {
            "lowest_bin": row["current_bin"], "price_range_low": row["price_range_low"],
            "price_range_high": row["price_range_high"], "source_age_text": row["bin_source_age_text"],
            "captured_at": row["bin_captured_at"],
        }

    async def get_recent_sales(self, card_id: int, limit: int = 50):
        return [
            {
                "sold_price": 52000, "ea_tax": 2600, "net_price": 49400,
                "approximate_sold_at": AS_OF - timedelta(minutes=3),
                "source_age_text": "3 minutes ago", "source_age_seconds": 180,
            }
        ]

    async def get_price_history(self, card_id: int, period=None):
        return [
            {
                "lowest_bin": 50000, "price_range_low": 49000, "price_range_high": 51000,
                "source_age_text": "4 minutes ago", "captured_at": AS_OF - timedelta(minutes=4),
            }
        ]

    async def search_players(self, filters: PlayerFilters, *, limit=25, offset=0, order_by="rating DESC NULLS LAST"):
        self.search_calls.append({"limit": limit, "offset": offset, "order_by": order_by})
        return self.rows[offset: offset + limit]

    async def count_players(self, filters: PlayerFilters) -> int:
        return len(self.rows)

    async def get_freshness_summary(self):
        return {
            "heartbeats": [
                {"worker": "futgg_player_sync", "last_run_at": AS_OF, "ok": True, "detail": None},
                {"worker": "futgg_price_sync", "last_run_at": AS_OF, "ok": True, "detail": None},
            ],
            "cards_discovered": 10, "cards_priced": 8, "cards_no_market": 1,
            "cards_untradeable": 1, "cards_stale": 0, "stale_by_price_tier": {},
            "latest_source_errors": [],
        }


def _client(rows: Optional[List[Dict[str, Any]]] = None) -> TestClient:
    app = FastAPI()
    app.include_router(futgg_router, prefix="/api/v2")
    app.dependency_overrides[get_provider] = lambda: FakeProvider(rows)
    return TestClient(app)


def test_list_players_shape():
    resp = _client().get("/api/v2/players")
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body and "total" in body
    assert body["items"][0]["name"] == "Lionel Messi"


def test_get_player_detail_shape():
    resp = _client().get("/api/v2/players/1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "futgg"
    assert body["player"]["source_card_id"] == 1
    assert "intelligence" in body
    assert body["intelligence"]["signal"] in (
        "strong_buy", "buy", "watch", "hold", "sell", "avoid", "insufficient_data",
    )


def test_get_player_detail_404_for_unknown_card():
    resp = _client().get("/api/v2/players/999")
    assert resp.status_code == 404


def test_sales_endpoint_marks_time_as_approximate():
    resp = _client().get("/api/v2/players/1/sales")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"][0]["approximate"] is True
    assert "approximate_sold_at" in body["items"][0]
    assert "not an exact" in body["note"]


def test_prices_endpoint_shape():
    resp = _client().get("/api/v2/players/1/prices")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"][0]["lowest_bin"] == 50000


def test_market_freshness_shape():
    resp = _client().get("/api/v2/market/freshness")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cards_discovered"] == 10
    assert len(body["heartbeats"]) == 2


def test_opportunities_only_returns_buy_signals():
    strong_row = _row(source_card_id=2, current_bin=40000, sales_median=55000, sales_trimmed_mean=55000, sales_count=40)
    weak_row = _row(source_card_id=3, current_bin=60000, sales_median=52000, sales_trimmed_mean=52000)
    resp = _client([strong_row, weak_row]).get("/api/v2/opportunities")
    assert resp.status_code == 200
    body = resp.json()
    for item in body["items"]:
        assert item["intelligence"]["signal"] in ("buy", "strong_buy")


def test_trade_finder_rejects_bad_sort_by():
    resp = _client().get("/api/v2/trade-finder?sort_by=not_a_real_sort")
    assert resp.status_code == 400


def test_trade_finder_shape():
    strong_row = _row(source_card_id=2, current_bin=40000, sales_median=55000, sales_trimmed_mean=55000, sales_count=40)
    resp = _client([strong_row]).get("/api/v2/trade-finder?sort_by=roi")
    assert resp.status_code == 200
    body = resp.json()
    assert body["sort_by"] == "roi"


class TestCandidateScanOrderedBySalesCount:
    """Regression test: CANDIDATE_SCAN_LIMIT's own module comment says the
    500-row candidate scan behind /players (intelligence-filtered),
    /opportunities, and /trade-finder considers "the top CANDIDATE_SCAN_LIMIT
    rows (by sales_count, as a liquidity-first proxy)" - but the three
    search_players() call sites never actually passed order_by, so they
    silently used search_players's own default ("rating DESC NULLS LAST")
    instead. On a database with more than 500 tradeable cards, that meant
    the candidate pool was the top-500 *highest-rated* cards, not the
    top-500 *most-liquid* ones - a genuinely liquid, profitable mid/low-rated
    opportunity could never appear on /opportunities or /trade-finder at
    all, silently, with no error to notice."""

    def test_players_with_intelligence_filter_scans_by_sales_count(self):
        client = _client([_row()])
        resp = client.get("/api/v2/players?min_roi=0.01")
        assert resp.status_code == 200

    def test_opportunities_scans_candidates_by_sales_count(self):
        app = FastAPI()
        app.include_router(futgg_router, prefix="/api/v2")
        provider = FakeProvider([_row()])
        app.dependency_overrides[get_provider] = lambda: provider
        client = TestClient(app)
        resp = client.get("/api/v2/opportunities")
        assert resp.status_code == 200
        assert provider.search_calls, "search_players was never called"
        assert provider.search_calls[-1]["order_by"] == "sales_count DESC NULLS LAST"

    def test_trade_finder_scans_candidates_by_sales_count(self):
        app = FastAPI()
        app.include_router(futgg_router, prefix="/api/v2")
        provider = FakeProvider([_row()])
        app.dependency_overrides[get_provider] = lambda: provider
        client = TestClient(app)
        resp = client.get("/api/v2/trade-finder")
        assert resp.status_code == 200
        assert provider.search_calls[-1]["order_by"] == "sales_count DESC NULLS LAST"

    def test_players_list_with_intelligence_filter_scans_by_sales_count(self):
        app = FastAPI()
        app.include_router(futgg_router, prefix="/api/v2")
        provider = FakeProvider([_row()])
        app.dependency_overrides[get_provider] = lambda: provider
        client = TestClient(app)
        # Any intelligence-derived filter (min_roi/min_confidence/etc.)
        # routes /players through the same candidate-scan path.
        resp = client.get("/api/v2/players?min_roi=0.01")
        assert resp.status_code == 200
        assert provider.search_calls[-1]["order_by"] == "sales_count DESC NULLS LAST"


class TestTradeFinderNewestSortDoesNotCrash:
    """Regression test for a live 500: sort_by=newest sorted on
    `row.get("price_updated_at") or ""` - the moment the candidate set had
    both a row with a real datetime and a row with price_updated_at=None
    (a card discovered but not yet priced), Python's sort raised
    `TypeError: '<' not supported between instances of 'str' and
    'datetime.datetime'` comparing the "" placeholder against a real
    datetime, crashing the whole request."""

    def test_newest_sort_with_mixed_null_and_real_timestamps(self):
        priced_row = _row(source_card_id=2, price_updated_at=AS_OF)
        unpriced_row = _row(source_card_id=3, price_updated_at=None, current_bin=40000,
                             sales_median=55000, sales_trimmed_mean=55000, sales_count=40)
        resp = _client([priced_row, unpriced_row]).get("/api/v2/trade-finder?sort_by=newest")
        assert resp.status_code == 200

    def test_newest_sort_puts_real_timestamp_before_null(self):
        older = _row(source_card_id=2, price_updated_at=AS_OF - timedelta(days=1),
                     current_bin=40000, sales_median=55000, sales_trimmed_mean=55000, sales_count=40)
        unpriced = _row(source_card_id=3, price_updated_at=None, current_bin=40000,
                        sales_median=55000, sales_trimmed_mean=55000, sales_count=40)
        resp = _client([older, unpriced]).get("/api/v2/trade-finder?sort_by=newest")
        assert resp.status_code == 200
        ids = [item["source_card_id"] for item in resp.json()["items"]]
        # A card with an actual price timestamp is "newer" than one that
        # was never priced at all - it must never sort behind the null.
        assert ids.index(2) < ids.index(3)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
