# app/services/aggregate.py
from __future__ import annotations
from datetime import datetime, timezone
from collections import defaultdict
from typing import Dict, Tuple

FRAME_S = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}

async def aggregate_ticks_to_candles(db, timeframe: str = "15m", since_hours: int = 48) -> Dict[str, int]:
    if timeframe not in FRAME_S:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    sec = FRAME_S[timeframe]

    rows = await db.fetch(
        """
        SELECT player_card_id, platform, ts, price
        FROM public.fut_ticks
        WHERE ts >= NOW() - ($1 || ' hours')::interval
        ORDER BY player_card_id, platform, ts ASC
        """,
        since_hours,
    )
    if not rows:
        return {"updated": 0}

    buckets: Dict[Tuple[str, str, int], list] = defaultdict(list)
    for r in rows:
        ts = r["ts"].replace(tzinfo=timezone.utc)
        epoch = int(ts.timestamp())
        bucket = epoch - (epoch % sec)
        key = (r["player_card_id"], r["platform"], bucket)
        buckets[key].append(r["price"])

    updated = 0
    for (player_card_id, platform, bucket), prices in buckets.items():
        if not prices:
            continue
        open_px = prices[0]
        close_px = prices[-1]
        high_px = max(prices)
        low_px = min(prices)
        vol = len(prices)
        open_time = datetime.fromtimestamp(bucket, tz=timezone.utc)

        await db.execute(
            """
            INSERT INTO public.fut_candles (player_card_id, platform, timeframe, open_time, open, high, low, close, volume)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            ON CONFLICT (player_card_id, platform, timeframe, open_time)
            DO UPDATE SET open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
                          close=EXCLUDED.close, volume=EXCLUDED.volume
            """,
            player_card_id, platform, timeframe, open_time, open_px, high_px, low_px, close_px, vol
        )
        updated += 1

    return {"updated": updated}

async def aggregate_all_timeframes(db, since_hours: int = 48) -> Dict[str, Dict[str, int]]:
    out = {}
    for tf in ("5m", "15m", "1h", "4h", "1d"):
        out[tf] = await aggregate_ticks_to_candles(db, tf, since_hours)
    return out