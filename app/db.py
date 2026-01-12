# app/db.py
import os
import asyncpg
from typing import Optional

_CORE_POOL: Optional[asyncpg.Pool] = None
_PLAYER_POOL: Optional[asyncpg.Pool] = None
_WATCHLIST_POOL: Optional[asyncpg.Pool] = None

async def _init_conn(conn: asyncpg.Connection):
    await conn.execute("SET search_path TO public")

def _require(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing {name}")
    return v

async def get_core_pool() -> asyncpg.Pool:
    global _CORE_POOL
    if _CORE_POOL is None:
        _CORE_POOL = await asyncpg.create_pool(
            dsn=_require("DATABASE_URL"),
            min_size=int(os.getenv("POOL_MIN", 1)),
            max_size=int(os.getenv("POOL_MAX", 10)),
            init=_init_conn,
        )
    return _CORE_POOL

async def get_player_pool() -> asyncpg.Pool:
    global _PLAYER_POOL
    if _PLAYER_POOL is None:
        _PLAYER_POOL = await asyncpg.create_pool(
            dsn=_require("PLAYER_DATABASE_URL"),
            min_size=int(os.getenv("POOL_MIN", 1)),
            max_size=int(os.getenv("POOL_MAX", 10)),
            init=_init_conn,
        )
    return _PLAYER_POOL

async def get_watchlist_pool() -> asyncpg.Pool:
    global _WATCHLIST_POOL
    if _WATCHLIST_POOL is None:
        _WATCHLIST_POOL = await asyncpg.create_pool(
            dsn=_require("WATCHLIST_DATABASE_URL"),
            min_size=int(os.getenv("POOL_MIN", 1)),
            max_size=int(os.getenv("POOL_MAX", 10)),
            init=_init_conn,
        )
    return _WATCHLIST_POOL

# Default dependency for most routers (CORE DB)
async def get_db():
    pool = await get_core_pool()
    async with pool.acquire() as conn:
        yield conn

# Optional dependencies if you want them explicitly
async def get_player_db():
    pool = await get_player_pool()
    async with pool.acquire() as conn:
        yield conn

async def get_watchlist_db():
    pool = await get_watchlist_pool()
    async with pool.acquire() as conn:
        yield conn
