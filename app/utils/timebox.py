
from __future__ import annotations
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")
UTC = timezone.utc

def now_utc() -> datetime:
    return datetime.now(UTC)

def london_now() -> datetime:
    return datetime.now(LONDON)

def next_daily_london_hour(minute_hour: int = 18) -> datetime:
    ln = london_now()
    target = ln.replace(hour=minute_hour, minute=0, second=0, microsecond=0)
    if target <= ln:
        target = target + timedelta(days=1)
    return target.astimezone(UTC)

def is_within_quiet_hours(dt_utc: datetime, quiet_start: time | None, quiet_end: time | None) -> bool:
    if not quiet_start or not quiet_end:
        return False
    dt_local = dt_utc.astimezone(LONDON)
    t = dt_local.time()
    if quiet_start <= quiet_end:
        return quiet_start <= t < quiet_end
    return t >= quiet_start or t < quiet_end
