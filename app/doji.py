"""Exit plan for Doji trades, kept free of IBKR and config imports so it can
be tested on its own.

A Doji trade is sold in quarters: one quarter at 2 R, 5 R and 10 R each, the
last quarter rides on a trailing stop. R is the per-share risk of the actual
fill (fill price minus the stop). The stop follows a ladder that only ever
moves up:

    reached 1.5 R -> stop to the entry (breakeven)
    reached 5 R   -> stop to entry + 2 R
    reached 10 R  -> stop to entry + 5 R, then trails trail_r R below the high
"""
import math
from typing import Optional

TARGETS_R = (2.0, 5.0, 10.0)
LADDER = ((5.0, 2.0), (10.0, 5.0))  # (reached R, stop at R)
TRAIL_FROM_R = 10.0


def split_quarters(quantity: int) -> list[int]:
    """Returns the share counts for [2 R, 5 R, 10 R, trailing rest].

    The remainder goes to the trailing rest (13 -> 3, 3, 3, 4). Below four
    shares the targets are filled from the front, the trail always keeps at
    least one share: 1 -> [0, 0, 0, 1], 2 -> [1, 0, 0, 1], 3 -> [1, 1, 0, 1].
    """
    if quantity < 1:
        return [0, 0, 0, 0]
    if quantity >= 4:
        base = quantity // 4
        return [base, base, base, quantity - 3 * base]
    return {1: [0, 0, 0, 1], 2: [1, 0, 0, 1], 3: [1, 1, 0, 1]}[quantity]


def split_for_tier(quantity: int, tier: str, orange_first_share: float = 0.75) -> list[int]:
    """Share counts for [2 R, 5 R, 10 R, trailing rest] by colour.

    Green and yellow: quarters (split_quarters), they need their runners.
    Orange: orange_first_share of the position at 2 R and the rest trails --
    orange Dojis mostly reach 2-3 R and then turn (test 17.11.: 47 signals,
    +28.2 R instead of +21.8 R). The trail keeps at least one share.
    """
    if tier != "orange" or orange_first_share <= 0.25 or quantity < 2:
        return split_quarters(quantity)
    first = min(int(quantity * orange_first_share), quantity - 1)
    return [first, 0, 0, quantity - first]


def stop_quantities(quantity: int) -> list[int]:
    """Sizes of the stop orders: one per quarter that has a target (its OCA
    partner) plus the trailing rest. They always add up to the position."""
    sizes = split_quarters(quantity)
    return [q for q in sizes[:3] if q > 0] + [sizes[3]]


def floor_cent(price: float) -> float:
    # Sell stops and exit limits round down: never above the intended price.
    return math.floor(price * 100 + 1e-9) / 100


def ceil_cent(price: float) -> float:
    # Take-profit limits round up: never sell below the target.
    return math.ceil(price * 100 - 1e-9) / 100


def target_prices(entry: float, risk: float) -> list[float]:
    return [ceil_cent(entry + r * risk) for r in TARGETS_R]


def ladder_stop(
    entry: float,
    risk: float,
    high: float,
    current_stop: float,
    trail_r: float,
    breakeven_r: float = 1.5,
) -> tuple[float, bool]:
    """Returns (stop price, trailing active) for the highest price seen so far.

    Never returns a stop below current_stop: the ladder only moves up.
    """
    if risk <= 0:
        return current_stop, False
    reached = (high - entry) / risk
    stop = current_stop
    if reached >= breakeven_r:
        stop = max(stop, entry)
    for at_r, stop_r in LADDER:
        if reached >= at_r:
            stop = max(stop, entry + stop_r * risk)
    trailing = reached >= TRAIL_FROM_R
    if trailing:
        stop = max(stop, high - trail_r * risk)
    return floor_cent(stop), trailing


def extended_hours_exit_limit(bid: float, below_pct: float) -> Optional[float]:
    """Limit price for a sell outside regular hours, where IBKR takes no
    market orders: a little below the bid so it fills like a market order."""
    if not bid or bid <= 0:
        return None
    return floor_cent(bid * (1 - below_pct / 100))
