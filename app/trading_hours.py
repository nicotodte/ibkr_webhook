from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from config import settings

MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)


def _window(now: datetime | None) -> tuple[datetime, datetime, datetime, datetime]:
    """Returns (now, entry window start, entry window end, market close), all
    in exchange time."""
    tz = ZoneInfo(settings.trading_timezone)
    local = (now or datetime.now(timezone.utc)).astimezone(tz)
    open_ = datetime.combine(local.date(), MARKET_OPEN, tzinfo=tz)
    close = datetime.combine(local.date(), MARKET_CLOSE, tzinfo=tz)
    start = open_ + timedelta(minutes=settings.trading_skip_minutes_after_open)
    end = close - timedelta(minutes=settings.trading_stop_minutes_before_close)
    return local, start, end, close


def is_within_trading_hours(now: datetime | None = None) -> bool:
    local, start, end, _ = _window(now)
    return start <= local < end


EXTENDED_OPEN = time(4, 0)
EXTENDED_CLOSE = time(20, 0)


def _local(now: datetime | None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(settings.trading_timezone))


def is_within_doji_entry_hours(now: datetime | None = None) -> bool:
    local = _local(now)
    if local.weekday() >= 5:
        return False
    tz = local.tzinfo
    start = datetime.combine(local.date(), MARKET_OPEN, tzinfo=tz) + timedelta(
        minutes=settings.doji_skip_minutes_after_open
    )
    end = datetime.combine(local.date(), MARKET_CLOSE, tzinfo=tz) - timedelta(
        minutes=settings.doji_stop_minutes_before_close
    )
    return start <= local < end


def is_regular_session(now: datetime | None = None) -> bool:
    """Mon-Fri 09:30-16:00 ET, where IBKR's own stop orders run."""
    local = _local(now)
    return local.weekday() < 5 and MARKET_OPEN <= local.time() < MARKET_CLOSE


def is_extended_session(now: datetime | None = None) -> bool:
    """Pre-market 04:00-09:30 and after-hours 16:00-20:00 ET, Mon-Fri: US
    stocks trade, but only limit orders execute."""
    local = _local(now)
    if local.weekday() >= 5:
        return False
    t = local.time()
    return EXTENDED_OPEN <= t < MARKET_OPEN or MARKET_CLOSE <= t < EXTENDED_CLOSE


def is_within_closing_flatten_window(now: datetime | None = None) -> bool:
    """True during the FLATTEN_BEFORE_CLOSE_MINUTES window right before the
    market close, so open positions still run after the entry window ended."""
    local, _, _, close = _window(now)
    flatten_start = close - timedelta(minutes=settings.flatten_before_close_minutes)
    return flatten_start <= local < close
