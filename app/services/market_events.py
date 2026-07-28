# app/services/market_events.py
#
# Read queries over market_events + sbc_details + sbc_challenges +
# event_market_impact (migrations 018/019). Kind is a plain TEXT
# discriminator so new event kinds (promo/objective/store-pack/
# evolution) never need a schema change here - only 'sbc' has a real
# writer (auto_sync/futbin_sbc_sync.py) as of this module.
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import asyncpg


async def get_events(
    pool: asyncpg.Pool, *, kind: Optional[str] = None, limit: int = 30, offset: int = 0
) -> List[Dict[str, Any]]:
    """List view - includes sbc_details' hub-list-relevant fields
    (category/cost/expiry) via LEFT JOIN so a Hub page doesn't need N
    detail-endpoint calls just to render a usable list. Other event
    kinds simply get NULLs here until they get their own detail table."""
    where = "WHERE e.kind = $3" if kind else ""
    params: List[Any] = [limit, offset]
    if kind:
        params.append(kind)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT e.id, e.kind, e.source, e.external_id, e.title, e.description,
                   e.starts_at, e.ends_at, e.fingerprint, e.first_seen_at, e.updated_at,
                   d.category, d.total_cost_coins, d.repeatable,
                   rc.name AS reward_card_name, rc.rating AS reward_card_rating,
                   rc.version AS reward_card_version, rc.image_url AS reward_card_image_url,
                   rc.card_bg_image AS reward_card_bg_image,
                   rc.card_cutout_image AS reward_card_cutout_image,
                   rc.card_cutout_type AS reward_card_cutout_type,
                   rc.card_name AS reward_card_card_name
            FROM market_events e
            LEFT JOIN sbc_details d ON d.event_id = e.id
            LEFT JOIN fut_players rc ON rc.card_id = d.reward_card_id
            {where}
            ORDER BY e.starts_at DESC NULLS LAST, e.first_seen_at DESC
            LIMIT $1 OFFSET $2
            """,
            *params,
        )
    return [dict(r) for r in rows]


async def get_event(pool: asyncpg.Pool, event_id: int) -> Optional[Dict[str, Any]]:
    async with pool.acquire() as conn:
        event = await conn.fetchrow(
            """
            SELECT id, kind, source, external_id, title, description,
                   starts_at, ends_at, fingerprint, payload, first_seen_at, updated_at
            FROM market_events WHERE id = $1
            """,
            event_id,
        )
        if not event:
            return None
        result = dict(event)
        # JSONB columns come back from asyncpg as JSON *strings* (no codec
        # is registered anywhere in app/db.py - the same reason
        # dashboard.py and entitlements.py already hand-parse their own
        # JSONB reads), not parsed dicts - decode here so callers get real
        # objects, not double-encoded strings.
        if result.get("payload") is not None:
            result["payload"] = json.loads(result["payload"])

        if event["kind"] == "sbc":
            details = await conn.fetchrow(
                """
                SELECT d.set_name, d.category, d.total_cost_coins, d.repeatable,
                       d.reward_card_id, d.reward_description, d.expires_at,
                       rc.name AS reward_card_name, rc.rating AS reward_card_rating,
                       rc.version AS reward_card_version, rc.image_url AS reward_card_image_url,
                       rc.card_bg_image AS reward_card_bg_image,
                       rc.card_cutout_image AS reward_card_cutout_image,
                       rc.card_cutout_type AS reward_card_cutout_type,
                       rc.card_name AS reward_card_card_name
                FROM sbc_details d
                LEFT JOIN fut_players rc ON rc.card_id = d.reward_card_id
                WHERE d.event_id = $1
                """,
                event_id,
            )
            result["sbc_details"] = dict(details) if details else None

            challenges = await conn.fetch(
                """
                SELECT id, challenge_name, requirements, estimated_cost_coins, display_order
                FROM sbc_challenges WHERE event_id = $1
                ORDER BY display_order ASC, id ASC
                """,
                event_id,
            )
            parsed_challenges = []
            for c in challenges:
                cd = dict(c)
                if cd.get("requirements") is not None:
                    cd["requirements"] = json.loads(cd["requirements"])
                parsed_challenges.append(cd)
            result["sbc_challenges"] = parsed_challenges

        return result


async def get_event_impact(pool: asyncpg.Pool, event_id: int) -> List[Dict[str, Any]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT i.card_id, p.name, p.rating, p.version, p.image_url, i.relation,
                   p.card_bg_image, p.card_cutout_image, p.card_cutout_type, p.card_name,
                   i.price_before, i.price_after, i.price_change_pct,
                   i.volume_before_24h, i.volume_after_24h,
                   i.measured_before_at, i.measured_after_at, i.computed_at
            FROM event_market_impact i
            LEFT JOIN fut_players p ON p.card_id = i.card_id
            WHERE i.event_id = $1
            ORDER BY ABS(COALESCE(i.price_change_pct, 0)) DESC
            """,
            event_id,
        )
    return [dict(r) for r in rows]
