from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import aiohttp
import asyncpg
import os

app = FastAPI()

# CORS setup - allow frontend
origins = [
    "https://frontend-production-ab5e.up.railway.app",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Load environment variables
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
REDIRECT_URI = "https://backend-production-1f1a.up.railway.app/callback"  # ✅ Set directly
DATABASE_URL = os.getenv("DATABASE_URL")


@app.get("/login")
async def login():
    discord_oauth_url = (
        f"https://discord.com/api/oauth2/authorize"
        f"?client_id={DISCORD_CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&response_type=code"
        f"&scope=identify"
    )
    return RedirectResponse(discord_oauth_url)


@app.get("/callback")
async def callback(code: str):
    async with aiohttp.ClientSession() as session:
        # Exchange code for access token
        token_data = {
            "client_id": DISCORD_CLIENT_ID,
            "client_secret": DISCORD_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "scope": "identify",
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        async with session.post("https://discord.com/api/oauth2/token", data=token_data, headers=headers) as token_resp:
            token_response = await token_resp.json()
            access_token = token_response.get("access_token")

            if not access_token:
                return JSONResponse({"error": "Failed to retrieve access token", "details": token_response}, status_code=400)

        # Get user info
        headers = {"Authorization": f"Bearer {access_token}"}
        async with session.get("https://discord.com/api/users/@me", headers=headers) as user_resp:
            user = await user_resp.json()
            discord_id = user.get("id")

            if not discord_id:
                return JSONResponse({"error": "Failed to retrieve Discord user info", "details": user}, status_code=400)

    # Redirect to frontend with user_id
    frontend_url = f"https://frontend-production-ab5e.up.railway.app/?user_id={discord_id}"
    return RedirectResponse(frontend_url)


@app.get("/api/profile/{user_id}")
async def get_profile(user_id: str):
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        query = "SELECT * FROM traders WHERE discord_id = $1"
        row = await conn.fetchrow(query, user_id)
        await conn.close()

        if row:
            return dict(row)
        else:
            return {"error": "User not found"}

    except Exception as e:
        return {"error": str(e)}