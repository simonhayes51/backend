from fastapi import FastAPI, Request, HTTPException, Form
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel
import aiohttp
import asyncpg
import os

app = FastAPI()

# Enable sessions for user tracking
app.add_middleware(SessionMiddleware, secret_key="your_secret_key")  # replace with a strong key

# CORS: allow frontend origin
origins = [
    "https://frontend-production-ab5e.up.railway.app",  # your deployed frontend
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Environment variables
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI")
DATABASE_URL = os.getenv("DATABASE_URL")

# -------------------------
# 🔐 Discord Login
# -------------------------
@app.get("/login")
async def login():
    discord_oauth_url = (
        f"https://discord.com/api/oauth2/authorize?client_id={DISCORD_CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}&response_type=code&scope=identify"
    )
    return RedirectResponse(discord_oauth_url)


@app.get("/callback")
async def callback(request: Request):
    code = request.query_params.get("code")
    async with aiohttp.ClientSession() as session:
        data = {
            "client_id": DISCORD_CLIENT_ID,
            "client_secret": DISCORD_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "scope": "identify",
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        async with session.post("https://discord.com/api/oauth2/token", data=data, headers=headers) as resp:
            token_response = await resp.json()
            access_token = token_response.get("access_token")

        # Fetch user data
        headers = {"Authorization": f"Bearer {access_token}"}
        async with session.get("https://discord.com/api/users/@me", headers=headers) as resp:
            user_data = await resp.json()

    if "id" not in user_data:
        raise HTTPException(status_code=400, detail="❌ Missing Discord ID")

    discord_id = user_data["id"]
    request.session["user"] = user_data  # store session

    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute(
        "INSERT INTO traders (discord_id) VALUES ($1) ON CONFLICT (discord_id) DO NOTHING",
        discord_id,
    )
    await conn.close()

    # redirect back to frontend with user_id
    return RedirectResponse(f"https://frontend-production-ab5e.up.railway.app/?user_id={discord_id}")

# -------------------------
# 👤 Get Profile
# -------------------------
@app.get("/api/profile/{user_id}")
async def get_profile(user_id: str):
    conn = await asyncpg.connect(DATABASE_URL)
    row = await conn.fetchrow("SELECT * FROM traders WHERE discord_id = $1", user_id)
    await conn.close()

    if row:
        return dict(row)
    else:
        return {"error": "User not found"}

# -------------------------
# 💼 Trade model
# -------------------------
class Trade(BaseModel):
    player_name: str
    version: str
    buy_price: int
    sell_price: int
    platform: str

# -------------------------
# 📥 Log Trade
# -------------------------
@app.post("/api/trade")
async def log_trade(trade: Trade, request: Request):
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in.")

    discord_id = str(user["id"])

    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("""
        INSERT INTO trades (player_name, version, buy_price, sell_price, platform, discord_id)
        VALUES ($1, $2, $3, $4, $5, $6)
    """, trade.player_name, trade.version, trade.buy_price, trade.sell_price, trade.platform, discord_id)
    await conn.close()

    return {"message": "✅ Trade logged"}