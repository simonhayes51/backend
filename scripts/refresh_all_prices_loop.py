# scripts/refresh_all_prices_loop.py
import os
import asyncio
import logging
import time
from typing import Dict, List, Tuple, Optional

import aiohttp
import asyncpg
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is required")

# Which platforms to record into the history table (same price for each, source is FUT.GG).
PLATFORMS = [p.strip().lower() for p in os.getenv("PLATFORMS", "ps").split(",") if p.strip()]

# Tuning knobs
CONCURRENCY = int(os.getenv("REFRESH_CONCURRENCY", "16"))
BATCH_SIZE  = int(os.getenv("REFRESH_BATCH_SIZE", "500"))
SLEEP_SECS  = int(os.getenv("REFRESH_INTERVAL_SEC", "1800"))  # 30 minutes

# FUT.GG endpoint (returns a single "currentPrice" object)
FUTGG_PRICE_URL = "https://www.fut.gg/api/fut/player-prices/25/{card_id}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": "https://www.fut.gg/",
    "Origin": "https://www.fut.gg",
}

# ---------- bootstrap DDL ----------
DDL_STATEMENTS = [
    # ensure snapshot columns exist on fut_players
    """
    ALTER TABLE IF EXISTS fut_players
        ADD COLUMN IF NOT EXISTS price INTEGER,
        ADD COLUMN IF NOT EXISTS price_updated_at TIMESTAMPTZ
    """,
    # create historical table
    """
    CREATE TABLE IF NOT EXISTS fut_prices_history (
        id BIGSERIAL PRIMARY KEY,
        card_id
