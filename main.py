import os
import json
import asyncpg
import aiohttp
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI")
SECRET_KEY = os.getenv("SECRET_KEY", "supersecretkey")

app = FastAPI()

# Middleware
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://frontend-production-ab5e.up.railway.app",
        "http://localhost:5173",
        "http://localhost:3000",
        "*"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# DB connection
async def get_db():
    return await asyncpg.connect(DATABASE_URL)

# OAuth Login
@app.get("/login")
async def login():
    return RedirectResponse(
        f"https://discord.com/oauth2/authorize?client_id={DISCORD_CLIENT_ID}&redirect_uri={DISCORD_REDIRECT_URI}&response_type=code&scope=identify"
    )

@app.get("/callback")
async def callback(request: Request):
    code = request.query_params.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="Missing code")

    token_url = "https://discord.com/api/oauth2/token"
    data = {
        "client_id": DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": DISCORD_REDIRECT_URI,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    async with aiohttp.ClientSession() as session:
        async with session.post(token_url, data=data, headers=headers) as resp:
            token_data = await resp.json()
            if "access_token" not in token_data:
                raise HTTPException(status_code=400, detail="OAuth failed")
            access_token = token_data["access_token"]

        async with session.get(
            "https://discord.com/api/users/@me",
            headers={"Authorization": f"Bearer {access_token}"}
        ) as resp:
            user_data = await resp.json()
            user_id = user_data["id"]
            request.session["user_id"] = user_id
            return RedirectResponse(f"https://frontend-production-ab5e.up.railway.app/?user_id={user_id}")

# ✅ NEW ENDPOINT: Profile
@app.get("/api/profile/me")
async def get_profile(request: Request):
    """Returns the logged-in user's stats"""
    user_id = request.query_params.get("user_id")
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing user_id")
    return await get_dashboard(user_id)

# Add Trade
@app.post("/api/add_trade")
async def add_trade(request: Request):
    try:
        data = await request.json()
        required_fields = ["user_id", "player", "version", "buy", "sell", "quantity", "platform", "tag"]
        if not all(field in data for field in required_fields):
            raise HTTPException(status_code=400, detail="Missing required fields")

        quantity = int(data["quantity"])
        buy = int(data["buy"])
        sell = int(data["sell"])
        profit = (sell - buy) * quantity
        ea_tax = int((sell * quantity) * 0.05)

        conn = await get_db()
        await conn.execute(
            """
            INSERT INTO trades (user_id, player, version, buy, sell, quantity, platform, profit, ea_tax, tag, timestamp)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,NOW())
            """,
            data["user_id"],
            data["player"],
            data["version"],
            buy,
            sell,
            quantity,
            data["platform"],
            profit,
            ea_tax,
            data["tag"]
        )
        await conn.close()
        return {"message": "Trade added successfully!"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Dashboard Endpoint
@app.get("/api/dashboard/{user_id}")
async def get_dashboard(user_id: str):
    try:
        conn = await get_db()

        portfolio = await conn.fetchrow("SELECT starting_balance FROM portfolio WHERE user_id=$1", user_id)
        stats = await conn.fetchrow(
            "SELECT COALESCE(SUM(profit),0) AS total_profit, COALESCE(SUM(ea_tax),0) AS total_tax FROM trades WHERE user_id=$1",
            user_id,
        )

        trades = await conn.fetch(
            "SELECT player, version, buy, sell, quantity, platform, profit, ea_tax, tag, timestamp "
            "FROM trades WHERE user_id=$1 ORDER BY timestamp DESC LIMIT 10",
            user_id,
        )

        total_profit = sum(t["profit"] or 0 for t in trades)
        win_count = len([t for t in trades if t["profit"] and t["profit"] > 0])
        win_rate = round((win_count / len(trades)) * 100, 1) if trades else 0

        tag_count = {}
        for t in trades:
            tag = t.get("tag", "N/A") or "N/A"
            tag_count[tag] = tag_count.get(tag, 0) + 1
        most_used_tag = max(tag_count.items(), key=lambda x: x[1])[0] if tag_count else "N/A"

        best_trade = max(trades, key=lambda t: t["profit"] or 0, default=None)
        await conn.close()

        return {
            "netProfit": stats["total_profit"] or 0,
            "taxPaid": stats["total_tax"] or 0,
            "startingBalance": portfolio["starting_balance"] if portfolio else 0,
            "trades": [dict(row) for row in trades],
            "profile": {
                "totalProfit": total_profit or 0,
                "tradesLogged": len(trades),
                "winRate": win_rate,
                "mostUsedTag": most_used_tag,
                "bestTrade": dict(best_trade) if best_trade else None,
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health_check():
    return {"status": "healthy"}