from datetime import datetime, timezone

from .config import settings


def is_within_trading_hours(now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    hour = now.astimezone(timezone.utc).hour
    return settings.trading_start_utc_hour <= hour < settings.trading_end_utc_hour
