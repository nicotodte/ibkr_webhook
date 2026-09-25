from datetime import datetime, timedelta, timezone

from config import settings


def is_within_trading_hours(now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    hour = now.astimezone(timezone.utc).hour
    return settings.trading_start_utc_hour <= hour < settings.trading_end_utc_hour


def is_within_closing_flatten_window(now: datetime | None = None) -> bool:
    """True during the FLATTEN_BEFORE_CLOSE_MINUTES window right before close."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    close = now.replace(
        hour=settings.trading_end_utc_hour, minute=0, second=0, microsecond=0
    )
    flatten_start = close - timedelta(minutes=settings.flatten_before_close_minutes)
    return flatten_start <= now < close
