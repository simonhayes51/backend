# app/db.py
import os
import asyncpg
from typing import Optional, AsyncGenerator

_CORE_POOL: Optional[asyncpg.Pool] = None
_PLAYER_POOL: Optional[asyncpg.Pool] = None
_WATCHLIST_POOL: Optional[asyncpg.Pool] = None


async def _init_conn(conn: asyncpg.Connection) -> None:
    # Ensure the 'public' schema is visible for unqualified table names
    await conn.execute("SET search_path TO public")


def _require(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v


def _pool_sizes() -> tuple[int, int]:
    min_size = int(os.getenv("POOL_MIN", "1"))
    max_size = int(os.getenv("POOL_MAX", "10"))
    if min_size < 1:
        min_size = 1
    if max_size < min_size:
        max_size = min_size
    return min_size, max_size


async def get_core_pool() -> asyncpg.Pool:
    global _CORE_POOL
    if _CORE_POOL is None:
        min_size, max_size = _pool_sizes()
        _CORE_POOL = await asyncpg.create_pool(
            dsn=_require("DATABASE_URL"),
            min_size=min_size,
            max_size=max_size,
            init=_init_conn,
        )
    return _CORE_POOL


async def get_player_pool() -> asyncpg.Pool:
    global _PLAYER_POOL
    if _PLAYER_POOL is None:
        min_size, max_size = _pool_sizes()
        _PLAYER_POOL = await asyncpg.create_pool(
            dsn=_require("PLAYER_DATABASE_URL"),
            min_size=min_size,
            max_size=max_size,
            init=_init_conn,
        )
    return _PLAYER_POOL


async def get_watchlist_pool() -> asyncpg.Pool:
    global _WATCHLIST_POOL
    if _WATCHLIST_POOL is None:
        min_size, max_size = _pool_sizes()
        _WATCHLIST_POOL = await asyncpg.create_pool(
            dsn=_require("WATCHLIST_DATABASE_URL"),
            min_size=min_size,
            max_size=max_size,
            init=_init_conn,
        )
    return _WATCHLIST_POOL


# ✅ Backwards-compatible name used all over the codebase
# Defaults to CORE database (DATABASE_URL)
async def get_pool() -> asyncpg.Pool:
    return await get_core_pool()


# ✅ Default dependency used by routers (CORE)
async def get_db() -> AsyncGenerator[asyncpg.Connection, None]:
    pool = await get_core_pool()
    async with pool.acquire() as conn:
        yield conn


# Optional explicit dependencies
async def get_player_db() -> AsyncGenerator[asyncpg.Connection, None]:
    pool = await get_player_pool()
    async with pool.acquire() as conn:
        yield conn


async def get_watchlist_db() -> AsyncGenerator[asyncpg.Connection, None]:
    pool = await get_watchlist_pool()
    async with pool.acquire() as conn:
        yield conn
