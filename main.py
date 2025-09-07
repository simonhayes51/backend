@app.post("/api/trades")
async def add_trade(request: Request, user_id: str = Depends(get_current_user), conn=Depends(get_db)):
    data = await request.json()
    required_fields = ["player", "version", "buy", "sell", "quantity", "platform", "tag"]
    missing_fields = [f for f in required_fields if f not in data or data[f] == ""]
    if missing_fields:
        raise HTTPException(status_code=400, detail=f"Missing required fields: {missing_fields}")

    try:
        quantity = int(data["quantity"])
        buy = parse_coin_amount(data["buy"])
        sell = parse_coin_amount(data["sell"])
        if quantity <= 0 or buy < 0 or sell <= 0:
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid numeric values")

    # ✅ trade_id: sanitize or generate
    trade_id = data.get("trade_id")
    if isinstance(trade_id, str):
        tid = trade_id.strip()
        trade_id = int(tid) if tid.isdigit() else None
    elif not isinstance(trade_id, (int, type(None))):
        trade_id = None

    if trade_id is None:
        # generate snowflake-ish id + ensure uniqueness for this user
        base = int(time.time() * 1000)
        trade_id = base + secrets.randbelow(1000)
        exists = await conn.fetchval(
            "SELECT 1 FROM trades WHERE user_id=$1 AND trade_id=$2",
            user_id, trade_id
        )
        if exists:
            trade_id = base + secrets.randbelow(1000000)

    profit = (sell - buy) * quantity
    ea_tax = int(round(sell * quantity * 0.05))

    row = await conn.fetchrow(
        """
        INSERT INTO trades (
            user_id, player, version, buy, sell, quantity, platform, profit, ea_tax, tag, notes, timestamp, trade_id
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,NOW(),$12)
        RETURNING player, version, buy, sell, quantity, platform, profit, ea_tax, tag, notes, timestamp, trade_id
        """,
        user_id,
        (data["player"] or "").strip(),
        (data["version"] or "").strip(),
        buy,
        sell,
        quantity,
        (data["platform"] or "").strip(),
        profit,
        ea_tax,
        (data["tag"] or "").strip(),
        (data.get("notes", "") or "").strip(),
        trade_id,
    )
    return {
        "message": "Trade added successfully!",
        "trade": dict(row)  # includes trade_id, timestamp, profit, ea_tax, etc.
    }

@app.get("/api/trades")
async def get_all_trades(user_id: str = Depends(get_current_user), conn=Depends(get_db)):
    rows = await conn.fetch("SELECT * FROM trades WHERE user_id=$1 ORDER BY timestamp DESC", user_id)
    return {"trades": [dict(r) for r in rows]}

@app.put("/api/trades/{trade_id}")
async def update_trade(
    trade_id: int,
    payload: 'TradeUpdate',
    user_id: str = Depends(get_current_user),
    conn=Depends(get_db),
):
    data = payload.model_dump(exclude_none=True)
    if not data:
        raise HTTPException(status_code=400, detail="No fields to update")

    if "buy" in data:
        data["buy"] = parse_coin_amount(data["buy"])
    if "sell" in data:
        data["sell"] = parse_coin_amount(data["sell"])

    fields, values = [], []
    for col, val in data.items():
        fields.append(f"{col} = ${len(values)+1}")
        values.append(val)

    q = f"""
        UPDATE trades
           SET {', '.join(fields)}
         WHERE trade_id = ${len(values)+1}
           AND user_id  = ${len(values)+2}
     RETURNING player, version, quantity, buy, sell, platform, tag, notes, ea_tax, profit, timestamp, trade_id
    """
    values.extend([trade_id, user_id])
    row = await conn.fetchrow(q, *values)
    if not row:
        raise HTTPException(status_code=404, detail="Trade not found")

    sell = int(row["sell"] or 0)
    buy  = int(row["buy"] or 0)
    qty  = int(row["quantity"] or 1)
    ea_tax = int(round(sell * qty * 0.05))
    profit = (sell - buy) * qty

    row2 = await conn.fetchrow(
        """
        UPDATE trades
           SET ea_tax = $1,
               profit = $2
         WHERE trade_id = $3
           AND user_id  = $4
     RETURNING player, version, quantity, buy, sell, platform, tag, notes, ea_tax, profit, timestamp, trade_id
        """,
        ea_tax, profit, trade_id, user_id
    )
    return dict(row2)

@app.delete("/api/trades/{trade_id}")
async def delete_trade(trade_id: int, user_id: str = Depends(get_current_user), conn=Depends(get_db)):
    result = await conn.execute("DELETE FROM trades WHERE trade_id=$1 AND user_id=$2", trade_id, user_id)
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Trade not found")
    return {"message": "Trade deleted successfully"}

@app.get("/api/fut-player-definition/{card_id}")
async def get_player_definition(card_id: str):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://www.fut.gg/api/fut/player-item-definitions/25/{card_id}/",
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "en-GB,en;q=0.9",
                    "Referer": "https://www.fut.gg/",
                    "Origin": "https://www.fut.gg",
                },
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                return {"error": f"API returned status {resp.status}"}
    except Exception as e:
        logging.error(f"Player definition fetch error: {e}")
        return {"error": str(e)}

@app.get("/api/fut-player-price/{card_id}")
async def get_player_price_proxy(card_id: str):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://www.fut.gg/api/fut/player-prices/25/{card_id}",
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "en-GB,en;q=0.9",
                    "Referer": "https://www.fut.gg/",
                    "Origin": "https://www.fut.gg",
                },
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                return {"error": f"API returned status {resp.status}"}
    except Exception as e:
        logging.error(f"Player price fetch error: {e}")
        return {"error": str(e)}

# ----------------- WATCHLIST ROUTES (simple list used by UI) -----------------
@app.post("/api/watchlist")
async def add_watch_item(payload: WatchlistCreate, user_id: str = Depends(get_current_user)):
    try:
        async with watchlist_pool.acquire() as conn:
            live = await fetch_price(payload.card_id, payload.platform)
            start_price = live["price"] if isinstance(live["price"], int) else 0

            row = await conn.fetchrow(
                """
                INSERT INTO watchlist (
                    user_id, card_id, player_name, version, platform, started_price, last_price, last_checked, notes
                )
                VALUES ($1,$2,$3,$4,$5,$6,$7,NOW(),$8)
                ON CONFLICT (user_id, card_id, platform) DO UPDATE
                  SET player_name=EXCLUDED.player_name,
                      version=EXCLUDED.version,
                      notes=EXCLUDED.notes,
                      last_price=EXCLUDED.last_price,
                      last_checked=NOW()
                RETURNING id
                """,
                user_id,
                payload.card_id,
                payload.player_name,
                payload.version,
                payload.platform.lower(),
                start_price,
                live["price"] if isinstance(live["price"], int) else None,
                payload.notes,
            )
            return {
                "ok": True,
                "id": row["id"],
                "start_price": start_price,
                "is_extinct": live.get("isExtinct", False),
            }
    except Exception as e:
        print(f"Watchlist POST error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/watchlist")
async def list_watch_items(user_id: str = Depends(get_current_user)):
    try:
        async with watchlist_pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM watchlist WHERE user_id=$1 ORDER BY started_at DESC", user_id)
            watches = [dict(r) for r in rows]
            if not watches:
                return {"ok": True, "items": []}

            card_ids = [str(w["card_id"]) for w in watches if w.get("card_id") is not None]

            async with player_pool.acquire() as pconn:
                meta_rows = await pconn.fetch(
                    """
                    SELECT card_id, name, rating, club, nation
                    FROM fut_players
                    WHERE card_id = ANY($1::text[])
                    """,
                    card_ids,
                )

            meta_map = {
                str(m["card_id"]): {k: m[k] for k in ("name", "rating", "club", "nation")}
                for m in meta_rows
            }

            enriched = []
            for w in watches:
                live = await fetch_price(w["card_id"], w["platform"])
                live_price = live.get("price")
                change = None
                change_pct = None
                if (isinstance(live_price, (int, float)) and w["started_price"] and w["started_price"] > 0):
                    change = int(live_price) - int(w["started_price"])
                    change_pct = round((change / int(w["started_price"])) * 100, 2)

                m = meta_map.get(str(w["card_id"]), {})
                enriched.append({
                    "id": w["id"],
                    "card_id": w["card_id"],
                    "player_name": w["player_name"],
                    "version": w["version"],
                    "platform": w["platform"],
                    "started_price": w["started_price"],
                    "started_at": w["started_at"].isoformat(),
                    "current_price": live_price if isinstance(live_price, int) else None,
                    "is_extinct": live.get("isExtinct", False),
                    "updated_at": live.get("updatedAt"),
                    "change": change,
                    "change_pct": change_pct,
                    "notes": w["notes"],
                    "name": m.get("name"),
                    "rating": m.get("rating"),
                    "club": m.get("club"),
                    "nation": m.get("nation"),
                })
            return {"ok": True, "items": enriched}
    except Exception as e:
        print(f"Watchlist GET error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/watchlist/{watch_id}")
async def delete_watch_item(watch_id: int, user_id: str = Depends(get_current_user)):
    try:
        async with watchlist_pool.acquire() as conn:
            res = await conn.execute("DELETE FROM watchlist WHERE id=$1 AND user_id=$2", watch_id, user_id)
            if res == "DELETE 0":
                raise HTTPException(status_code=404, detail="Watch item not found")
            return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Watchlist DELETE error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/watchlist/{watch_id}/refresh")
async def refresh_watch_item(watch_id: int, user_id: str = Depends(get_current_user)):
    try:
        async with watchlist_pool.acquire() as conn:
            w = await conn.fetchrow("SELECT * FROM watchlist WHERE id=$1 AND user_id=$2", watch_id, user_id)
            if not w:
                raise HTTPException(status_code=404, detail="Watch item not found")

            live = await fetch_price(w["card_id"], w["platform"])
            live_price = live.get("price") if isinstance(live.get("price"), int) else None

            await conn.execute(
                "UPDATE watchlist SET last_price=$1, last_checked=NOW() WHERE id=$2",
                live_price,
                watch_id,
            )

            change = None
            change_pct = None
            if live_price is not None and w["started_price"] > 0:
                change = int(live_price) - int(w["started_price"])
                change_pct = round((change / int(w["started_price"])) * 100, 2)

            async with player_pool.acquire() as pconn:
                meta = await pconn.fetchrow(
                    """
                    SELECT card_id, name, rating, club, nation
                    FROM fut_players
                    WHERE card_id = $1::text
                    """,
                    str(w["card_id"]),
                )
            meta_dict = dict(meta) if meta else {}

            return {
                "ok": True,
                "item": {
                    "id": w["id"],
                    "card_id": w["card_id"],
                    "player_name": w["player_name"],
                    "version": w["version"],
                    "platform": w["platform"],
                    "started_price": w["started_price"],
                    "started_at": w["started_at"].isoformat(),
                    "current_price": live_price,
                    "is_extinct": live.get("isExtinct", False),
                    "updated_at": live.get("updatedAt"),
                    "change": change,
                    "change_pct": change_pct,
                    "notes": w["notes"],
                    "name": meta_dict.get("name"),
                    "rating": meta_dict.get("rating"),
                    "club": meta_dict.get("club"),
                    "nation": meta_dict.get("nation"),
                },
            }
    except HTTPException:
        raise
    except Exception as e:
        print(f"Watchlist REFRESH error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ----------------- WATCHLIST ALERTS (new) -----------------
async def _send_discord_dm(user_discord_id: str, content: str) -> bool:
    if not DISCORD_BOT_TOKEN or not user_discord_id:
        return False
    try:
        async with aiohttp.ClientSession(headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}", "Content-Type":"application/json"}) as sess:
            # Create or get DM channel
            async with sess.post("https://discord.com/api/v10/users/@me/channels", json={"recipient_id": user_discord_id}) as r:
                if r.status not in (200, 201):
                    return False
                ch = await r.json()
                ch_id = ch.get("id")
            async with sess.post(f"https://discord.com/api/v10/channels/{ch_id}/messages", json={"content": content}) as r2:
                return r2.status in (200, 201)
    except Exception as e:
        logging.warning("DM send failed: %s", e)
        return False

async def _send_channel_fallback(channel_id: Optional[str], content: str) -> bool:
    if not DISCORD_BOT_TOKEN:
        return False
    ch_id = channel_id or WATCHLIST_FALLBACK_CHANNEL_ID
    if not ch_id:
        return False
    try:
        async with aiohttp.ClientSession(headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}", "Content-Type":"application/json"}) as sess:
            async with sess.post(f"https://discord.com/api/v10/channels/{ch_id}/messages", json={"content": content}) as r:
                return r.status in (200, 201)
    except Exception as e:
        logging.warning("Channel send failed: %s", e)
        return False

def _fmt_alert(name: str, platform: str, direction: str, pct: float, price: float, ref_mode: str, ref_price: Optional[float]) -> str:
    arrow = "📈" if direction == "rise" else "📉"
    rp = f"{int(ref_price):,}c" if isinstance(ref_price, (int, float)) else "—"
    return (f"{arrow} Watchlist Alert • {name} ({platform.upper()})\n"
            f"Current: {int(price):,}c • Change: {pct:+.2f}%\n"
            f"Ref: {rp} ({ref_mode})")

async def _ref_price_for_alert(row: asyncpg.Record) -> Optional[float]:
    mode = row["ref_mode"]
    if mode == "fixed" and row["ref_price"]:
        return float(row["ref_price"])
    # started_price: look up user's watchlist baseline
    if mode == "started_price":
        async with watchlist_pool.acquire() as w:
            r = await w.fetchrow(
                "SELECT started_price FROM watchlist WHERE user_id=$1 AND card_id=$2 AND platform=$3",
                row["user_id"], row["card_id"], row["platform"]
            )
        return float(r["started_price"]) if r and r["started_price"] else None
    # last_close: use latest history point
    try:
        hist = await get_price_history(int(row["card_id"]), row["platform"], "today")
        if hist:
            p = hist[-1]
            v = p.get("price") or p.get("v") or p.get("y")
            return float(v) if v else None
    except Exception:
        return None
    return None

async def _resolve_player_name(card_id: int) -> str:
    try:
        async with player_pool.acquire() as p:
            r = await p.fetchrow("SELECT name FROM fut_players WHERE card_id=$1::text", str(card_id))
        return r["name"] if r and r["name"] else f"Card {card_id}"
    except Exception:
        return f"Card {card_id}"

async def _eval_alerts_for_pair(card_id: int, platform: str, price_now: float) -> int:
    sent = 0
    now = now_utc()
    async with watchlist_pool.acquire() as w:
        rows = await w.fetch("SELECT * FROM watchlist_alerts WHERE card_id=$1 AND platform=$2", card_id, platform)
    if not rows:
        return 0
    name = await _resolve_player_name(card_id)
    for row in rows:
        try:
            if row["quiet_start"] or row["quiet_end"]:
                if is_within_quiet_hours(now, row["quiet_start"], row["quiet_end"]):
                    continue
            last = row["last_alert_at"]
            if last and (now - last).total_seconds() < (row["cooloff_minutes"] * 60):
                continue
            refp = await _ref_price_for_alert(row)
            if not refp:
                continue
            pct = 100.0 * (price_now - refp) / refp
            direction = None
            if pct >= float(row["rise_pct"] or 0):
                direction = "rise"
            elif pct <= -float(row["fall_pct"] or 0):
                direction = "fall"
            if not direction:
                continue

            content = _fmt_alert(name, platform, direction, pct, price_now, row["ref_mode"], refp)
            ok = False
            if row["prefer_dm"] and row["user_discord_id"]:
                ok = await _send_discord_dm(row["user_discord_id"], content)
            if not ok:
                await _send_channel_fallback(row["fallback_channel_id"], content)

            async with watchlist_pool.acquire() as w:
                await w.execute(
                    "INSERT INTO alerts_log (user_id, user_discord_id, card_id, platform, direction, pct, price, ref_mode, ref_price) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                    row["user_id"], row["user_discord_id"], card_id, platform, direction, pct, price_now, row["ref_mode"], refp
                )
                await w.execute("UPDATE watchlist_alerts SET last_alert_at=$1 WHERE id=$2", now, row["id"])
            sent += 1
        except Exception as e:
            logging.warning("alert eval error: %s", e)
    return sent

async def _alerts_poll_loop():
    await asyncio.sleep(3)
    while True:
        try:
            # distinct pairs
            async with watchlist_pool.acquire() as w:
                pairs = await w.fetch("SELECT DISTINCT card_id, platform FROM watchlist_alerts")
            tasks = []
            for rec in pairs:
                cid = int(rec["card_id"]); plat = rec["platform"]
                tasks.append(_poll_pair_once(cid, plat))
            if tasks:
                await asyncio.gather(*tasks)
        except Exception as e:
            logging.warning("poll loop error: %s", e)
        await asyncio.sleep(WATCHLIST_POLL_INTERVAL)

async def _poll_pair_once(card_id: int, platform: str):
    try:
        live = await fetch_price(card_id, platform)
        price = live.get("price")
        if not isinstance(price, (int, float)):
            return
        n = await _eval_alerts_for_pair(card_id, platform, float(price))
        if n:
            logging.info("sent %s alerts for %s/%s", n, card_id, platform)
    except Exception as e:
        logging.debug("poll pair error: %s", e)

# CRUD for alerts
@app.get("/api/watchlist-alerts")
async def list_watchlist_alerts(user_id: str = Depends(get_current_user)):
    async with watchlist_pool.acquire() as w:
        rows = await w.fetch("SELECT * FROM watchlist_alerts WHERE user_id=$1 ORDER BY created_at DESC", user_id)
    return {"items": [dict(r) for r in rows]}

@app.post("/api/watchlist-alerts")
async def create_watchlist_alert(payload: WatchlistAlertCreate, user_id: str = Depends(get_current_user)):
    qs = None; qe = None
    if payload.quiet_start:
        try: qs = datetime.strptime(payload.quiet_start, "%H:%M").time()
        except: qs = None
    if payload.quiet_end:
        try: qe = datetime.strptime(payload.quiet_end, "%H:%M").time()
        except: qe = None
    async with watchlist_pool.acquire() as w:
        await w.execute("""
            INSERT INTO watchlist_alerts (user_id, user_discord_id, card_id, platform, ref_mode, ref_price, rise_pct, fall_pct, cooloff_minutes, quiet_start, quiet_end, prefer_dm, fallback_channel_id)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
        """, user_id, user_id, int(payload.card_id), payload.platform.lower(),
           payload.ref_mode or "last_close", payload.ref_price, payload.rise_pct or 5, payload.fall_pct or 5,
           payload.cooloff_minutes or 30, qs, qe, bool(payload.prefer_dm), payload.fallback_channel_id)
    return {"ok": True}

@app.delete("/api/watchlist-alerts/{alert_id}")
async def delete_watchlist_alert(alert_id: int, user_id: str = Depends(get_current_user)):
    async with watchlist_pool.acquire() as w:
        res = await w.execute("DELETE FROM watchlist_alerts WHERE id=$1 AND user_id=$2", alert_id, user_id)
    if res == "DELETE 0":
        raise HTTPException(404, "Alert not found")
    return {"ok": True}

@app.post("/api/watchlist-alerts/test")
async def test_alert_endpoint(card_id: int, platform: str = "ps", price: Optional[int] = None, user_id: str = Depends(get_current_user)):
    # trigger eval once with forced price or live
    if price is None:
        live = await fetch_price(card_id, platform)
        price = live.get("price")
    if not isinstance(price, (int, float)):
        raise HTTPException(400, "No price available")
    n = await _eval_alerts_for_pair(card_id, platform, float(price))
    return {"sent": n}

# ----------------- ME / SETTINGS / PORTFOLIO -----------------
@app.get("/api/me")
async def get_current_user_info(request: Request):
    uid = request.session.get("user_id")
    if not uid:
        return {"authenticated": False}
    return {
        "authenticated": True,
        "user_id": uid,
        "username": request.session.get("username"),
        "avatar_url": request.session.get("avatar_url"),
        "global_name": request.session.get("global_name"),
    }

@app.get("/api/settings")
async def get_user_settings(user_id: str = Depends(get_current_user), conn=Depends(get_db)):
    try:
        settings_row = await conn.fetchrow("""
            SELECT 
                default_platform, 
                custom_tags, 
                currency_format, 
                theme, 
                timezone, 
                date_format, 
                include_tax_in_profit, 
                default_chart_range, 
                visible_widgets
            FROM usersettings 
            WHERE user_id = $1
        """, user_id)
        
        if settings_row:
            return {
                "default_platform": settings_row["default_platform"],
                "custom_tags": settings_row["custom_tags"] or [],
                "currency_format": settings_row["currency_format"],
                "theme": settings_row["theme"],
                "timezone": settings_row["timezone"],
                "date_format": settings_row["date_format"],
                "include_tax_in_profit": settings_row["include_tax_in_profit"],
                "default_chart_range": settings_row["default_chart_range"],
                "visible_widgets": settings_row["visible_widgets"] or ["profit", "tax", "balance", "trades"]
            }
        else:
            return UserSettings().dict()
    except Exception as e:
        logging.error(f"Error fetching user settings: {e}")
        return UserSettings().dict()

@app.post("/api/settings")
async def update_user_settings(settings: UserSettings, user_id: str = Depends(get_current_user), conn=Depends(get_db)):
    try:
        await conn.execute(
            """
            INSERT INTO usersettings (
                user_id, default_platform, custom_tags, currency_format, theme, 
                timezone, date_format, include_tax_in_profit, default_chart_range, 
                visible_widgets, updated_at
            ) 
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NOW())
            ON CONFLICT (user_id) 
            DO UPDATE SET 
                default_platform = EXCLUDED.default_platform,
                custom_tags = EXCLUDED.custom_tags,
                currency_format = EXCLUDED.currency_format,
                theme = EXCLUDED.theme,
                timezone = EXCLUDED.timezone,
                date_format = EXCLUDED.date_format,
                include_tax_in_profit = EXCLUDED.include_tax_in_profit,
                default_chart_range = EXCLUDED.default_chart_range,
                visible_widgets = EXCLUDED.visible_widgets,
                updated_at = NOW()
            """,
            user_id,
            settings.default_platform,
            json.dumps(settings.custom_tags),
            settings.currency_format,
            settings.theme,
            settings.timezone,
            settings.date_format,
            settings.include_tax_in_profit,
            settings.default_chart_range,
            json.dumps(settings.visible_widgets)
        )
        
        return {"message": "Settings updated successfully"}
    except Exception as e:
        logging.error(f"Error updating user settings: {e}")
        raise HTTPException(status_code=500, detail="Failed to update settings")

@app.post("/api/portfolio/balance")
async def update_starting_balance(request: Request, user_id: str = Depends(get_current_user), conn=Depends(get_db)):
    data = await request.json()
    starting_balance = parse_coin_amount(data.get("starting_balance", 0))
    await conn.execute(
        "INSERT INTO portfolio (user_id, starting_balance) VALUES ($1, $2) "
        "ON CONFLICT (user_id) DO UPDATE SET starting_balance = $2",
        user_id,
        starting_balance,
    )
    return {"message": "Starting balance updated successfully"}

# ----------------- GOALS -----------------
@app.get("/api/goals")
async def get_trading_goals(user_id: str = Depends(get_current_user), conn=Depends(get_db)):
    goals = await conn.fetch("SELECT * FROM trading_goals WHERE user_id=$1 ORDER BY created_at DESC", user_id)
    return {"goals": [dict(g) for g in goals]}

@app.post("/api/goals")
async def create_trading_goal(goal: TradingGoal, user_id: str = Depends(get_current_user), conn=Depends(get_db)):
    await conn.execute(
        """
        INSERT INTO trading_goals (user_id, title, target_amount, target_date, goal_type, is_completed, created_at)
        VALUES ($1, $2, $3, $4, $5, $6, NOW())
        """,
        user_id,
        goal.title,
        goal.target_amount,
        goal.target_date,
        goal.goal_type,
        goal.is_completed,
    )
    return {"message": "Goal created successfully"}

# ----------------- ANALYTICS -----------------
@app.get("/api/analytics/advanced")
async def get_advanced_analytics(user_id: str = Depends(get_current_user), conn=Depends(get_db)):
    daily_profits = await conn.fetch(
        """
        SELECT DATE(timestamp) as date, COALESCE(SUM(profit), 0) as daily_profit, COUNT(*) as trades_count
        FROM trades WHERE user_id=$1 AND timestamp >= NOW() - INTERVAL '30 days'
        GROUP BY DATE(timestamp) ORDER BY date
        """,
        user_id,
    )
    tag_performance = await conn.fetch(
        """
        SELECT tag, COUNT(*) as trade_count, COALESCE(SUM(profit), 0) as total_profit,
               COALESCE(AVG(profit), 0) as avg_profit,
               COUNT(CASE WHEN profit > 0 THEN 1 END) * 100.0 / COUNT(*) as win_rate
        FROM trades WHERE user_id=$1 AND tag IS NOT NULL AND tag != ''
        GROUP BY tag ORDER BY total_profit DESC
        """,
        user_id,
    )
    platform_stats = await conn.fetch(
        """
        SELECT platform, COUNT(*) as trade_count, COALESCE(SUM(profit), 0) as total_profit,
               COALESCE(AVG(profit), 0) as avg_profit
        FROM trades WHERE user_id=$1 GROUP BY platform
        """,
        user_id,
    )
    monthly_summary = await conn.fetch(
        """
        SELECT DATE_TRUNC('month', timestamp) as month, COUNT(*) as trades_count,
               COALESCE(SUM(profit), 0) as total_profit, COALESCE(SUM(ea_tax), 0) as total_tax
        FROM trades WHERE user_id=$1
        GROUP BY DATE_TRUNC('month', timestamp) ORDER BY month DESC LIMIT 12
        """,
        user_id,
    )
    return {
        "daily_profits": [dict(r) for r in daily_profits],
        "tag_performance": [dict(r) for r in tag_performance],
        "platform_stats": [dict(r) for r in platform_stats],
        "monthly_summary": [dict(r) for r in monthly_summary],
    }

# ----------------- BULK / EXPORT / IMPORT / NUKE -----------------
@app.put("/api/trades/bulk")
async def bulk_edit_trades(request: Request, user_id: str = Depends(get_current_user), conn=Depends(get_db)):
    data = await request.json()
    trade_ids = data.get("trade_ids", [])
    updates = data.get("updates", {})
    if not trade_ids or not updates:
        raise HTTPException(status_code=400, detail="trade_ids and updates required")

    set_clauses = []
    params: List[Any] = []

    if "tag" in updates:
        set_clauses.append(f"tag = ${len(params)+1}")
        params.append(updates["tag"])
    if "platform" in updates:
        set_clauses.append(f"platform = ${len(params)+1}")
        params.append(updates["platform"])

    if not set_clauses:
        raise HTTPException(status_code=400, detail="No valid updates provided")

    params.extend([user_id, trade_ids])

    query = (
        f"UPDATE trades SET {', '.join(set_clauses)} "
        f"WHERE user_id = ${len(params)-1} AND trade_id = ANY(${len(params)})"
    )

    await conn.execute(query, *params)
    return {"message": f"Updated {len(trade_ids)} trades successfully"}

@app.get("/api/export/trades")
async def export_trades(format: str = "csv", user_id: str = Depends(get_current_user), conn=Depends(get_db)):
    rows = await conn.fetch("SELECT * FROM trades WHERE user_id=$1 ORDER BY timestamp DESC", user_id)
    data = [dict(r) for r in rows]
    if format.lower() == "json":
        blob = json.dumps(data, indent=2, default=str).encode()
        return StreamingResponse(
            io.BytesIO(blob),
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=trades_export.json"},
        )

    output = io.StringIO()
    if data:
        writer = csv.DictWriter(output, fieldnames=data[0].keys())
        writer.writeheader()
        writer.writerows(data)

    return StreamingResponse(
        io.StringIO(output.getvalue()),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=trades_export.csv"},
    )

@app.post("/api/import/trades")
async def import_trades(file: UploadFile = File(...), user_id: str = Depends(get_current_user), conn=Depends(get_db)):
    contents = await file.read()
    if file.filename.endswith(".json"):
        payload = json.loads(contents.decode("utf-8"))
        trades_to_import = payload if isinstance(payload, list) else [payload]
    elif file.filename.endswith(".csv"):
        reader = csv.DictReader(io.StringIO(contents.decode("utf-8")))
        trades_to_import = list(reader)
    else:
        raise HTTPException(status_code=400, detail="Unsupported file format")

    imported_count = 0
    errors: List[str] = []

    for t in trades_to_import:
        try:
            player = (t.get("player", "") or "").strip()
            version = (t.get("version", "") or "").strip()
            buy = parse_coin_amount(t.get("buy", 0))
            sell = parse_coin_amount(t.get("sell", 0))
            quantity = int(t.get("quantity", 1))
            platform = (t.get("platform", "Console") or "").strip()
            tag = (t.get("tag", "") or "").strip()

            if not player or not version or buy <= 0 or sell <= 0:
                errors.append(f"Invalid trade data: {t}")
                continue

            profit = (sell - buy) * quantity
            ea_tax = int(round(sell * quantity * 0.05))

            await conn.execute(
                """
                INSERT INTO trades (user_id, player, version, buy, sell, quantity, platform, profit, ea_tax, tag, timestamp)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,NOW())
                """,
                user_id,
                player,
                version,
                buy,
                sell,
                quantity,
                platform,
                profit,
                ea_tax,
                tag,
            )
            imported_count += 1
        except Exception as e:
            errors.append(f"Error importing trade {t}: {str(e)}")

    return {
        "message": f"Successfully imported {imported_count} trades",
        "imported_count": imported_count,
        "errors": errors[:10],
    }

@app.delete("/api/data/delete-all")
async def delete_all_user_data(confirm: bool = False, user_id: str = Depends(get_current_user), conn=Depends(get_db)):
    if not confirm:
        raise HTTPException(status_code=400, detail="Confirmation required")
    res = await conn.execute("DELETE FROM trades WHERE user_id=$1", user_id)
    deleted = int(res.split()[-1]) if res.startswith("DELETE ") else 0
    await conn.execute("UPDATE portfolio SET starting_balance = 0 WHERE user_id=$1", user_id)
    return {"message": "All data deleted successfully", "trades_deleted": deleted}

# ----------------- SEARCH PLAYERS -----------------
@app.get("/api/search-players")
async def search_players(q: str = "", pos: Optional[str] = None):
    q = (q or "").strip()
    p = (pos or "").strip().upper() or None

    try:
        async with player_pool.acquire() as conn:
            where = []
            params = []

            if q:
                where.append(f"(LOWER(name) LIKE LOWER($1) OR card_id::text LIKE $1)")
                params.append(f"%{q}%")

            if p:
                params.append(p)
                idx = len(params)
                where.append(f"""
                (
                  UPPER(position) = ${idx}
                  OR (
                    COALESCE(altposition, '') <> ''
                    AND EXISTS (
                      SELECT 1
                      FROM regexp_split_to_table(altposition, '[,;/|\\s]+') ap
                      WHERE UPPER(TRIM(ap)) = ${idx}
                    )
                  )
                )
                """)

            base_where = " AND ".join(where) if where else "TRUE"
            sql = f"""
                SELECT
                  card_id, name, rating, version, image_url, club, league, nation,
                  position, altposition, price
                FROM fut_players
                WHERE {base_where}
                ORDER BY
                  CASE WHEN price IS NULL THEN 1 ELSE 0 END,
                  rating DESC NULLS LAST,
                  name ASC
                LIMIT 50
            """

            rows = await conn.fetch(sql, *params)

        players = [{
            "card_id": int(r["card_id"]),
            "name": r["name"],
            "rating": r["rating"],
            "version": r["version"],
            "image_url": r["image_url"],
            "club": r["club"],
            "league": r["league"],
            "nation": r["nation"],
            "position": r["position"],
            "altposition": r["altposition"],
            "price": r["price"],
        } for r in rows]

        return {"players": players}
    except Exception as e:
        logging.error(f"Player search error: {e}")
        return {"players": [], "error": str(e)}


# ----------------- DEBUG -----------------
@app.get("/api/debug/session")
async def debug_session(req: Request):
    return {
        "cookies_present": bool(req.cookies),
        "session_user_id": req.session.get("user_id"),
        "all_session_keys": list(req.session.keys()),
    }

# ----------------- TRENDING -----------------
async def _enrich_with_meta(items: list[dict]) -> list[dict]:
    if not items:
        return []
    ids = [str(it["card_id"]) for it in items]
    async with player_pool.acquire() as pconn:
        rows = await pconn.fetch(
            """
            SELECT card_id, name, rating, version, image_url, club, league
              FROM fut_players
             WHERE card_id = ANY($1::text[])
            """,
            ids,
        )
    meta = {str(r["card_id"]): dict(r) for r in rows}
    out = []
    for it in items:
        m = meta.get(str(it["card_id"]), {})
        out.append({
            "pid": it["card_id"],
            "name": m.get("name") or f"Card {it['card_id']}",
            "rating": m.get("rating"),
            "version": m.get("version"),
            "image": m.get("image_url"),
            "club": m.get("club"),
            "league": m.get("league"),
            "percent": it["percent"],
            "price_ps": None,
            "price_xb": None,
        })
    return out

async def _attach_prices_ps(items: list[dict]) -> list[dict]:
    tasks = [get_player_price(it["pid"], "ps") for it in items]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for it, val in zip(items, results):
        it["price_ps"] = int(val) if isinstance(val, (int, float)) else None
    return items

@app.get("/api/trending")
async def api_trending(
    type: Literal["risers","fallers"],
    tf: Optional[str] = "24",
):
    kind = (type or "fallers").lower()
    tf_norm = _norm_tf(tf)

    if kind == "fallers":
        items, _ = await _momentum_page_items(tf_norm, 1)
        items.sort(key=lambda x: x["percent"])
        pick = items[:10]
    elif kind == "risers":
        _, html = await _momentum_page_items(tf_norm, 1)
        last = _parse_last_page_number(html)
        items, _ = await _momentum_page_items(tf_norm, last)
        items.sort(key=lambda x: x["percent"], reverse=True)
        pick = items[:10]
    else:
        raise HTTPException(status_code=400, detail="type must be 'risers' or 'fallers'")

    enriched = await _enrich_with_meta(pick)
    enriched = await _attach_prices_ps(enriched)
    return {"type": kind, "timeframe": f"{tf_norm}h", "items": enriched}

# ----------------- COMPARE: side-by-side player data -----------------
def _cmp_now_ms() -> int:
    return int(time.time() * 1000)

def _cmp_window(points: List[Dict[str, Any]], hours: int) -> List[Dict[str, Any]]:
    if not points:
        return []
    cutoff = _cmp_now_ms() - hours * 60 * 60 * 1000
    return [p for p in points if int(p.get("t", 0)) >= cutoff]

def _cmp_chg_pct(points: List[Dict[str, Any]]) -> Optional[float]:
    if len(points) < 2:
        return None
    first = points[0].get("price")
    last = points[-1].get("price")
    if not first:
        return None
    try:
        return round(((last - first) / first) * 100.0, 2)
    except Exception:
        return None

def _cmp_low_high(points: List[Dict[str, Any]]) -> Dict[str, Optional[int]]:
    if not points:
        return {"low": None, "high": None}
    vals = [p.get("price") for p in points if isinstance(p.get("price"), (int, float))]
    return {"low": min(vals) if vals else None, "high": max(vals) if vals else None}

def _cmp_platform(p: str) -> str:
    p = (p or "").lower()
    if p in ("ps", "playstation", "console"):
        return "ps"
    if p in ("xbox", "xb"):
        return "xbox"
    if p in ("pc", "origin"):
        return "pc"
    return "ps"

async def _cmp_price_range_via_futgg(card_id: str) -> Dict[str, Optional[int]]:
    url = f"https://www.fut.gg/api/fut/player-item-definitions/25/{card_id}/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-GB,en;q=0.9",
        "Referer": "https://www.fut.gg/",
        "Origin": "https://www.fut.gg",
    }
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, headers=headers, timeout=15) as r:
                if r.status != 200:
                    return {"min": None, "max": None}
                js = await r.json()
        data = js.get("data") or {}
        pr = data.get("priceRange") or {}
        mn = pr.get("min")
        mx = pr.get("max")
        if (mn is None or mx is None) and "ranges" in data:
            rng = (data["ranges"] or {}).get("priceRange") or {}
            mn = mn if mn is not None else rng.get("min")
            mx = mx if mx is not None else rng.get("max")
        return {"min": int(mn) if isinstance(mn, (int, float)) else None,
                "max": int(mx) if isinstance(mx, (int, float)) else None}
    except Exception:
        return {"min": None, "max": None}

async def _cmp_recent_sales_futbin(card_id: str, platform: str) -> List[Dict[str, Any]]:
    plat = platform if platform in ("ps", "xbox", "pc") else "ps"
    url = f"https://www.futbin.com/25/sales/{card_id}?platform={plat}"
    out: List[Dict[str, Any]] = []
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, timeout=20, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-GB,en;q=0.9",
                "Referer": "https://www.futbin.com/",
            }) as r:
                if r.status != 200:
                    return out
                html = await r.text()
        m = re.search(r"var\s+table_data\s*=\s*(\[\s*{.*?}\s*\]);", html, re.S)
        if not m:
            return out
        raw = m.group(1)
        for price_str, when in re.findall(r"\{[^}]*price[^}\d]*(\d+)[^}]*time[^\"]*\"([^\"]+)\"[^}]*\}", raw):
            try:
                out.append({"price": int(price_str), "time": when})
            except Exception:
                continue
        return out[:20]
    except Exception:
        return []

@app.get("/api/player-compare")
async def player_compare(
    ids: str = Query(..., description="CSV of 1 or 2 card_ids (as stored in fut_players)"),
    platform: str = Query("ps", description="ps|xbox|pc|console"),
    include_pc: bool = Query(True, description="Also return PC current price"),
    include_sales: bool = Query(True, description="Include recent sales list"),
):
    raw_ids = [x.strip() for x in ids.split(",") if x.strip()]
    if not raw_ids or len(raw_ids) > 2:
        raise HTTPException(status_code=400, detail="Provide 1 or 2 ids")
    plat = _cmp_platform(platform)

    async with player_pool.acquire() as pconn:
        meta_rows = await pconn.fetch(
            """
            SELECT card_id, name, rating, position, league, nation, club, image_url
            FROM fut_players
            WHERE card_id = ANY($1::text[])
            """,
            raw_ids,
        )
    meta = {str(r["card_id"]): dict(r) for r in meta_rows}

    players_out: List[Dict[str, Any]] = []
    for cid_str in raw_ids:
        m = meta.get(cid_str, {})
        try:
            cid_int = int(cid_str)
        except Exception:
            cid_int = None

        console_price = await get_player_price(cid_int, plat) if cid_int is not None else None
        pc_price = await get_player_price(cid_int, "pc") if (cid_int is not None and include_pc) else None

        hist_short = await get_price_history(cid_int, plat, "today") if cid_int is not None else []
        try:
            hist_long = await get_price_history(cid_int, plat, "week") if cid_int is not None else []
        except Exception:
            hist_long = hist_short

        w4 = _cmp_window(hist_short, 4)
        w24 = _cmp_window(hist_short, 24)
        chg4 = _cmp_chg_pct(w4)
        chg24 = _cmp_chg_pct(w24)
        lohi = _cmp_low_high(w24)

        pr = await _cmp_price_range_via_futgg(cid_str)
        sales = await _cmp_recent_sales_futbin(cid_str, plat) if include_sales else []

        players_out.append({
            "id": cid_str,
            "name": m.get("name") or f"Card {cid_str}",
            "rating": m.get("rating"),
            "position": m.get("position"),
            "league": m.get("league"),
            "nation": m.get("nation"),
            "club": m.get("club"),
            "image": m.get("image_url"),
            "prices": {"console": console_price, "pc": pc_price},
            "priceRange": pr,
            "trend": {
                "chg4hPct": chg4,
                "chg24hPct": chg24,
                "low24h": lohi["low"],
                "high24h": lohi["high"],
            },
            "history": {
                "short": hist_short,
                "long": hist_long,
            },
            "recentSales": sales,
        })

    return {"players": players_out}

# ----------------- Next Promo endpoint -----------------
@app.get("/api/events/next")
async def next_event():
    # If no events, fallback to next 18:00 UK "Daily Content Drop"
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT name, kind, start_at, confidence FROM events WHERE start_at > $1 ORDER BY start_at ASC LIMIT 1",
            now_utc()
        )
    if row:
        return {"name": row["name"], "kind": row["kind"], "start_at": row["start_at"].isoformat(), "confidence": "confidence" in row and row["confidence"] or "heuristic"}
    nxt = next_daily_london_hour(18)
    return {"name": "Daily Content Drop", "kind": "promo", "start_at": nxt.isoformat(), "confidence": "heuristic"}

# ----------------- Deal Confidence endpoint -----------------
@app.get("/api/deal-confidence/{card_id}")
async def deal_confidence(card_id: int, platform: str = "ps"):
    try:
        hist = await get_price_history(card_id, platform, "today")
    except Exception as e:
        raise HTTPException(502, f"history error: {e}")
    prices = [p.get("price") or p.get("v") or p.get("y") for p in hist if (p.get("price") or p.get("v") or p.get("y"))]
    if len(prices) < 6:
        live = await fetch_price(card_id, platform)
        if isinstance(live.get("price"), (int, float)):
            prices = [int(live["price"])] * 6
        else:
            return {"score": 0, "components": {}, "note": "no data"}

    n = len(prices)
    def _slope(xs):
        m = len(xs)
        if m < 2: return 0.0
        xb = (m-1)/2.0
        yb = sum(xs)/m
        num = sum((i-xb)*(y-yb) for i,y in enumerate(xs))
        den = sum((i-xb)**2 for i in range(m))
        return num/den if den else 0.0
    last_q = prices[max(0, n - max(6, n//4)):]
    sl = _slope(last_q)
    momentum4h = 1.0 if sl > 0 else 0.0
    first = prices[:n//2] or prices
    second = prices[n//2:] or prices
    regime = 1.0 if (sum(second)/len(second) >= sum(first)/len(first)) else 0.0
    diffs = [abs(prices[i]-prices[i-1]) for i in range(1, n)]
    vol_abs = sum(diffs)/len(diffs) if diffs else 0.0
    avgp = sum(prices)/len(prices)
    volRisk = min(1.0, (vol_abs/avgp) if avgp else 1.0)
    liquidity = min(1.0, max(0.0, (n-6)/90))
    wnd = prices[-min(12, n):]
    if wnd:
        lo, hi = min(wnd), max(wnd)
        spread_proxy = (hi-lo)/hi if hi else 0.1
    else:
        spread_proxy = 0.1
    recent_hi = max(wnd) if wnd else max(prices)
    cur = prices[-1]
    srRoom = (recent_hi - cur)/recent_hi if recent_hi else 0.0
    secs = (next_daily_london_hour(18) - now_utc()).total_seconds()
    catalyst = max(0.0, min(1.0, 1 - abs(secs)/(6*3600)))

    score = 100 * (0.22*momentum4h + 0.14*regime + 0.16*(1-volRisk) + 0.18*liquidity + 0.12*(1-spread_proxy) + 0.10*srRoom + 0.08*catalyst)
    score = max(0.0, min(100.0, score))
    return {"score": round(score,1), "components": {
        "momentum4h": round(momentum4h,3), "regimeAgreement": regime, "volRisk": round(volRisk,3),
        "liquidity": round(liquidity,3), "spreadProxy": round(spread_proxy,3), "srRoom": round(srRoom,3),
        "catalystBoost": round(catalyst,3)
    }}

# ----------------- Backtest endpoint (simple dip->tp/sl) -----------------
@app.post("/api/backtest")
async def backtest(payload: Dict[str, Any]):
    players: List[int] = payload.get("players") or []
    platform: str = payload.get("platform", "ps")
    window_days: int = int(payload.get("window_days", 7))
    entry = payload.get("entry", {"type":"dip_from_high","x_pct":5})
    exit_ = payload.get("exit", {"tp_pct":7,"sl_pct":4,"max_hold_h":24})
    size = payload.get("size", {"coins":200000})
    concurrency = int(payload.get("concurrency", 3))

    if not players:
        raise HTTPException(400, "players required")

    def norm_series(hist: List[dict]) -> List[Tuple[int, float]]:
        out = []
        for p in hist:
            t = p.get("t") or p.get("ts") or p.get("time")
            v = p.get("price") or p.get("v") or p.get("y")
            if t is not None and v is not None:
                out.append((int(t), float(v)))
        return out

    equity = []; all_trades = []; cash = 0.0; open_trades = []
    for pid in players:
        hist = await get_price_history(pid, platform, "today")
        pts = norm_series(hist)[-(window_days*96):]
        if len(pts) < 16: 
            continue
        recent_high = max(v for _,v in pts[:8])
        for i in range(8, len(pts)):
            t, px = pts[i]
            if px > recent_high: recent_high = px
            # exits
            keep = []
            for tr in open_trades:
                tp = tr["entry_price"]*(1+exit_.get("tp_pct",7)/100.0)
                sl = tr["entry_price"]*(1-exit_.get("sl_pct",4)/100.0)
                hold_h = (t - tr["t_in"]) / 3600000.0
                reason = None
                if px >= tp: reason="tp"
                elif px <= sl: reason="sl"
                elif hold_h >= exit_.get("max_hold_h",24): reason="time"
                if reason:
                    pnl = (px - tr["entry_price"]) * tr["qty"]
                    pnl_after_tax = pnl * 0.95
                    all_trades.append({**tr, "t_out": t, "px_out": px, "exit": reason, "pnl_after_tax": pnl_after_tax})
                    cash += pnl_after_tax
                else:
                    keep.append(tr)
            open_trades = keep
            # entries
            if len(open_trades) < concurrency and entry.get("type") == "dip_from_high":
                x = entry.get("x_pct", 5)
                if recent_high > 0 and ((recent_high - px)/recent_high)*100 >= x:
                    qty = max(1, int(size.get("coins",200000) // px))
                    open_trades.append({"player_id": pid, "t_in": t, "px_in": px, "entry_price": px, "qty": qty})
            equity.append({"t": t, "value": cash + sum((px - tr["entry_price"]) * tr["qty"] * 0.95 for tr in open_trades)})

    wins = [tr for tr in all_trades if tr["pnl_after_tax"] > 0]
    summary = {
        "trades": len(all_trades),
        "net_profit": round(sum(tr["pnl_after_tax"] for tr in all_trades), 2),
        "win_rate": round(100*len(wins)/len(all_trades), 1) if all_trades else 0.0,
        "avg_hold_h": round(sum(((tr["t_out"]-tr["t_in"])/3600000.0) for tr in all_trades)/len(all_trades), 2) if all_trades else 0.0,
    }
    return {"equity": equity, "summary": summary, "trades": all_trades}

# ----------------- INCLUDE OPTIONAL ROUTERS -----------------
try:
    from app.routers.squad import router as squad_router  # type: ignore
    app.include_router(squad_router, prefix="/api")
    logging.info("✅ Squad router loaded")
except Exception as e:
    logging.warning("⚠️ Squad router not loaded: %s", e)

# ----------------- ENTRYPOINT -----------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT)import os
import re
import io
import csv
import jwt
import time
import json
import asyncio
import logging
import secrets
import aiohttp
import asyncpg

from bs4 import BeautifulSoup
from types import SimpleNamespace
from urllib.parse import urlencode
from contextlib import asynccontextmanager, suppress
from typing import Any, Dict, List, Literal, Optional, Tuple

from dotenv import load_dotenv
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta, timezone, time as dt_time

from fastapi import (
    FastAPI, APIRouter, Request, HTTPException, Depends,
    UploadFile, File, Query
)
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel

from app.services.price_history import get_price_history
from app.services.prices import get_player_price
from app.routers.smart_buy import router as smart_buy_router

# ✅ Trade Finder router
from app.routers.trade_finder import router as trade_finder_router


# ----------------- BOOTSTRAP -----------------
logging.basicConfig(level=logging.INFO)
load_dotenv()

# --------- ENV ---------
required_env_vars = ["DATABASE_URL", "DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "DISCORD_REDIRECT_URI"]
missing_vars = [var for var in required_env_vars if not os.getenv(var)]
if missing_vars:
    raise ValueError(f"Missing required environment variables: {missing_vars}")

DATABASE_URL = os.getenv("DATABASE_URL")
PLAYER_DATABASE_URL = os.getenv("PLAYER_DATABASE_URL", DATABASE_URL)
WATCHLIST_DATABASE_URL = os.getenv("WATCHLIST_DATABASE_URL", DATABASE_URL)

DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI")
SECRET_KEY = os.getenv("SECRET_KEY")
FRONTEND_URL = (os.getenv("FRONTEND_URL", "https://app.futhub.co.uk").rstrip("/"))
PORT = int(os.getenv("PORT", 8000))

ENV = os.getenv("ENV", "production").lower()
IS_PROD = ENV in ("prod", "production")

# JWT / Discord
JWT_PRIVATE_KEY = os.getenv("JWT_PRIVATE_KEY", "dev-secret-change-me")
JWT_ISSUER = os.getenv("JWT_ISSUER", "fut-dashboard")
JWT_TTL_SECONDS = int(os.getenv("JWT_TTL_SECONDS", "2592000"))  # 30 days
DISCORD_OAUTH_AUTHORIZE = "https://discord.com/api/oauth2/authorize"
DISCORD_OAUTH_TOKEN = "https://discord.com/api/oauth2/token"
DISCORD_USERS_ME = "https://discord.com/api/users/@me"
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_SERVER_ID = os.getenv("DISCORD_SERVER_ID")

# Watchlist alert env
WATCHLIST_FALLBACK_CHANNEL_ID = os.getenv("WATCHLIST_FALLBACK_CHANNEL_ID")
WATCHLIST_POLL_INTERVAL = int(os.getenv("WATCHLIST_POLL_INTERVAL", "60"))  # seconds

# ephemeral state store
OAUTH_STATE: Dict[str, Dict[str, Any]] = {}

if not SECRET_KEY:
    raise ValueError("SECRET_KEY environment variable is required")

# --------- FUT.GG / Watchlist config ---------
FUTGG_BASE = "https://www.fut.gg/api/fut/player-prices/25"
PRICE_CACHE_TTL = 5  # seconds
_price_cache: Dict[str, Dict[str, Any]] = {}

# ----------------- FUT.GG MOMENTUM (NEW) -----------------
MOMENTUM_BASE = "https://www.fut.gg/players/momentum"
MOMENTUM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": "https://www.fut.gg/",
}
_CARD_HREF_RE = re.compile(r"/players/(\d+)-[a-z0-9-]+/25-(\d+)/?", re.IGNORECASE)

def _norm_tf(tf: Optional[str]) -> str:
    if not tf:
        return "24"
    tf = tf.lower().strip()
    if tf.endswith("h"):
        tf = tf[:-1]
    return tf if tf in ("6", "12", "24") else "24"

async def _fetch_momentum_page(tf: str, page: int) -> str:
    url = f"{MOMENTUM_BASE}/{tf}/?page={page}"
    timeout = aiohttp.ClientTimeout(total=12)
    async with aiohttp.ClientSession(timeout=timeout, headers=MOMENTUM_HEADERS) as sess:
        async with sess.get(url) as r:
            if r.status != 200:
                raise HTTPException(status_code=502, detail=f"MOMENTUM {r.status}")
            return await r.text()

def _parse_last_page_number(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    nums = []
    for a in soup.find_all("a"):
        href = a.get("href") or ""
        if "page=" in href:
            try:
                n = int(href.split("page=", 1)[1].split("&", 1)[0])
                nums.append(n)
            except Exception:
                continue
        else:
            t = a.text.strip()
            if t.isdigit():
                nums.append(int(t))
    return max(nums) if nums else 1

def _extract_items(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    tiles = []
    for a in soup.find_all("a", href=True):
        m = _CARD_HREF_RE.search(a["href"])
        if not m:
            continue
        node = a
        for _ in range(4):
            if node is None or node.name == "body":
                break
            txt = node.get_text(separator=" ", strip=True)
            if "%" in txt:
                tiles.append((m.group(2), txt))
                break
            node = node.parent

    items = []
    pct_re = re.compile(r"([+\-]?\s?\d+(?:\.\d+)?)\s*%")
    seen = set()
    for cid, text in tiles:
        if cid in seen:
            continue
        m = pct_re.search(text)
        if not m:
            continue
        try:
            pct = float(m.group(1).replace(" ", ""))
            items.append({"card_id": int(cid), "percent": pct})
            seen.add(cid)
        except Exception:
            continue
    return items

async def _momentum_page_items(tf: str, page: int) -> tuple[list[dict], str]:
    html = await _fetch_momentum_page(tf, page)
    return _extract_items(html), html


# ----------------- HELPERS -----------------
def parse_coin_amount(v) -> int:
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(round(float(v)))
    s = str(v).strip().lower()
    s = re.sub(r"[\s_]", "", s)
    s = re.sub(r"(?<=\d)[,\.](?=\d{3}\b)", "", s)
    s = s.replace(",", ".")
    if s.endswith("kk"):
        try: return int(round(float(s[:-2]) * 1_000_000))
        except: return 0
    if s.endswith("k"):
        try: return int(round(float(s[:-1]) * 1_000))
        except: return 0
    if s.endswith("m"):
        try: return int(round(float(s[:-1]) * 1_000_000))
        except: return 0
    try:
        return int(round(float(s)))
    except:
        return 0

# Time helpers
LONDON = ZoneInfo("Europe/London")
UTC = timezone.utc
def now_utc() -> datetime:
    return datetime.now(UTC)
def london_now() -> datetime:
    return datetime.now(LONDON)
def next_daily_london_hour(hour: int = 18) -> datetime:
    ln = london_now()
    tgt = ln.replace(hour=hour, minute=0, second=0, microsecond=0)
    if tgt <= ln:
        tgt = tgt + timedelta(days=1)
    return tgt.astimezone(UTC)
def is_within_quiet_hours(dt: datetime, quiet_start: Optional[dt_time], quiet_end: Optional[dt_time]) -> bool:
    if not quiet_start or not quiet_end:
        return False
    t = dt.astimezone(LONDON).time()
    if quiet_start <= quiet_end:
        return quiet_start <= t < quiet_end
    return t >= quiet_start or t < quiet_end


# ----------------- MODELS -----------------
class UserSettings(BaseModel):
    default_platform: Optional[str] = "Console"
    custom_tags: Optional[List[str]] = []
    currency_format: Optional[str] = "coins"
    theme: Optional[str] = "dark"
    timezone: Optional[str] = "UTC"
    date_format: Optional[str] = "US"
    include_tax_in_profit: Optional[bool] = True
    default_chart_range: Optional[str] = "30d"
    visible_widgets: Optional[List[str]] = ["profit", "tax", "balance", "trades"]

class TradingGoal(BaseModel):
    title: str
    target_amount: int
    target_date: Optional[str] = None
    goal_type: str = "profit"
    is_completed: bool = False

class ExtSale(BaseModel):
    trade_id: int
    player_name: str
    card_version: Optional[str] = None
    buy_price: Optional[int] = None
    sell_price: int
    timestamp_ms: int

class TradeUpdate(BaseModel):
    player: Optional[str] = None
    version: Optional[str] = None
    quantity: Optional[int] = None
    buy: Optional[Any] = None
    sell: Optional[Any] = None
    platform: Optional[str] = None
    tag: Optional[str] = None
    notes: Optional[str] = None
    timestamp: Optional[str] = None

class WatchlistCreate(BaseModel):
    card_id: int
    player_name: str
    version: Optional[str] = None
    platform: str  # "ps" | "xbox"
    notes: Optional[str] = None

# Alerts config
class WatchlistAlertCreate(BaseModel):
    card_id: int
    platform: str  # ps|xbox|pc
    rise_pct: Optional[float] = 5
    fall_pct: Optional[float] = 5
    ref_mode: Optional[str] = "last_close"  # last_close | fixed | started_price
    ref_price: Optional[float] = None
    cooloff_minutes: Optional[int] = 30
    quiet_start: Optional[str] = None  # "22:00"
    quiet_end: Optional[str] = None    # "07:00"
    prefer_dm: Optional[bool] = True
    fallback_channel_id: Optional[str] = None


# ----------------- POOLS & LIFESPAN -----------------
pool = None
player_pool = None
watchlist_pool = None
_watchlist_task: Optional[asyncio.Task] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool, player_pool, watchlist_pool, _watchlist_task

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)

    if PLAYER_DATABASE_URL == DATABASE_URL:
        player_pool = pool
    else:
        player_pool = await asyncpg.create_pool(PLAYER_DATABASE_URL, min_size=1, max_size=10)

    if WATCHLIST_DATABASE_URL == DATABASE_URL:
        watchlist_pool = pool
    else:
        watchlist_pool = await asyncpg.create_pool(WATCHLIST_DATABASE_URL, min_size=1, max_size=10)

    # ✅ expose pools to routers/services that access request.app.state.*
    app.state.pool = pool
    app.state.player_pool = player_pool
    app.state.watchlist_pool = watchlist_pool

    # Create all tables in MAIN database
    async with pool.acquire() as conn:
        # Portfolio table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS portfolio (
                id SERIAL PRIMARY KEY,
                user_id VARCHAR(255) UNIQUE NOT NULL,
                starting_balance INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Trades table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id BIGSERIAL PRIMARY KEY,
                user_id VARCHAR(255) NOT NULL,
                player VARCHAR(255) NOT NULL,
                version VARCHAR(100),
                buy INTEGER NOT NULL,
                sell INTEGER NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                platform VARCHAR(50),
                profit INTEGER,
                ea_tax INTEGER,
                tag VARCHAR(100),
                notes TEXT,
                timestamp TIMESTAMPTZ DEFAULT NOW(),
                trade_id BIGINT
            )
        """)

        # User settings table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS usersettings (
                id SERIAL PRIMARY KEY,
                user_id VARCHAR(255) UNIQUE NOT NULL,
                default_platform VARCHAR(50) DEFAULT 'Console',
                custom_tags JSONB DEFAULT '[]',
                currency_format VARCHAR(20) DEFAULT 'coins',
                theme VARCHAR(20) DEFAULT 'dark',
                timezone VARCHAR(50) DEFAULT 'UTC',
                date_format VARCHAR(10) DEFAULT 'US',
                include_tax_in_profit BOOLEAN DEFAULT true,
                default_chart_range VARCHAR(10) DEFAULT '30d',
                visible_widgets JSONB DEFAULT '["profit", "tax", "balance", "trades"]',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # User profiles table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_profiles (
                id SERIAL PRIMARY KEY,
                user_id VARCHAR(255) UNIQUE NOT NULL,
                username VARCHAR(255),
                avatar_url TEXT,
                global_name VARCHAR(255),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Trading goals table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS trading_goals (
                id SERIAL PRIMARY KEY,
                user_id VARCHAR(255) NOT NULL,
                title VARCHAR(255) NOT NULL,
                target_amount INTEGER NOT NULL,
                target_date DATE,
                goal_type VARCHAR(50) DEFAULT 'profit',
                is_completed BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            )
        """)

        # Extension trades table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS fut_trades (
              id           BIGSERIAL PRIMARY KEY,
              discord_id   TEXT NOT NULL,
              trade_id     BIGINT NOT NULL,
              player_name  TEXT NOT NULL,
              card_version TEXT,
              buy_price    INTEGER,
              sell_price   INTEGER NOT NULL,
              ts           TIMESTAMP WITH TIME ZONE NOT NULL,
              source       TEXT DEFAULT 'webapp'
            )
        """)

        # Events table
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
          id BIGSERIAL PRIMARY KEY,
          name TEXT NOT NULL,
          kind TEXT NOT NULL,
          start_at TIMESTAMPTZ NOT NULL,
          end_at TIMESTAMPTZ,
          confidence TEXT NOT NULL DEFAULT 'heuristic',
          source TEXT NOT NULL DEFAULT 'rule:18:00',
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )""")

        # Smart Buy tables - ALL in main database
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS smart_buy_feedback (
                id SERIAL PRIMARY KEY,
                card_id BIGINT NOT NULL,
                action TEXT NOT NULL,  -- bought|ignored|watchlisted
                notes TEXT DEFAULT '',
                platform TEXT DEFAULT 'ps',
                ts TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS smart_buy_market_cache (
                id SMALLINT PRIMARY KEY DEFAULT 1,
                payload JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS smart_buy_suggestions (
                id BIGSERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                card_id TEXT NOT NULL,
                suggestion_type VARCHAR(50) NOT NULL,
                current_price INTEGER NOT NULL,
                target_price INTEGER NOT NULL,
                expected_profit INTEGER NOT NULL,
                risk_level VARCHAR(20) NOT NULL,
                confidence_score INTEGER NOT NULL,
                priority_score INTEGER NOT NULL,
                reasoning TEXT NOT NULL,
                time_to_profit VARCHAR(50),
                platform VARCHAR(10) NOT NULL,
                market_state VARCHAR(30) NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                expires_at TIMESTAMP WITH TIME ZONE
            )
        """)
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS market_states (
                id BIGSERIAL PRIMARY KEY,
                platform VARCHAR(10) NOT NULL,
                state VARCHAR(30) NOT NULL,
                confidence_score INTEGER NOT NULL,
                detected_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                indicators JSONB
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS smart_buy_preferences (
                id BIGSERIAL PRIMARY KEY,
                user_id TEXT UNIQUE NOT NULL,
                default_budget INTEGER DEFAULT 100000,
                risk_tolerance VARCHAR(20) DEFAULT 'moderate',
                preferred_time_horizon VARCHAR(20) DEFAULT 'short',
                preferred_categories JSONB DEFAULT '[]'::jsonb,
                excluded_positions JSONB DEFAULT '[]'::jsonb,
                preferred_leagues JSONB DEFAULT '[]'::jsonb,
                preferred_nations JSONB DEFAULT '[]'::jsonb,
                min_rating INTEGER DEFAULT 75,
                max_rating INTEGER DEFAULT 95,
                min_profit INTEGER DEFAULT 1000,
                notifications_enabled BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)

        # Create indexes
        await conn.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS trade_id BIGINT")
        await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS trades_user_trade_uidx ON trades (user_id, trade_id)")
        await conn.execute("DROP INDEX IF EXISTS idx_trades_date")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_user_ts ON trades(user_id, timestamp)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_tag ON trades(user_id, tag)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_platform ON trades(user_id, platform)")
        await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS fut_trades_uidx ON fut_trades (discord_id, trade_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_at)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind)")
        
        # Smart Buy indexes
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_smart_buy_feedback_card ON smart_buy_feedback(card_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_smart_buy_feedback_action ON smart_buy_feedback(action)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_smart_buy_feedback_ts ON smart_buy_feedback(ts)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_smart_buy_suggestions_user_created ON smart_buy_suggestions(user_id, created_at DESC)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_smart_buy_suggestions_card_platform ON smart_buy_suggestions(card_id, platform)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_market_states_platform_detected ON market_states(platform, detected_at DESC)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_smart_buy_preferences_user ON smart_buy_preferences(user_id)")

        # Backfill trade_id for existing trades
        await conn.execute("""
            WITH to_fix AS (
              SELECT ctid, user_id,
                     ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY timestamp, player) AS rn
              FROM trades
              WHERE trade_id IS NULL
            )
            UPDATE trades t
               SET trade_id = ((EXTRACT(EPOCH FROM NOW())*1000)::bigint) + tf.rn
            FROM to_fix tf
            WHERE t.ctid = tf.ctid AND t.trade_id IS NULL
        """)

    # Create watchlist tables in WATCHLIST database
    async with watchlist_pool.acquire() as wconn:
        await wconn.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
              id SERIAL PRIMARY KEY,
              user_id TEXT NOT NULL,
              card_id BIGINT NOT NULL,
              player_name TEXT NOT NULL,
              version TEXT,
              platform TEXT NOT NULL,
              started_price INTEGER NOT NULL,
              started_at TIMESTAMP NOT NULL DEFAULT NOW(),
              last_price INTEGER,
              last_checked TIMESTAMP,
              notes TEXT
            )
        """)
        await wconn.execute("CREATE INDEX IF NOT EXISTS idx_watchlist_user ON watchlist(user_id)")
        await wconn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_watchlist_unique
            ON watchlist(user_id, card_id, platform)
        """)

        # Alerts config + log
        await wconn.execute("""
        CREATE TABLE IF NOT EXISTS watchlist_alerts (
          id BIGSERIAL PRIMARY KEY,
          user_id TEXT NOT NULL,
          user_discord_id TEXT,
          card_id BIGINT NOT NULL,
          platform TEXT NOT NULL CHECK (platform IN ('ps','xbox','pc')),
          ref_mode TEXT NOT NULL DEFAULT 'last_close', -- last_close | fixed | started_price
          ref_price NUMERIC,
          rise_pct NUMERIC DEFAULT 5,
          fall_pct NUMERIC DEFAULT 5,
          cooloff_minutes INT NOT NULL DEFAULT 30,
          quiet_start TIME,
          quiet_end TIME,
          prefer_dm BOOLEAN NOT NULL DEFAULT TRUE,
          fallback_channel_id TEXT,
          last_alert_at TIMESTAMPTZ,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )""")
        await wconn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_user ON watchlist_alerts(user_id)")
        await wconn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_pair ON watchlist_alerts(card_id, platform)")

        await wconn.execute("""
        CREATE TABLE IF NOT EXISTS alerts_log (
          id BIGSERIAL PRIMARY KEY,
          user_id TEXT NOT NULL,
          user_discord_id TEXT,
          card_id BIGINT NOT NULL,
          platform TEXT NOT NULL,
          direction TEXT NOT NULL,
          pct NUMERIC NOT NULL,
          price NUMERIC NOT NULL,
          ref_mode TEXT NOT NULL,
          ref_price NUMERIC,
          sent_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )""")
        await wconn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_user_time ON alerts_log(user_id, sent_at)")

    # Start alerts loop
    _watchlist_task = asyncio.create_task(_alerts_poll_loop())
    logging.info("✅ All tables created successfully")
    logging.info("✅ Watchlist alerts loop started (%ss)", WATCHLIST_POLL_INTERVAL)

    try:
        yield
    finally:
        if _watchlist_task:
            _watchlist_task.cancel()
            with suppress(asyncio.CancelledError):
                await _watchlist_task
        to_close = {pool, player_pool, watchlist_pool}
        for p in to_close:
            if p is not None:
                await p.close()


# ----------------- APP & MIDDLEWARE -----------------
app = FastAPI(lifespan=lifespan)

FRONTEND_URL = os.getenv("FRONTEND_ORIGIN", "https://app.futhub.co.uk")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        FRONTEND_URL,
        "https://app.futhub.co.uk",
        "https://www.futhub.co.uk",
        "https://futhub.co.uk",       # if you ever serve the apex directly
        "http://localhost:5173",
        "http://localhost:3000",
    ],
    allow_origin_regex=r"^(https://.*\.railway\.app|chrome-extension://.*)$",
    allow_credentials=True,
    allow_methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS"],
    allow_headers=["Authorization","Content-Type","X-Requested-With","Accept"],
    expose_headers=["Content-Disposition"],
    max_age=600,
)

app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    same_site="none" if IS_PROD else "lax",
    https_only=IS_PROD,
)

# ✅ mount Trade Finder API
app.include_router(trade_finder_router)

app.include_router(smart_buy_router, prefix="/api")


# ----------------- DEPENDENCIES & HELPERS -----------------
async def get_db():
    async with pool.acquire() as connection:
        yield connection

async def get_watchlist_db():
    async with watchlist_pool.acquire() as connection:
        yield connection

def get_current_user(request: Request) -> str:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_id

async def get_discord_user_info(access_token: str):
    async with aiohttp.ClientSession() as session:
        async with session.get(DISCORD_USERS_ME, headers={"Authorization": f"Bearer {access_token}"}) as resp:
            if resp.status != 200:
                return None
            return await resp.json()

async def check_server_membership(user_id: str) -> bool:
    if not (DISCORD_BOT_TOKEN and DISCORD_SERVER_ID):
        return True
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://discord.com/api/guilds/{DISCORD_SERVER_ID}/members/{user_id}",
                headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
            ) as resp:
                return resp.status == 200
    except Exception:
        return False

def issue_extension_token(discord_id: str) -> str:
    now = int(time.time())
    payload = {"sub": discord_id, "scope": "trade:ingest", "iat": now, "exp": now + JWT_TTL_SECONDS, "iss": JWT_ISSUER}
    return jwt.encode(payload, JWT_PRIVATE_KEY, algorithm="HS256")

def require_extension_jwt(request: Request):
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = auth[7:]
    try:
        payload = jwt.decode(token, JWT_PRIVATE_KEY, algorithms=["HS256"], issuer=JWT_ISSUER)
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")
    return SimpleNamespace(discord_id=payload.get("sub"))

# --------- FUT.GG price fetch ---------
async def fetch_price(card_id: int, platform: str) -> Dict[str, Any]:
    platform = (platform or "").lower()
    key = f"{card_id}|{platform}"
    now = time.time()

    if key in _price_cache and (now - _price_cache[key]["at"] < PRICE_CACHE_TTL):
        c = _price_cache[key]
        return {"price": c["price"], "isExtinct": c["isExtinct"], "updatedAt": c["updatedAt"]}

    url = f"{FUTGG_BASE}/{card_id}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-GB,en;q=0.9",
        "Referer": "https://www.fut.gg/",
        "Origin": "https://www.fut.gg",
    }

    async with aiohttp.ClientSession() as sess:
        async with sess.get(url, headers=headers) as r:
            if r.status != 200:
                cached = _price_cache.get(key)
                if cached:
                    return {"price": cached["price"], "isExtinct": cached["isExtinct"], "updatedAt": cached["updatedAt"]}
                raise HTTPException(status_code=502, detail="Failed to fetch price")
            data = await r.json()

    current = (data.get("data") or {}).get("currentPrice") or {}
    price = current.get("price")
    is_extinct = current.get("isExtinct", False)
    updated_at = current.get("priceUpdatedAt")

    _price_cache[key] = {"at": now, "price": price, "isExtinct": is_extinct, "updatedAt": updated_at}
    return {"price": price, "isExtinct": is_extinct, "updatedAt": updated_at}


# ----------------- EXTENSION ROUTER (no NameError) -----------------
ext_router = APIRouter()

@ext_router.get("/ext/ping")
async def ext_ping(auth = Depends(require_extension_jwt)):
    """Sanity check for the extension – proves the JWT is valid."""
    return {"ok": True, "sub": auth.discord_id}

@ext_router.post("/ext/trades")
async def ext_add_trade(
    sale: ExtSale,
    auth = Depends(require_extension_jwt),   # extension JWT, not web session
    conn = Depends(get
