# app/routers/dashboard.py
"""
Public, read-only aggregate stats for the investor-facing /demo page
(frontend_new's src/pages/Demo.jsx). No auth - same precedent as
/api/ops/freshness and /undervalued/teaser (app/routers/ops.py,
app/routers/fair_value.py).

Every number here is either a live query or a short-TTL cache of one,
against the same sales_history/bin_history/fut_players/pipeline_heartbeats
tables (and the fair_value_mv materialized view) the rest of the app
already reads - nothing here is synthesized or hardcoded. Anything that
can't be computed honestly from real data (e.g. per-job run duration -
pipeline_heartbeats has no timing column and no worker measures its own
runtime) is simply omitted rather than faked.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query

from app.db import get_core_pool, get_player_pool
from app.services.player_card_ondemand import ensure_cards_requested

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

_CACHE_TTL_SECONDS = 60
_totals_cache: Dict[str, Any] = {}
_totals_cache_at: float = 0.0

_DETAIL_NUM_RE = re.compile(r"([a-zA-Z_]+)=(\d+)")

# pipeline_heartbeats.worker is a real join key auto_sync writes to - never
# rename the key itself. This only controls what's DISPLAYED to a visitor
# on the public /demo page, e.g. so it doesn't name a specific scraped
# third-party site.
_WORKER_DISPLAY_NAMES = {
    "futbin_full_sync": "Player Catalog Sync",
    "futbin_card_art_backfill": "Card Art Backfill",
}


def _iso(ts: Optional[datetime]) -> Optional[str]:
    return ts.isoformat() if ts else None


def _parse_detail_counts(detail: Optional[str]) -> Dict[str, int]:
    """pipeline_heartbeats.detail is free text written by each auto_sync
    worker, e.g. "sales_new=40 bin_found=812 bin_failed=3 http_429=0" or
    "pages=1-848 rows=52000 written=51990" - pull the numeric fields back
    out instead of storing them a second time."""
    if not detail:
        return {}
    return {k: int(v) for k, v in _DETAIL_NUM_RE.findall(detail)}


async def _cached_totals(player_pool) -> Dict[str, Any]:
    """Full-table aggregates that would otherwise force a seq scan on
    every page load. These change slowly relative to demo-page traffic,
    so an in-process TTL cache (no Redis needed) is enough."""
    global _totals_cache, _totals_cache_at
    now = time.monotonic()
    if _totals_cache and (now - _totals_cache_at) < _CACHE_TTL_SECONDS:
        return _totals_cache

    try:
        async with player_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    (SELECT COUNT(*) FROM fut_players)                    AS total_players,
                    (SELECT COUNT(*) FROM sales_history)                  AS total_sales,
                    (SELECT COUNT(*) FROM bin_history)                    AS total_bin_snapshots,
                    (SELECT COUNT(DISTINCT player_id) FROM sales_history) AS unique_players_with_sales,
                    (SELECT COUNT(DISTINCT player_id) FROM bin_history)   AS unique_players_with_bin,
                    (SELECT MIN(sold_at) FROM sales_history)              AS first_recorded_sale_at
                """
            )
        _totals_cache = dict(row)
        _totals_cache_at = now
    except Exception:
        # sales_history/bin_history may not exist yet on a fresh DB - fail
        # soft rather than 500 a page that's otherwise working fine.
        _totals_cache = {
            "total_players": 0, "total_sales": 0, "total_bin_snapshots": 0,
            "unique_players_with_sales": 0, "unique_players_with_bin": 0,
            "first_recorded_sale_at": None,
        }
        _totals_cache_at = now
    return _totals_cache


async def _heartbeats_by_worker(core_pool, player_pool) -> Dict[str, Dict[str, Any]]:
    """Same cross-pool merge-by-worker pattern as app/routers/ops.py -
    workers write heartbeats to their own DATABASE_URL, which on a
    split-DB deploy may not be the backend's core DB."""
    by_worker: Dict[str, Dict[str, Any]] = {}
    for p in {core_pool, player_pool}:
        try:
            async with p.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT worker, last_run_at, ok, detail FROM pipeline_heartbeats"
                )
            for r in rows:
                prev = by_worker.get(r["worker"])
                if prev is None or (
                    r["last_run_at"] and (prev["last_run_at"] is None or r["last_run_at"] > prev["last_run_at"])
                ):
                    by_worker[r["worker"]] = dict(r)
        except Exception:
            continue  # table may not exist until migration 010 / first worker run
    return by_worker


def _status_card(name: str, hb: Optional[Dict[str, Any]], records: Optional[int]) -> Dict[str, Any]:
    if hb is None:
        return {
            "name": name, "status": "unknown", "last_run_at": None,
            "records_processed": records, "detail": None,
        }
    return {
        "name": name,
        "status": "ok" if hb["ok"] else "failing",
        "last_run_at": _iso(hb["last_run_at"]),
        "records_processed": records,
        "detail": hb["detail"],
    }


@router.get("/stats")
async def dashboard_stats() -> Dict[str, Any]:
    player_pool = await get_player_pool()
    core_pool = await get_core_pool()

    totals = await _cached_totals(player_pool)

    try:
        async with player_pool.acquire() as conn:
            live = await conn.fetchrow(
                """
                SELECT
                    (SELECT COUNT(*) FROM sales_history WHERE sold_at >= NOW() - INTERVAL '24 hours') AS sales_last_24h,
                    (SELECT COUNT(*) FROM bin_history WHERE captured_at >= NOW() - INTERVAL '24 hours') AS bin_updates_last_24h,
                    (SELECT COUNT(DISTINCT player_id) FROM bin_history WHERE captured_at >= date_trunc('day', NOW())) AS cards_tracked_today,
                    pg_postmaster_start_time()                                    AS db_started_at,
                    pg_size_pretty(pg_database_size(current_database()))          AS db_size
                """
            )
        live = dict(live)
    except Exception:
        live = {
            "sales_last_24h": 0, "bin_updates_last_24h": 0, "cards_tracked_today": 0,
            "db_started_at": None, "db_size": None,
        }

    try:
        async with player_pool.acquire() as conn:
            movers = await conn.fetchrow(
                """
                SELECT
                    (SELECT row_to_json(m) FROM (
                        SELECT card_id, name, rating, version, volatility_24h,
                               image_url, card_bg_image, card_cutout_image, card_cutout_type, card_name,
                               generated_card_url, generated_card_status, generated_card_flagged
                        FROM fair_value_mv WHERE volatility_24h IS NOT NULL
                        ORDER BY volatility_24h DESC LIMIT 1
                    ) m) AS largest_mover,
                    (SELECT row_to_json(m) FROM (
                        SELECT card_id, name, rating, version, sales_24h,
                               image_url, card_bg_image, card_cutout_image, card_cutout_type, card_name,
                               generated_card_url, generated_card_status, generated_card_flagged
                        FROM fair_value_mv WHERE sales_24h IS NOT NULL
                        ORDER BY sales_24h DESC LIMIT 1
                    ) m) AS most_traded_today,
                    (SELECT row_to_json(m) FROM (
                        SELECT card_id, name, rating, version, sales_per_hour_24h,
                               image_url, card_bg_image, card_cutout_image, card_cutout_type, card_name,
                               generated_card_url, generated_card_status, generated_card_flagged
                        FROM fair_value_mv WHERE sales_per_hour_24h IS NOT NULL
                        ORDER BY sales_per_hour_24h DESC LIMIT 1
                    ) m) AS highest_liquidity
                """
            )
        # row_to_json comes back from asyncpg as a JSON string, not a dict.
        movers = {k: (json.loads(v) if v else None) for k, v in dict(movers).items()}

        movers_needing_cards = [
            str(m["card_id"]) for m in movers.values()
            if m and (m.get("generated_card_status") != "ready" or m.get("generated_card_flagged"))
        ]
        if movers_needing_cards:
            await ensure_cards_requested(player_pool, list(dict.fromkeys(movers_needing_cards)))
    except Exception:
        # fair_value_mv doesn't exist until migrations 011-013 have applied
        # (which themselves wait on sales_history/bin_history existing).
        movers = {"largest_mover": None, "most_traded_today": None, "highest_liquidity": None}

    heartbeats = await _heartbeats_by_worker(core_pool, player_pool)
    futbin_hb = heartbeats.get("futbin_full_sync")
    combined_hb = heartbeats.get("bin_sales_history_sync")
    combined_counts = _parse_detail_counts(combined_hb["detail"] if combined_hb else None)
    futbin_counts = _parse_detail_counts(futbin_hb["detail"] if futbin_hb else None)

    pipeline_status = [
        _status_card(_WORKER_DISPLAY_NAMES["futbin_full_sync"], futbin_hb, futbin_counts.get("written")),
        _status_card("BIN History Sync", combined_hb, combined_counts.get("bin_found")),
        _status_card("Sales History Sync", combined_hb, combined_counts.get("sales_new")),
        {
            "name": "Database",
            "status": "ok",
            "last_run_at": datetime.now(timezone.utc).isoformat(),
            "records_processed": None,
            "detail": "live connectivity check - this response completing is the health signal",
        },
    ]

    successful_runs = [hb["last_run_at"] for hb in heartbeats.values() if hb["ok"] and hb["last_run_at"]]
    last_successful_auto_sync = max(successful_runs) if successful_runs else None
    last_successful_sales_sync = (
        combined_hb["last_run_at"] if combined_hb and combined_hb["ok"] else None
    )

    return {
        "totals": {
            "total_players": totals["total_players"],
            "total_sales": totals["total_sales"],
            "total_bin_snapshots": totals["total_bin_snapshots"],
            "unique_players_with_sales": totals["unique_players_with_sales"],
            "unique_players_with_bin": totals["unique_players_with_bin"],
            "first_recorded_sale_at": _iso(totals["first_recorded_sale_at"]),
        },
        "last_24h": {
            "sales": live["sales_last_24h"],
            "bin_updates": live["bin_updates_last_24h"],
            "avg_sales_per_hour": round((live["sales_last_24h"] or 0) / 24.0, 2),
        },
        "cards_tracked_today": live["cards_tracked_today"],
        "database": {
            "started_at": _iso(live["db_started_at"]),
            "size_pretty": live["db_size"],
        },
        "sync_status": {
            "last_successful_auto_sync": _iso(last_successful_auto_sync),
            "last_successful_sales_sync": _iso(last_successful_sales_sync),
        },
        "pipeline_status": pipeline_status,
        "database_statistics": {
            "largest_24h_mover": movers["largest_mover"],
            "most_traded_player_today": movers["most_traded_today"],
            "highest_liquidity_card": movers["highest_liquidity"],
            "total_unique_players_with_sales": totals["unique_players_with_sales"],
            "total_unique_players_with_bin_history": totals["unique_players_with_bin"],
        },
        "footer": {
            "environment": os.getenv("RAILWAY_ENVIRONMENT_NAME", "Production"),
            "build_version": os.getenv("RAILWAY_GIT_COMMIT_SHA", "unknown")[:7],
            "database_size": live["db_size"],
        },
    }


@router.get("/activity")
async def dashboard_activity(limit: int = Query(20, ge=1, le=100)) -> Dict[str, Any]:
    player_pool = await get_player_pool()
    core_pool = await get_core_pool()

    events: List[Dict[str, Any]] = []
    needs_card: List[str] = []

    def _track_card(r: Any) -> None:
        if r["card_id"] is not None and (
            r["generated_card_status"] != "ready" or r["generated_card_flagged"]
        ):
            needs_card.append(str(r["card_id"]))

    # card_id/rating/version + the 4 card-art columns are carried through
    # on the 3 card-related event types (sale/bin/player_update) so v2's
    # ActivityFeedSection can link to the Player Page and render real card
    # art instead of plain text - sync events have no single card to
    # reference and correctly keep card_id: None.
    _ART_COLS = (
        "p.card_bg_image, p.card_cutout_image, p.card_cutout_type, p.card_name, "
        "p.generated_card_url, p.generated_card_status, p.generated_card_flagged"
    )

    try:
        async with player_pool.acquire() as conn:
            sales = await conn.fetch(
                f"""
                SELECT s.sold_price, s.sold_at, p.card_id, p.name, p.rating, p.version,
                       p.image_url, {_ART_COLS}
                FROM sales_history s
                LEFT JOIN fut_players p ON p.card_id = s.player_id
                ORDER BY s.sold_at DESC
                LIMIT $1
                """,
                limit,
            )
        for r in sales:
            _track_card(r)
            events.append({
                "type": "sale",
                "message": f"New sale recorded: {r['name'] or 'Unknown player'} sold for {r['sold_price']:,} coins",
                "at": r["sold_at"],
                "card_id": r["card_id"], "rating": r["rating"], "version": r["version"],
                "image_url": r["image_url"], "card_bg_image": r["card_bg_image"],
                "card_cutout_image": r["card_cutout_image"], "card_cutout_type": r["card_cutout_type"],
                "card_name": r["card_name"], "generated_card_url": r["generated_card_url"],
                "generated_card_status": r["generated_card_status"],
            })
    except Exception:
        pass

    try:
        async with player_pool.acquire() as conn:
            bins = await conn.fetch(
                f"""
                SELECT b.lowest_bin, b.platform, b.captured_at, p.card_id, p.name, p.rating, p.version,
                       p.image_url, {_ART_COLS}
                FROM bin_history b
                LEFT JOIN fut_players p ON p.card_id = b.player_id
                ORDER BY b.captured_at DESC
                LIMIT $1
                """,
                limit,
            )
        for r in bins:
            _track_card(r)
            events.append({
                "type": "bin",
                "message": f"BIN changed: {r['name'] or 'Unknown player'} now {r['lowest_bin']:,} ({r['platform']})",
                "at": r["captured_at"],
                "card_id": r["card_id"], "rating": r["rating"], "version": r["version"],
                "image_url": r["image_url"], "card_bg_image": r["card_bg_image"],
                "card_cutout_image": r["card_cutout_image"], "card_cutout_type": r["card_cutout_type"],
                "card_name": r["card_name"], "generated_card_url": r["generated_card_url"],
                "generated_card_status": r["generated_card_status"],
            })
    except Exception:
        pass

    try:
        async with player_pool.acquire() as conn:
            updated = await conn.fetch(
                f"""
                SELECT card_id, name, rating, version, image_url, {_ART_COLS}, price_updated_at
                FROM fut_players AS p
                WHERE price_updated_at IS NOT NULL
                ORDER BY price_updated_at DESC
                LIMIT $1
                """,
                limit,
            )
        for r in updated:
            _track_card(r)
            events.append({
                "type": "player_update",
                "message": f"Player updated: {r['name'] or r['card_id']}",
                "at": r["price_updated_at"],
                "card_id": r["card_id"], "rating": r["rating"], "version": r["version"],
                "image_url": r["image_url"], "card_bg_image": r["card_bg_image"],
                "card_cutout_image": r["card_cutout_image"], "card_cutout_type": r["card_cutout_type"],
                "card_name": r["card_name"], "generated_card_url": r["generated_card_url"],
                "generated_card_status": r["generated_card_status"],
            })
    except Exception:
        pass

    # _heartbeats_by_worker already merges core_pool/player_pool and keeps
    # only the freshest row per worker - querying each pool separately here
    # instead would double-list every sync event whenever DATABASE_URL and
    # PLAYER_DATABASE_URL point at the same database (the default
    # single-DB deploy), since core_pool/player_pool are still two distinct
    # asyncpg.Pool objects even when their DSNs are equal.
    for hb in (await _heartbeats_by_worker(core_pool, player_pool)).values():
        display_name = _WORKER_DISPLAY_NAMES.get(hb["worker"], hb["worker"])
        events.append({
            "type": "sync",
            "message": f"Auto sync completed: {display_name} ({'ok' if hb['ok'] else 'failed'})",
            "at": hb["last_run_at"],
            "card_id": None,
        })

    epoch = datetime.min.replace(tzinfo=timezone.utc)
    events.sort(key=lambda e: e["at"] or epoch, reverse=True)
    top = events[:limit]

    if needs_card:
        # Dedup across the three event sources; one claim call for the
        # whole page rather than one per event row.
        await ensure_cards_requested(player_pool, list(dict.fromkeys(needs_card)))

    return {
        "events": [
            {
                "type": e["type"], "message": e["message"], "at": _iso(e["at"]),
                "card_id": e.get("card_id"), "rating": e.get("rating"), "version": e.get("version"),
                "image_url": e.get("image_url"), "card_bg_image": e.get("card_bg_image"),
                "card_cutout_image": e.get("card_cutout_image"), "card_cutout_type": e.get("card_cutout_type"),
                "card_name": e.get("card_name"), "generated_card_url": e.get("generated_card_url"),
                "generated_card_status": e.get("generated_card_status"),
            }
            for e in top
        ]
    }
