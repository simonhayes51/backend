# app/services/event_impact.py
#
# Computes event_market_impact rows (migration 019) from market_events +
# sbc_details/sbc_challenges + real bin_history/sales_history snapshots.
# Runs as a scheduled background pass, same advisory-lock-guarded shape
# as app/services/fair_value.py's refresher_loop - never called from the
# collector, which only knows what it scraped, not how prices moved
# afterward.
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import asyncpg

log = logging.getLogger("event_impact")

REFRESH_LOCK_KEY = 7741005  # distinct from fair-value (7741002), migration runner (7741003),
                              # core-bootstrap consolidation N/A, analytics engine (7741004)


async def _bin_before_after(conn: asyncpg.Connection, card_id: int, starts_at: Optional[datetime]) -> tuple[Optional[int], Optional[int], Optional[datetime], Optional[datetime]]:
    """Closest PS BIN snapshot at/before the event's start, and the most
    recent snapshot since - real timestamped history, not a guess."""
    before = await conn.fetchrow(
        """
        SELECT lowest_bin, captured_at FROM bin_history
        WHERE player_id = $1 AND platform = 'ps' AND lowest_bin IS NOT NULL
          AND ($2::timestamptz IS NULL OR captured_at <= $2)
        ORDER BY captured_at DESC LIMIT 1
        """,
        card_id, starts_at,
    )
    after = await conn.fetchrow(
        """
        SELECT lowest_bin, captured_at FROM bin_history
        WHERE player_id = $1 AND platform = 'ps' AND lowest_bin IS NOT NULL
        ORDER BY captured_at DESC LIMIT 1
        """,
        card_id,
    )
    return (
        before["lowest_bin"] if before else None,
        after["lowest_bin"] if after else None,
        before["captured_at"] if before else None,
        after["captured_at"] if after else None,
    )


async def _sales_volume_24h(conn: asyncpg.Connection, card_id: int, around: Optional[datetime]) -> int:
    if around is None:
        return 0
    row = await conn.fetchval(
        """
        SELECT COUNT(*) FROM sales_history
        WHERE player_id = $1 AND sold_at >= $2::timestamptz - INTERVAL '24 hours' AND sold_at < $2::timestamptz
        """,
        card_id, around,
    )
    return int(row or 0)


def _pct_change(before: Optional[int], after: Optional[int]) -> Optional[float]:
    if not before or after is None:
        return None
    return round(100.0 * (after - before) / before, 2)


async def _upsert_impact(
    conn: asyncpg.Connection, event_id: int, card_id: int, relation: str,
    price_before: Optional[int], price_after: Optional[int],
    measured_before_at: Optional[datetime], measured_after_at: Optional[datetime],
    volume_before_24h: int, volume_after_24h: int,
) -> None:
    await conn.execute(
        """
        INSERT INTO event_market_impact (
            event_id, card_id, relation, price_before, price_after, price_change_pct,
            volume_before_24h, volume_after_24h, measured_before_at, measured_after_at, computed_at
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10, now())
        ON CONFLICT (event_id, card_id, relation) DO UPDATE SET
            price_before = EXCLUDED.price_before,
            price_after = EXCLUDED.price_after,
            price_change_pct = EXCLUDED.price_change_pct,
            volume_before_24h = EXCLUDED.volume_before_24h,
            volume_after_24h = EXCLUDED.volume_after_24h,
            measured_before_at = EXCLUDED.measured_before_at,
            measured_after_at = EXCLUDED.measured_after_at,
            computed_at = now()
        """,
        event_id, card_id, relation, price_before, price_after,
        _pct_change(price_before, price_after),
        volume_before_24h, volume_after_24h, measured_before_at, measured_after_at,
    )


async def compute_event_impact(player_pool: asyncpg.Pool) -> int:
    """One pass over every market_events row, computing/refreshing its
    event_market_impact rows. Returns the number of (event, card, relation)
    rows written."""
    written = 0
    async with player_pool.acquire() as conn:
        events = await conn.fetch(
            "SELECT id, kind, starts_at, payload FROM market_events WHERE kind IN ('sbc', 'promo')"
        )
        for ev in events:
            event_id, starts_at = ev["id"], ev["starts_at"]

            if ev["kind"] == "promo":
                # A promo event has no single reward/target card the way an
                # SBC does - auto_sync/promo_event_detector.py's own cluster
                # of newly-discovered cards ARE the affected set. There's no
                # meaningful "before" BIN for a card that didn't exist
                # before this event (before-lookup correctly returns None,
                # not a fabricated value), but price/volume trend since
                # release is still a real, useful signal.
                payload = ev["payload"]
                if isinstance(payload, str):
                    payload = json.loads(payload)
                for card_id in (payload or {}).get("card_ids", []):
                    card_id = int(card_id)
                    pb, pa, mb, ma = await _bin_before_after(conn, card_id, starts_at)
                    vb = await _sales_volume_24h(conn, card_id, starts_at)
                    va = await _sales_volume_24h(conn, card_id, datetime.now(timezone.utc))
                    await _upsert_impact(conn, event_id, card_id, "promo_card", pb, pa, mb, ma, vb, va)
                    written += 1
                continue

            # reward_supply: structural - the SBC's own reward card, if any.
            # A new supply of that exact card enters the market on
            # completion, which is a real, unambiguous demand-side effect.
            details = await conn.fetchrow(
                "SELECT reward_card_id FROM sbc_details WHERE event_id = $1", event_id
            )
            if details and details["reward_card_id"]:
                card_id = details["reward_card_id"]
                pb, pa, mb, ma = await _bin_before_after(conn, card_id, starts_at)
                vb = await _sales_volume_24h(conn, card_id, starts_at)
                va = await _sales_volume_24h(conn, card_id, datetime.now(timezone.utc))
                await _upsert_impact(conn, event_id, card_id, "reward_supply", pb, pa, mb, ma, vb, va)
                written += 1

            # requirement_target: structural, but only when a challenge's
            # requirements JSONB names a specific card (rare - most SBC
            # requirements are rating/chemistry/league constraints, not a
            # named player). Only computed when that explicit signal exists.
            challenges = await conn.fetch(
                "SELECT requirements FROM sbc_challenges WHERE event_id = $1", event_id
            )
            for ch in challenges:
                req = ch["requirements"] or {}
                target_card_id = req.get("required_card_id") if isinstance(req, dict) else None
                if target_card_id:
                    card_id = int(target_card_id)
                    pb, pa, mb, ma = await _bin_before_after(conn, card_id, starts_at)
                    vb = await _sales_volume_24h(conn, card_id, starts_at)
                    va = await _sales_volume_24h(conn, card_id, datetime.now(timezone.utc))
                    await _upsert_impact(conn, event_id, card_id, "requirement_target", pb, pa, mb, ma, vb, va)
                    written += 1

            # fodder_demand / meta_shift: this per-event, per-card impact
            # relation is still not computed here - a real per-card fodder-
            # demand row would need to know every card genuinely eligible
            # for an SBC's rating/chemistry bands, not just its nation/
            # league. The aggregate nation/league version of this signal
            # (how many currently-live SBCs name a given nation/league) IS
            # computed now, in app/services/sbc_demand.py, and wired into
            # dashboard.py's BUY reasoning directly - that's a coarser but
            # honest version of this same gap, closed without pretending to
            # the per-card rigor the two structural relations above have.

    return written


async def refresher_loop(player_pool: asyncpg.Pool, interval_seconds: int = 1800) -> None:
    """Background task started from the app lifespan. Same advisory-lock
    pattern as fair_value.refresher_loop."""
    await asyncio.sleep(5)
    while True:
        try:
            async with player_pool.acquire() as conn:
                got = await conn.fetchval("SELECT pg_try_advisory_lock($1)", REFRESH_LOCK_KEY)
                if got:
                    try:
                        n = await compute_event_impact(player_pool)
                        if n:
                            log.info("event_market_impact: wrote %d rows", n)
                    finally:
                        await conn.execute("SELECT pg_advisory_unlock($1)", REFRESH_LOCK_KEY)
        except Exception as e:  # never let the loop die
            log.error("event_impact refresher iteration failed: %s", e)
        await asyncio.sleep(interval_seconds)
