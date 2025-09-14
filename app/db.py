# app/db.py
import os
import asyncpg

_POOL = None

async def get_pool():
    global _POOL
    if _POOL is None:
        _POOL = await asyncpg.create_pool(
            dsn=os.getenv("DATABASE_URL"),
            min_size=int(os.getenv("POOL_MIN", 1)),
            max_size=int(os.getenv("POOL_MAX", 10)),
        )
    return _POOL

# FastAPI dependency
async def get_db():
    pool = await get_pool()
    async with pool.acquire() as conn:
        yield conn
