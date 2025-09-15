# app/db.py 
import os
import asyncpg

_POOL = None

async def __init_conn(conn: asyncpg.Connection):
    # Ensure the 'public' schema is visible for unqualified table names
    await conn.execute("SET search_path TO public")

def _dsn() -> str:
    dsn = os.getenv("PLAYER_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("No PLAYER_DATABASE_URL or DATABASE_URL set")
    return dsn

async def get_pool() -> asyncpg.Pool:
    global _POOL
    if _POOL is None:
        _POOL = await asyncpg.create_pool(
            dsn=_dsn(),
            min_size=int(os.getenv("POOL_MIN", 1)),
            max_size=int(os.getenv("POOL_MAX", 10)),
            init=__init_conn,
        )
    return _POOL

# FastAPI dependency
async def get_db():
    pool = await get_pool()
    async with pool.acquire() as conn:
        yield conn
