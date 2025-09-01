
from __future__ import annotations
from typing import Dict, Any
from datetime import datetime, timezone
from app.services.price_history import get_price_history
from app.services.prices import get_player_price

UTC = timezone.utc

def _slope(xs):
    n = len(xs)
    if n < 2: return 0.0
    xbar = (n - 1) / 2.0
    ybar = sum(xs) / n
    num = sum((i - xbar) * (y - ybar) for i, y in enumerate(xs))
    den = sum((i - xbar)**2 for i in range(n))
    return num / den if den else 0.0

def _norm01(x, lo, hi):
    if hi <= lo: return 0.0
    v = (x - lo) / (hi - lo)
    return max(0.0, min(1.0, v))

async def compute_deal_confidence(pool, player_id: int, platform: str) -> Dict[str, Any]:
    hist_24h = await get_price_history(player_id=player_id, platform=platform, tf="today")
    prices = [p.get("price") or p.get("v") or p.get("y") for p in hist_24h if (p.get("price") or p.get("v") or p.get("y"))]
    if len(prices) < 6:
        cur_price = await get_player_price(player_id, platform) or 0
        if not cur_price:
            return {"score": 0, "note": "no data", "components": {}}
        prices = [cur_price] * 6

    n = len(prices)
    last_q = prices[max(0, n - max(6, n//4)):]
    slope4h = _slope(last_q)
    momentum4h = _norm01(slope4h, 0, max(1e-9, abs(slope4h))) if slope4h > 0 else 0.0

    first_half = prices[:n//2] or prices
    second_half = prices[n//2:] or prices
    regimeAgreement = 1.0 if (sum(second_half)/len(second_half) >= sum(first_half)/len(first_half)) else 0.0

    diffs = [abs(prices[i]-prices[i-1]) for i in range(1, n)]
    vol_abs = sum(diffs)/len(diffs) if diffs else 0.0
    avg_price = sum(prices)/len(prices)
    volRisk = min(1.0, (vol_abs/avg_price) if avg_price else 1.0)

    liquidity = _norm01(n, 6, 96)

    window = prices[-min(12, n):]
    if window:
        lo, hi = min(window), max(window)
        spread_proxy = (hi-lo)/hi if hi else 0.1
    else:
        spread_proxy = 0.1

    recent_hi = max(window) if window else max(prices)
    cur = prices[-1]
    srRoom = (recent_hi - cur)/recent_hi if recent_hi else 0.0

    from app.utils.timebox import next_daily_london_hour, now_utc
    nxt = next_daily_london_hour(18)
    secs = (nxt - now_utc()).total_seconds()
    catalystBoost = _norm01(6*3600 - abs(secs - 6*3600), 0, 6*3600)

    score = 100 * (
        0.22 * momentum4h +
        0.14 * regimeAgreement +
        0.16 * (1 - volRisk) +
        0.18 * liquidity +
        0.12 * (1 - spread_proxy) +
        0.10 * srRoom +
        0.08 * catalystBoost
    )
    score = max(0.0, min(100.0, score))
    return {"score": round(score, 1),
            "components": {"momentum4h": round(momentum4h,3),
                           "regimeAgreement": regimeAgreement,
                           "volRisk": round(volRisk,3),
                           "liquidity": round(liquidity,3),
                           "spreadProxy": round(spread_proxy,3),
                           "srRoom": round(srRoom,3),
                           "catalystBoost": round(catalystBoost,3)}}
