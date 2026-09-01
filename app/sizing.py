import math

from .config import settings


class SizingError(ValueError):
    pass


def calculate_quantity(entry_price_usd: float, stop_price_usd: float, fx_rate_eur_usd: float) -> int:
    """Returns the share quantity for a new entry.

    fixed_shares mode: always settings.fixed_shares_qty (phase 1: just
    validate the wiring end-to-end with a trivial position size).

    fixed_risk mode: risk a fixed EUR amount per trade, converted to USD
    at the live EUR/USD rate, capped by max_position_eur notional.
    """
    if settings.sizing_mode == "fixed_shares":
        if settings.fixed_shares_qty < 1:
            raise SizingError("fixed_shares_qty must be >= 1")
        return settings.fixed_shares_qty

    per_share_risk = abs(entry_price_usd - stop_price_usd)
    if per_share_risk <= 0:
        raise SizingError("entry and stop price must differ")

    risk_usd = settings.fixed_risk_eur * fx_rate_eur_usd
    raw_qty = risk_usd / per_share_risk

    max_qty_by_notional = (settings.max_position_eur * fx_rate_eur_usd) / entry_price_usd
    qty = math.floor(min(raw_qty, max_qty_by_notional))

    if qty < 1:
        raise SizingError(
            f"calculated quantity < 1 (risk_usd={risk_usd:.2f}, "
            f"per_share_risk={per_share_risk:.4f})"
        )
    return qty
