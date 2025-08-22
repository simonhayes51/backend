from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import asyncpg
import os

app = FastAPI()

DATABASE_URL = os.getenv("DATABASE_URL")

origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/profile/{user_id}")
async def get_profile(user_id: str):
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        portfolio = await conn.fetchrow("SELECT starting_balance FROM portfolio WHERE user_id=$1", user_id)
        stats = await conn.fetchrow("SELECT SUM(profit) AS total_profit, SUM(ea_tax) AS total_tax, COUNT(*) AS count FROM trades WHERE user_id=$1", user_id)
        await conn.close()
        return {
            "starting_balance": portfolio["starting_balance"] if portfolio else 0,
            "total_profit": stats["total_profit"] or 0,
            "total_tax": stats["total_tax"] or 0,
            "trades": stats["count"]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/sales/{user_id}")
async def get_sales(user_id: str):
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        rows = await conn.fetch("SELECT player, quantity, sell, profit, timestamp FROM trades WHERE user_id=$1 ORDER BY timestamp DESC LIMIT 10", user_id)
        await conn.close()
        return [dict(row) for row in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080)        
