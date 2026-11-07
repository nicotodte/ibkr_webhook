from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from config import settings

MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)


def _window(now: datetime | None) -> tuple[datetime, datetime, datetime]:
    """Returns (now, window start, window end), all in exchange time."""
    tz = ZoneInfo(settings.trading_timezone)
    local = (now or datetime.now(timezone.utc)).astimezone(tz)
    open_ = datetime.combine(local.date(), MARKET_OPEN, tzinfo=tz)
    close = datetime.combine(local.date(), MARKET_CLOSE, tzinfo=tz)
    start = open_ + timedelta(minutes=settings.trading_skip_minutes_after_open)
    end = close - timedelta(minutes=settings.trading_stop_minutes_before_close)
    return local, start, end


def is_within_trading_hours(now: datetime | None = None) -> bool:
    local, start, end = _window(now)
    return start <= local < end


def is_within_closing_flatten_window(now: datetime | None = None) -> bool:
    """True during the FLATTEN_BEFORE_CLOSE_MINUTES window right before the
    end of the trading window."""
    local, _, end = _window(now)
    flatten_start = end - timedelta(minutes=settings.flatten_before_close_minutes)
    return flatten_start <= local < end
