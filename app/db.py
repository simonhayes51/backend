# app/db.py
import os, asyncpg
_POOL = None

async def __init_conn(conn):
    # Ensure the app searches 'public' so plain table names resolve
    await conn.execute("SET search_path TO public")

async def get_pool():
    global _POOL
    if _POOL is None:
        _POOL = await asyncpg.create_pool(
            dsn=os.getenv("DATABASE_URL"),
            min_size=int(os.getenv("POOL_MIN", 1)),
            max_size=int(os.getenv("POOL_MAX", 10)),
            init=__init_conn,  # <-- important
        )
    return _POOL

async def get_db():
    pool = await get_pool()
    async with pool.acquire() as conn:
        yield conn
