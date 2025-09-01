
from __future__ import annotations
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
import asyncio, logging, os, aiohttp, asyncpg

from app.services.prices import get_player_price
from app.services.price_history import get_price_history
from app.utils.timebox import now_utc, is_within_quiet_hours

log = logging.getLogger("watchlist")
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
FALLBACK_CHANNEL_ID = os.getenv("WATCHLIST_FALLBACK_CHANNEL_ID")
POLL_INTERVAL_SECONDS = int(os.getenv("WATCHLIST_POLL_INTERVAL", "60"))

async def _send_discord_dm(user_discord_id: str, content: str) -> bool:
    if not DISCORD_BOT_TOKEN or not user_discord_id: return False
    try:
        async with aiohttp.ClientSession(headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}", "Content-Type":"application/json"}) as sess:
            async with sess.post("https://discord.com/api/v10/users/@me/channels", json={"recipient_id": user_discord_id}) as r:
                if r.status not in (200,201): return False
                ch = await r.json()
                ch_id = ch.get("id")
            async with sess.post(f"https://discord.com/api/v10/channels/{ch_id}/messages", json={"content": content}) as r2:
                return r2.status in (200,201)
    except Exception as e:
        log.warning("DM send failed: %s", e)
        return False

async def _send_channel_fallback(channel_id: Optional[str], content: str) -> bool:
    if not DISCORD_BOT_TOKEN: return False
    ch_id = channel_id or FALLBACK_CHANNEL_ID
    if not ch_id: return False
    try:
        async with aiohttp.ClientSession(headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}", "Content-Type":"application/json"}) as sess:
            async with sess.post(f"https://discord.com/api/v10/channels/{ch_id}/messages", json={"content": content}) as r:
                return r.status in (200,201)
    except Exception as e:
        log.warning("Channel send failed: %s", e)
        return False

async def _ref_price(pool: asyncpg.Pool, item: asyncpg.Record, platform: str) -> Optional[float]:
    mode = item["ref_mode"]
    if mode in ("fixed","avg_buy"):
        return float(item["ref_price"] or 0) or None
    hist = await get_price_history(player_id=item["player_id"], platform=platform, tf="today")
    if not hist: return None
    last = hist[-1]
    return float(last.get("price") or last.get("v") or last.get("y") or 0) or None

def _pct_change(cur: float, ref: float) -> float:
    return 0.0 if not ref else 100.0 * (cur - ref) / ref

def _format_alert(player_name: str, platform: str, direction: str, pct: float, price: float, ref_price: float, ref_mode: str) -> str:
    arrow = "📈" if direction == "rise" else "📉"
    return (f"{arrow} Watchlist Alert • {player_name} ({platform.upper()})\n"
            f"Current: {int(price):,}c • Change: {pct:+.2f}%\n"
            f"Ref: {int(ref_price):,}c ({ref_mode})")

async def process_price_tick(pool: asyncpg.Pool, player_id: int, platform: str, price: float, player_name: str = "") -> int:
    sent = 0
    now = now_utc()
    async with pool.acquire() as con:
        rows = await con.fetch("SELECT * FROM watchlist_items WHERE player_id=$1 AND platform=$2", player_id, platform)
    for row in rows:
        try:
            if row["quiet_start"] or row["quiet_end"]:
                if is_within_quiet_hours(now, row["quiet_start"], row["quiet_end"]): continue
            last_at = row["last_alert_at"]
            if last_at and (now - last_at).total_seconds() < (row["cooloff_minutes"] * 60): continue
            refp = await _ref_price(pool, row, platform)
            if not refp: continue
            pct = _pct_change(price, refp)
            direction = "rise" if pct >= float(row["rise_pct"] or 0) else ("fall" if pct <= -float(row["fall_pct"] or 0) else None)
            if not direction: continue
            content = _format_alert(player_name or str(player_id), platform, direction, pct, price, refp, row["ref_mode"])
            ok = False
            if row["prefer_dm"] and row["user_discord_id"]:
                ok = await _send_discord_dm(row["user_discord_id"], content)
            if not ok:
                await _send_channel_fallback(row.get("fallback_channel_id"), content)
            async with pool.acquire() as con:
                await con.execute(
                    "INSERT INTO alerts_log (user_id, user_discord_id, player_id, platform, direction, pct, price, ref_mode, ref_price) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                    row["user_id"], row["user_discord_id"], player_id, platform, direction, pct, price, row["ref_mode"], refp
                )
                await con.execute("UPDATE watchlist_items SET last_alert_at=$1 WHERE id=$2", now, row["id"])
            sent += 1
        except Exception as e:
            log.warning("watchlist eval error: %s", e)
    return sent

async def watchlist_poll_loop(pool: asyncpg.Pool, get_name) -> None:
    await asyncio.sleep(3)
    log.info("watchlist poll loop started (every %ss)", POLL_INTERVAL_SECONDS)
    while True:
        try:
            async with pool.acquire() as con:
                pairs = await con.fetch("SELECT DISTINCT player_id, platform FROM watchlist_items")
            tasks = []
            for rec in pairs:
                pid = rec["player_id"]; plat = rec["platform"]
                tasks.append(_poll_one(pool, pid, plat, get_name))
            await asyncio.gather(*tasks)
        except Exception as e:
            log.warning("poll loop error: %s", e)
        await asyncio.sleep(POLL_INTERVAL_SECONDS)

async def _poll_one(pool: asyncpg.Pool, player_id: int, platform: str, get_name):
    try:
        price = await get_player_price(player_id, platform)
        if not price: return
        name = None
        if get_name:
            try: name = await get_name(player_id)
            except Exception: name = None
        await process_price_tick(pool, player_id, platform, price, name or "")
    except Exception as e:
        log.debug("poll-one error: %s", e)
