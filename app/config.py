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
    sizing_mode: Literal["fixed_shares", "fixed_risk"] = "fixed_shares"
    fixed_shares_qty: int = 1
    fixed_risk_eur: float = 100.0
    max_position_eur: float = 50000.0

    # Entry guardrails.
    max_spread_pct: float = 0.05  # percent, e.g. 0.05 = 0.05%

    # Trading window in UTC. 16-21 CEST and 15-20 CET are both 14:00-19:00
    # UTC, so this single fixed window covers both halves of the year with
    # no DST handling needed.
    trading_start_utc_hour: int = 14
    trading_end_utc_hour: int = 19

    # Where open-position state (symbol -> trade_id/qty/stop order) is
    # persisted so it survives a webhook container restart.
    state_file: str = "/data/positions.json"

    # If true, alerts are validated/sized/checked but no order is sent to IBKR.
    dry_run: bool = False


settings = Settings()
