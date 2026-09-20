# app/db.py
import os
import asyncio
import logging
import random
import asyncpg
from typing import Awaitable, Callable, Optional, AsyncGenerator, TypeVar

_CORE_POOL: Optional[asyncpg.Pool] = None
_PLAYER_POOL: Optional[asyncpg.Pool] = None
_WATCHLIST_POOL: Optional[asyncpg.Pool] = None
log = logging.getLogger("db")
T = TypeVar("T")

_TRANSIENT_CONNECT_ERRORS = (
    OSError,
    asyncio.TimeoutError,
    asyncpg.exceptions.ConnectionDoesNotExistError,
    asyncpg.exceptions.ConnectionFailureError,
    asyncpg.exceptions.CannotConnectNowError,
    asyncpg.exceptions.TooManyConnectionsError,
)

_PERMANENT_CONNECT_ERRORS = (
    asyncpg.exceptions.InvalidPasswordError,
    asyncpg.exceptions.InvalidCatalogNameError,
    asyncpg.exceptions.InvalidAuthorizationSpecificationError,
)


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


def _env_int(name: str, default: int, minimum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(minimum, value)


def _env_float(name: str, default: float, minimum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(minimum, value)


async def _with_connect_retry(label: str, factory: Callable[[], Awaitable[T]]) -> T:
    attempts = _env_int("DB_CONNECT_ATTEMPTS", 8, minimum=1)
    base_delay = _env_float("DB_CONNECT_RETRY_BASE_SECONDS", 1.5, minimum=0.1)
    max_delay = _env_float("DB_CONNECT_RETRY_MAX_SECONDS", 30.0, minimum=base_delay)
    last: BaseException | None = None

    for attempt in range(1, attempts + 1):
        try:
            return await factory()
        except _PERMANENT_CONNECT_ERRORS:
            raise
        except _TRANSIENT_CONNECT_ERRORS as exc:
            last = exc
            if attempt >= attempts:
                break
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            delay *= random.uniform(0.7, 1.3)
            log.warning(
                "%s database pool creation failed (%s: %s) - attempt %d/%d, retrying in %.1fs",
                label,
                type(exc).__name__,
                exc,
                attempt,
                attempts,
                delay,
            )
            await asyncio.sleep(delay)

    assert last is not None
    log.error(
        "%s database pool creation failed after %d attempt(s) (%s: %s)",
        label,
        attempts,
        type(last).__name__,
        last,
    )
    raise last


async def get_core_pool() -> asyncpg.Pool:
    global _CORE_POOL
    if _CORE_POOL is None:
        min_size, max_size = _pool_sizes()
        _CORE_POOL = await _with_connect_retry(
            "core",
            lambda: asyncpg.create_pool(
                dsn=_require("DATABASE_URL"),
                min_size=min_size,
                max_size=max_size,
                init=_init_conn,
            ),
        )
    return _CORE_POOL


async def get_player_pool() -> asyncpg.Pool:
    global _PLAYER_POOL
    if _PLAYER_POOL is None:
        min_size, max_size = _pool_sizes()
        _PLAYER_POOL = await _with_connect_retry(
            "player",
            lambda: asyncpg.create_pool(
                dsn=_require("PLAYER_DATABASE_URL"),
                min_size=min_size,
                max_size=max_size,
                init=_init_conn,
            ),
        )
    return _PLAYER_POOL


async def get_watchlist_pool() -> asyncpg.Pool:
    global _WATCHLIST_POOL
    if _WATCHLIST_POOL is None:
        min_size, max_size = _pool_sizes()
        _WATCHLIST_POOL = await _with_connect_retry(
            "watchlist",
            lambda: asyncpg.create_pool(
                dsn=_require("WATCHLIST_DATABASE_URL"),
                min_size=min_size,
                max_size=max_size,
                init=_init_conn,
            ),
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
