"""Regression checks for rotating FUT.GG scan selection and persistence."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

from app.services import futgg_scanner
from app.services.market_data_provider import FutggMarketDataProvider, PlayerFilters
from app.services.futgg_candidate_segments import SEGMENTS


class FakeConnection:
    def __init__(self):
        self.queries = []
        self.batches = []

    async def fetch(self, sql, *args):
        self.queries.append((sql, args))
        return []

    async def executemany(self, sql, values):
        self.batches.append((sql, values))

    async def fetchval(self, sql):
        return 10


class FakePool:
    def __init__(self):
        self.conn = FakeConnection()

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def test_segment_cooldowns_and_oldest_first():
    pool = FakePool()
    provider = FutggMarketDataProvider(pool)
    result = asyncio.run(provider.select_candidates_by_segment())

    assert set(result) == {segment.name for segment in SEGMENTS}
    assert len(pool.conn.queries) == len(SEGMENTS)
    for (sql, args), segment in zip(pool.conn.queries, SEGMENTS):
        assert "LEFT JOIN futgg_scan_state USING (source_card_id)" in sql
        assert "last_evaluated_at IS NULL" in sql
        assert "ORDER BY last_evaluated_at ASC NULLS FIRST" in sql
        assert args == (segment.refresh_minutes, segment.limit)


def test_all_signals_are_recorded_in_rotation_state():
    pool = FakePool()
    provider = FutggMarketDataProvider(pool)
    now = datetime.now(timezone.utc)
    evaluations = [(1, "hold", "downtrend", 12_000), (2, "avoid", "falling_knife", 7_000)]
    asyncio.run(provider.record_scan_evaluations(evaluations, now))

    assert len(pool.conn.batches) == 1
    sql, values = pool.conn.batches[0]
    assert "ON CONFLICT (source_card_id)" in sql
    assert values == [(1, now, "hold", "downtrend", 12_000), (2, now, "avoid", "falling_knife", 7_000)]


def test_scanner_buy_lookup_preserves_market_filters_and_recent_state():
    pool = FakePool()
    provider = FutggMarketDataProvider(pool)
    asyncio.run(provider.get_scanner_buy_candidates(PlayerFilters(max_price=25_000), limit=30))
    sql, args = pool.conn.queries[0]
    assert "JOIN futgg_scan_state USING (source_card_id)" in sql
    assert "last_signal IN ('buy', 'strong_buy')" in sql
    assert "last_evaluated_at >= now() - interval '3 hours'" in sql
    assert "current_bin <= $1" in sql
    assert args == (25_000, 30)


def test_scanner_records_non_actionable_evaluation(monkeypatch):
    class Provider:
        def __init__(self):
            self.recorded = []

        async def select_candidates_by_segment(self):
            return {"test": [{"source_card_id": 42, "current_bin": 5_000}]}

        async def get_sales_by_ids(self, ids):
            return {}

        async def record_scan_evaluations(self, evaluations, timestamp):
            self.recorded.extend(evaluations)

    monkeypatch.setattr(
        futgg_scanner, "evaluate_card",
        lambda *a, **kw: SimpleNamespace(signal="hold", trend_state="downtrend"),
    )
    provider = Provider()
    metrics = asyncio.run(futgg_scanner.run_scan(provider, FakePool()))

    assert metrics.unique_cards_evaluated == 1
    assert metrics.recommendations_written == 0
    assert provider.recorded == [(42, "hold", "downtrend", 5_000)]


def test_read_only_scan_does_not_advance_rotation(monkeypatch):
    class Provider:
        async def select_candidates_by_segment(self):
            return {"test": [{"source_card_id": 42, "current_bin": 5_000}]}

        async def get_sales_by_ids(self, ids):
            return {}

        async def record_scan_evaluations(self, *args):
            raise AssertionError("read-only scans must not advance state")

    monkeypatch.setattr(
        futgg_scanner, "evaluate_card",
        lambda *a, **kw: SimpleNamespace(signal="hold", trend_state="flat"),
    )
    asyncio.run(futgg_scanner.run_scan(Provider(), FakePool(), persist=False))
