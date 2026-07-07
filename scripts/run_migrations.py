"""
Ordered SQL migration runner — the deploy-time replacement for the pile of
CREATE TABLE IF NOT EXISTS statements that used to run inside the app's
lifespan on every boot (review issue C5).

Usage (Railway release phase or manually):

    python scripts/run_migrations.py                 # core DB (DATABASE_URL)
    python scripts/run_migrations.py --player        # player DB (PLAYER_DATABASE_URL)
    python scripts/run_migrations.py --dsn <url>     # explicit DSN

Rules:
  - Files in migrations/ run in lexicographic order.
  - Each applied filename is recorded in schema_migrations and never re-run.
  - A failing migration aborts the run (later files don't apply).
  - Files whose header comment says which DB they target are your contract;
    the runner doesn't parse it — pass the right flag.

Note: two legacy pairs share a numeric prefix (001_*, 006_*). Lexicographic
order disambiguates deterministically and both pairs are independent, so
this is safe — but stop reusing prefixes for new files.
"""
import argparse
import asyncio
import os
import pathlib
import sys

import asyncpg

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent.parent / "migrations"


async def run(dsn: str) -> int:
    conn = await asyncpg.connect(dsn)
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

        files = sorted(p for p in MIGRATIONS_DIR.glob("*.sql"))
        ran = 0
        for path in files:
            if path.name in applied:
                continue
            sql = path.read_text()
            print(f"→ applying {path.name} ...", flush=True)
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (filename) VALUES ($1)",
                    path.name,
                )
            ran += 1
            print(f"✓ {path.name}", flush=True)

        print(f"Done - {ran} migration(s) applied, {len(applied)} previously applied.")
        return 0
    finally:
        await conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--player", action="store_true", help="run against PLAYER_DATABASE_URL")
    ap.add_argument("--dsn", help="explicit DSN (overrides env)")
    args = ap.parse_args()

    dsn = args.dsn
    if not dsn:
        env = "PLAYER_DATABASE_URL" if args.player else "DATABASE_URL"
        dsn = os.getenv(env) or os.getenv("DATABASE_URL")
    if not dsn:
        print("No DSN - set DATABASE_URL (or pass --dsn).", file=sys.stderr)
        return 1
    return asyncio.run(run(dsn))


if __name__ == "__main__":
    raise SystemExit(main())
