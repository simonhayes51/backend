"""
Manually grant (or revoke) premium for a user — the admin escape hatch for
subscriptions that predate webhook wiring, comps, refunds, etc.

Writes users.tier (the manual-grant channel the entitlements engine reads;
see app/auth/entitlements.py::_resolve_tier) and optionally premium_until.

Usage:
    python scripts/grant_premium.py 712239212749127740 elite
    python scripts/grant_premium.py 712239212749127740 pro --days 365
    python scripts/grant_premium.py 712239212749127740 free      # revoke

The id can be the users.id / discord id string. Requires DATABASE_URL.
Takes effect within ENTITLEMENTS_CACHE_TTL (default 60s) for logged-in
sessions - no restart needed.
"""
import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import asyncpg

VALID = {"free", "pro", "elite"}


async def run(user_id: str, tier: str, days: int | None) -> int:
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 1

    until = (
        datetime.now(timezone.utc) + timedelta(days=days)
        if days and tier != "free"
        else None
    )
    conn = await asyncpg.connect(dsn)
    try:
        row = await conn.fetchrow(
            "SELECT id, username, tier, plan FROM users WHERE id = $1 OR discord_id::text = $1",
            user_id,
        )
        if not row:
            print(f"No users row matching id/discord_id {user_id!r}", file=sys.stderr)
            return 1

        await conn.execute(
            "UPDATE users SET tier = $2, premium_until = $3 WHERE id = $1",
            row["id"],
            tier if tier != "free" else "basic",
            until,
        )
        print(
            f"✓ {row['username'] or row['id']}: tier {row['tier']!r} -> {tier!r}"
            + (f", premium_until {until.isoformat()}" if until else "")
        )
        return 0
    finally:
        await conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("user_id", help="users.id or discord id")
    ap.add_argument("tier", choices=sorted(VALID))
    ap.add_argument("--days", type=int, help="optional expiry in days (sets premium_until)")
    a = ap.parse_args()
    return asyncio.run(run(a.user_id, a.tier, a.days))


if __name__ == "__main__":
    raise SystemExit(main())
