from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Shared secret that TradingView must include in every alert payload.
    webhook_secret: str

    # IB Gateway connection (container name "ib-gateway" on the docker network).
    # The gnzsnz/ib-gateway image exposes its API via a socat proxy on
    # 4003 (live) / 4004 (paper) -- NOT the TWS-internal 4001/4002.
    ib_host: str = "ib-gateway"
    ib_port: int = 4004  # 4004 = paper trading, 4003 = live
    ib_client_id: int = 7

    # Order/contract defaults.
    default_exchange: str = "SMART"
    default_currency: str = "USD"
    default_sec_type: str = "STK"

    # Account base currency, used for cash checks and risk sizing.
    account_currency: str = "EUR"
    fx_pair: str = "EURUSD"

    # Sizing: start with a fixed share count while validating the wiring,
    # switch to fixed_risk once confidence is established.
    sizing_mode: Literal["fixed_shares", "fixed_risk"] = "fixed_risk"
    fixed_shares_qty: int = 1
    # Risk per trade in the traded instrument's currency: the loss if the stop
    # fills exactly at its trigger. Entry and stop arrive from TradingView in
    # USD, so the risk is USD too and needs no FX conversion.
    fixed_risk_usd: float = 100.0
    max_position_eur: float = 1000000.0

    # Entry guardrails.
    max_spread_pct: float = 0.05  # percent, e.g. 0.05 = 0.05%

    # An entry goes out the moment the quote passes every check. Only while it
    # doesn't, the bot keeps watching it for up to this many seconds to see
    # whether it settles (an odd first tick, a spread that closes again).
    # Waiting is only ever spent on entries that would otherwise be
    # rejected, so it can't delay one that is good. TradingView gives up on
    # a webhook after 3 seconds (the order is still processed, the alert
    # log just shows a failure).
    quote_max_wait_seconds: float = 2.0

    # The spread must also stay small next to what the trade risks per share
    # (entry - stop): a spread that is fine as a percentage of the price can
    # still eat most of a tight stop. 0 disables the check.
    max_spread_pct_of_risk: float = 25.0

    # Reject the entry if the ask has run above the TradingView entry price
    # by more than this share of the per-share risk -- the setup that fired
    # is no longer the one being bought. 0 disables the check.
    max_entry_slippage_pct_of_risk: float = 25.0

    # Paper-trading escape hatch. IBKR hands the shared real-time
    # entitlement to only one active session at a time, so the gateway is
    # left without live quotes whenever the live login is in use. With this
    # on, the bot asks for delayed quotes and trades on them -- prices are
    # then up to 15 minutes old, so keep it off for live trading.
    allow_delayed_market_data: bool = False

    # Trading window in US exchange time (DST handled by the timezone). The
    # regular session runs 09:30-16:00 ET; no new entries during the first
    # trading_skip_minutes_after_open minutes and none during the last
    # trading_stop_minutes_before_close minutes, i.e. 10:00-15:00 ET.
    trading_timezone: str = "America/New_York"
    trading_skip_minutes_after_open: int = 30
    trading_stop_minutes_before_close: int = 60

    # How long before the market close (16:00 ET) every bot-managed open
    # position gets force-closed at market, stop cancelled. Positions opened
    # late in the window keep being managed until then. Protects against a stop
    # that would otherwise sit through the close and reopen the position on
    # the next session's gap.
    flatten_before_close_minutes: int = 10

    # Doji trades from the "Fibonacci Longterm Achtung/HOLD" script (event
    # "doji"). Sized by the risk of their colour, not by sizing_mode: the
    # quarter exits need a real share count.
    doji_enabled: bool = True
    doji_risk_green_usd: float = 100.0
    doji_risk_yellow_usd: float = 25.0
    doji_risk_orange_usd: float = 50.0
    # Entry window for Doji trades. Many good orange Dojis come in the first
    # minutes after the open, so unlike the TF entries they are not skipped.
    # The alert comes a few seconds after the Doji candle closed, often into
    # a fast bounce, and Doji risk per share is small -- the TF limit of 25 %
    # of R would turn away many good ones. 0 disables the check.
    doji_max_entry_slippage_pct_of_risk: float = 50.0
    # Test phase: buy a Doji whatever the spread or how far the ask has run,
    # as long as the ask is still above the stop. The position is then sized
    # off the ask, so the dollar risk stays at the colour's budget. Turns the
    # spread %, spread-of-risk and slippage checks off for Dojis only.
    doji_skip_spread_checks: bool = True
    doji_skip_minutes_after_open: int = 0
    doji_stop_minutes_before_close: int = 15
    # Stop ladder: breakeven at this R, trailing distance (in R) once 10 R is
    # reached, per colour.
    doji_breakeven_r: float = 1.5
    doji_trail_r_green: float = 5.0
    doji_trail_r_yellow: float = 5.0
    doji_trail_r_orange: float = 3.0
    # Orange sells this share of the position at 2 R (instead of a quarter at
    # 2, 5 and 10 R); the rest trails. 0.25 = quarters like green and yellow.
    doji_orange_first_target_share: float = 0.25
    # Doji buy: a limit at most this share of the risk (entry - stop) above
    # the alert price. The size is computed from that limit, so the dollar
    # risk never exceeds the budget; is the ask already above it, there is no
    # trade. 13./17.11.: all 6 buys filled more than 50 % of R above the alert
    # lost (-8.6 R). 0 = market order like before.
    doji_entry_cap_pct_of_risk: float = 50.0
    doji_entry_fill_timeout_seconds: float = 10.0
    # US ETFs cannot be bought by EU retail accounts (no KID, IBKR error 201):
    # trade their CFD instead. CFD trades are always closed before the bell.
    doji_etf_as_cfd: bool = True
    # Positions already trailing (10 R reached) stay open through the close;
    # all other Doji positions are flattened with the rest. Outside regular
    # hours the bot watches the price itself and sells with a limit this far
    # below the bid once the stop is crossed (IBKR runs no stop orders there).
    doji_keep_trailing_overnight: bool = True
    doji_extended_exit_below_bid_pct: float = 0.5
    doji_monitor_interval_seconds: float = 2.0
    # true: check Doji entries against IBKR's AvailableFunds (buying power
    # incl. margin) instead of the cash balance, so several Dojis can be
    # bought at the same time. Off by default: cash only, like TF entries.
    doji_use_margin: bool = False
    # Trade journal: one row per Doji alert (signals) and one per closed Doji
    # trade (trades). JOURNAL_TOKEN unlocks /doji/stats and the CSV downloads;
    # empty keeps them switched off.
    doji_signals_file: str = "/data/doji_signals.csv"
    doji_trades_file: str = "/data/doji_trades.csv"
    journal_token: str = ""

    # Where open-position state (symbol -> trade_id/qty/stop order) is
    # persisted so it survives a webhook container restart.
    state_file: str = "/data/positions.json"

    # If true, alerts are validated/sized/checked but no order is sent to IBKR.
    dry_run: bool = False


settings = Settings()
