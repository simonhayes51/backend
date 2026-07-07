"""
Ordered SQL migration runner — the deploy-time replacement for the pile of
CREATE TABLE IF NOT EXISTS statements that used to run inside the app's
lifespan on every boot (review issue C5).

Two ways to run it:

  1. Automatically at app boot (default; for deploys with no terminal
     access, e.g. Railway) - main.py's lifespan calls run() for the core
     and player targets unless RUN_MIGRATIONS_ON_BOOT=0. Guarded by a pg
     advisory lock so concurrent instances can't race, and a failure is
     logged loudly but does NOT crash the boot (every consumer of the new
     tables degrades gracefully until the migration succeeds).

  2. Manually / as a Railway pre-deploy command:
         python scripts/run_migrations.py                 # core DB
         python scripts/run_migrations.py --player        # player DB
         python scripts/run_migrations.py --dsn <url>

Rules:
  - Files in migrations/ run in lexicographic order, once each, recorded
    in schema_migrations.
  - A file may declare its target DB in a header comment
    ("-- target: player"); the default target is core. A run only executes
    files matching its target.
  - LEGACY_BASELINE files (001-009 + the 2025 events migration) predate
    this runner and were applied by hand in pgAdmin per the docs. They are
    recorded as applied WITHOUT being executed, because re-running them on
    a live DB would fail (they aren't idempotent). Pass --execute-legacy
    on a genuinely fresh database to actually run them.
  - A failing migration aborts the run (later files don't apply).
"""
import argparse
import asyncio
import logging
import os
import pathlib
import re
import sys

import asyncpg

log = logging.getLogger("migrations")

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent.parent / "migrations"
ADVISORY_LOCK_KEY = 7741003  # distinct from candle-aggregator + fair-value locks

_TARGET_RE = re.compile(r"--\s*target:\s*(core|player|watchlist)", re.IGNORECASE)

# Applied manually before this runner existed - baselined, never re-executed.
LEGACY_BASELINE = frozenset(
    {
        "001_subscription_enhancements.sql",
        "001_trade_finder.sql",
        "002_monetization_features.sql",
        "003_social_trading_feed.sql",
        "004_social_feed_post_updates.sql",
        "005_single_tier_subscriptions.sql",
        "006_multiple_images.sql",
        "006_trader_payment_accounts.sql",
        "007_currency.sql",
        "008_post_tips_payment_verification.sql",
        "009_drop_cut_features.sql",
        "20250901_events_watchlist.sql",
    }
)


def _file_target(sql: str) -> str:
    m = _TARGET_RE.search(sql[:2000])
    return m.group(1).lower() if m else "core"


async def run(dsn: str, target: str = "core", execute_legacy: bool = False) -> int:
    """Apply pending migrations for `target` against `dsn`.
    Returns the number of migrations executed (baselines not counted)."""
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", ADVISORY_LOCK_KEY)
        try:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    filename   TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            applied = {
                r["filename"]
                for r in await conn.fetch("SELECT filename FROM schema_migrations")
            }

            ran = 0
            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if path.name in applied:
                    continue
                sql = path.read_text()
                if _file_target(sql) != target:
                    continue

                if path.name in LEGACY_BASELINE and not execute_legacy:
                    await conn.execute(
                        "INSERT INTO schema_migrations (filename) VALUES ($1) ON CONFLICT DO NOTHING",
                        path.name,
                    )
                    log.info("baselined (recorded, not executed): %s", path.name)
                    continue

                log.info("applying %s ...", path.name)
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (filename) VALUES ($1)",
                        path.name,
                    )
                ran += 1
                log.info("applied %s", path.name)

            log.info(
                "migrations done (target=%s): %d executed, %d previously recorded",
                target, ran, len(applied),
            )
            return ran
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", ADVISORY_LOCK_KEY)
    finally:
        await conn.close()


async def run_on_boot(core_dsn: str, player_dsn: str) -> None:
    """Called from main.py's lifespan. Never raises - the app must still
    boot (and serve its degraded-but-working paths) if a migration fails."""
    try:
        await run(core_dsn, target="core")
    except Exception as e:
        log.error("BOOT MIGRATIONS FAILED (core): %s - app continuing", e)
    try:
        await run(player_dsn, target="player")
    except Exception as e:
        log.error("BOOT MIGRATIONS FAILED (player): %s - app continuing", e)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--player", action="store_true", help="run against PLAYER_DATABASE_URL")
    ap.add_argument("--dsn", help="explicit DSN (overrides env)")
    ap.add_argument(
        "--execute-legacy",
        action="store_true",
        help="actually execute the 001-009 legacy files (fresh databases only)",
    )
    args = ap.parse_args()

    dsn = args.dsn
    if not dsn:
        env = "PLAYER_DATABASE_URL" if args.player else "DATABASE_URL"
        dsn = os.getenv(env) or os.getenv("DATABASE_URL")
    if not dsn:
        print("No DSN - set DATABASE_URL (or pass --dsn).", file=sys.stderr)
        return 1

    target = "player" if args.player else "core"
    asyncio.run(run(dsn, target=target, execute_legacy=args.execute_legacy))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
