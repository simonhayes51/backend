# app/services/futgg_scanner.py
"""
The evaluation pass: segmented candidate selection -> trend-aware
evaluation -> persisted recommendation snapshots.

This is what closes the loop between the pieces. Each pass:

  1. Selects candidates across all liquidity segments, so the long tail
     is evaluated rather than being crowded out by the same few hundred
     high-volume cards winning the ordering every time.
  2. Batch-fetches raw sales for the whole candidate set in one query,
     so the trend layer gets real per-sale rows without N+1 round trips.
  3. Evaluates each card with its sales series attached.
  4. Freezes every actionable result as a recommendation snapshot, which
     is what the outcome grader will later grade.

Metrics are collected per pass and per segment, because "is the long tail
actually being evaluated" is a question that needs an answer in
production, not an assumption in a docstring.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.services.futgg_candidate_segments import MAX_TOTAL_CANDIDATES, segment_summary
from app.services.futgg_intelligence import evaluate_card
from app.services.futgg_recommendation_store import record_many

log = logging.getLogger("futgg_scanner")


@dataclass
class SegmentMetrics:
    name: str
    candidates: int = 0
    evaluated: int = 0
    recommendations: int = 0
    buy_signals: int = 0
    watch_signals: int = 0
    blocked_by_trend: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ScanMetrics:
    started_at: datetime
    duration_seconds: float = 0.0
    unique_cards_evaluated: int = 0
    total_player_pool: int = 0
    pool_coverage_pct: Optional[float] = None
    recommendations_written: int = 0
    segments: List[SegmentMetrics] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "duration_seconds": round(self.duration_seconds, 2),
            "unique_cards_evaluated": self.unique_cards_evaluated,
            "total_player_pool": self.total_player_pool,
            "pool_coverage_pct": self.pool_coverage_pct,
            "recommendations_written": self.recommendations_written,
            "segments": [s.as_dict() for s in self.segments],
        }


async def run_scan(provider, pool, *, persist: bool = True) -> ScanMetrics:
    """Run one full evaluation pass.

    `persist=False` runs the whole pipeline without writing snapshots -
    used by the metrics endpoint and by staging, so coverage can be
    inspected without polluting the outcome-grading corpus.
    """
    started = time.monotonic()
    metrics = ScanMetrics(started_at=datetime.now(timezone.utc))

    by_segment = await provider.select_candidates_by_segment()

    # De-duplicate across segments, remembering which segment first
    # surfaced each card so per-segment attribution stays meaningful.
    seen: Dict[int, str] = {}
    unique_rows: Dict[int, Dict[str, Any]] = {}
    segment_metrics: Dict[str, SegmentMetrics] = {}

    for segment_name, rows in by_segment.items():
        sm = SegmentMetrics(name=segment_name, candidates=len(rows))
        segment_metrics[segment_name] = sm
        for row in rows:
            card_id = int(row["source_card_id"])
            if card_id not in seen and len(unique_rows) < MAX_TOTAL_CANDIDATES:
                seen[card_id] = segment_name
                unique_rows[card_id] = row

    card_ids = list(unique_rows)
    sales_by_card = await provider.get_sales_by_ids(card_ids)

    as_of = datetime.now(timezone.utc)
    to_persist: List[Tuple[Dict[str, Any], Any]] = []

    for card_id, row in unique_rows.items():
        segment_name = seen[card_id]
        sm = segment_metrics[segment_name]
        try:
            ci = evaluate_card(row, as_of=as_of, sales=sales_by_card.get(card_id, []))
        except Exception:
            # One malformed row must never abort a whole pass.
            log.warning("evaluate_card failed for card_id=%s", card_id, exc_info=True)
            continue
        sm.evaluated += 1
        if ci.signal in ("buy", "strong_buy"):
            sm.buy_signals += 1
            sm.recommendations += 1
            to_persist.append((row, ci))
        elif ci.signal == "watch":
            sm.watch_signals += 1
            to_persist.append((row, ci))
        if ci.trend_state in ("falling_knife", "downtrend") and ci.signal == "avoid":
            sm.blocked_by_trend += 1

    if persist and to_persist:
        metrics.recommendations_written = await record_many(pool, to_persist)

    metrics.unique_cards_evaluated = len(unique_rows)
    metrics.segments = list(segment_metrics.values())

    try:
        async with pool.acquire() as conn:
            total = await conn.fetchval(
                "SELECT count(*) FROM futgg_players WHERE is_active AND is_tradeable IS DISTINCT FROM FALSE"
            )
        metrics.total_player_pool = int(total or 0)
        if metrics.total_player_pool:
            metrics.pool_coverage_pct = round(
                100.0 * metrics.unique_cards_evaluated / metrics.total_player_pool, 2
            )
    except Exception:
        log.warning("failed to compute player-pool coverage", exc_info=True)

    metrics.duration_seconds = time.monotonic() - started
    return metrics


async def coverage_report(pool) -> Dict[str, Any]:
    """Evaluation-coverage metrics for the internal dashboard.

    Answers the questions the segmented scan exists to make answerable:
    how much of the pool are we actually looking at, how stale is the
    least-recently-evaluated card, and are recommendations coming from
    across the liquidity range or only from the top of it.
    """
    async with pool.acquire() as conn:
        pool_size = await conn.fetchval(
            "SELECT count(*) FROM futgg_players WHERE is_active AND is_tradeable IS DISTINCT FROM FALSE"
        )
        evaluated_24h = await conn.fetchval(
            """
            SELECT count(DISTINCT source_card_id)
            FROM futgg_recommendation_snapshots
            WHERE evaluated_at >= now() - interval '24 hours'
            """
        )
        avg_staleness = await conn.fetchval(
            """
            SELECT avg(EXTRACT(EPOCH FROM (now() - last_seen)) / 60.0)
            FROM (
                SELECT source_card_id, max(evaluated_at) AS last_seen
                FROM futgg_recommendation_snapshots
                GROUP BY source_card_id
            ) t
            """
        )
        by_liquidity = await conn.fetch(
            """
            SELECT
                CASE
                    WHEN s.sales_count >= 25 THEN 'liquid'
                    WHEN s.sales_count >= 8 THEN 'medium'
                    WHEN s.sales_count > 0 THEN 'low'
                    ELSE 'none'
                END AS bucket,
                count(*) AS evaluations,
                count(*) FILTER (WHERE s.signal IN ('buy','strong_buy')) AS recommendations
            FROM futgg_recommendation_snapshots s
            WHERE s.evaluated_at >= now() - interval '24 hours'
            GROUP BY 1
            ORDER BY 1
            """
        )

    return {
        "player_pool": int(pool_size or 0),
        "cards_evaluated_24h": int(evaluated_24h or 0),
        "pool_coverage_24h_pct": (
            round(100.0 * int(evaluated_24h or 0) / int(pool_size), 2) if pool_size else None
        ),
        "avg_minutes_since_last_evaluation": (
            round(float(avg_staleness), 1) if avg_staleness is not None else None
        ),
        "by_liquidity_segment": [
            {
                "segment": row["bucket"],
                "evaluations": int(row["evaluations"]),
                "recommendations": int(row["recommendations"]),
            }
            for row in by_liquidity
        ],
        "segment_config": segment_summary(),
    }


async def refresher_loop(pool, poll_seconds: int = 600) -> None:
    """Periodic evaluation pass.

    Without this loop the whole outcome pipeline is inert: nothing calls
    evaluate_card() outside a user request, so no recommendation snapshot
    is ever frozen and the grader has nothing to grade. The engine would
    keep producing correct answers that nobody ever checks - which is the
    exact failure the outcome loop exists to end.

    Deliberately tolerant of a database where migration 040 has not run
    yet: the backend boots and serves fine without these tables, it just
    records nothing until they exist.
    """
    from app.services.market_data_provider import FutggMarketDataProvider

    await asyncio.sleep(45)  # let migrations and the snapshot refresh land first
    provider = FutggMarketDataProvider(pool)
    while True:
        try:
            metrics = await run_scan(provider, pool)
            log.info(
                "futgg scan: evaluated=%d coverage=%s%% recommendations=%d in %.1fs",
                metrics.unique_cards_evaluated,
                metrics.pool_coverage_pct,
                metrics.recommendations_written,
                metrics.duration_seconds,
            )
        except Exception:
            log.warning("futgg scan pass failed", exc_info=True)
        await asyncio.sleep(poll_seconds)
