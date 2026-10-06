import statistics
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class QuoteSample:
    bid: float
    ask: float


@dataclass(frozen=True)
class QuoteAssessment:
    ok: bool
    reason: Optional[str]
    # The latest quote in the window: the one the order would trade into.
    bid: float
    ask: float
    # Spread the checks were run against: the worse of median and latest.
    decision_spread: float
    median_spread: float
    latest_spread: float
    max_spread: float
    spread_pct: float
    risk_per_share: float
    spread_pct_of_risk: float
    slippage_pct_of_risk: float
    samples: int


def assess_entry_quotes(
    samples: list[QuoteSample],
    entry: float,
    stop: float,
    max_spread_pct: float,
    max_spread_pct_of_risk: float,
    max_slippage_pct_of_risk: float,
) -> QuoteAssessment:
    """Decides whether quotes collected over a short window allow an entry.

    Collecting for a few seconds filters out a single odd tick, but must not
    make the bot enter on worse terms than a single look would have. So:

    - the spread tested is the WORSE of the window's median and the latest
      quote -- a wide spread right now rejects even if the median was
      fine, and an earlier spike that has since closed doesn't reject on
      its own, but a window that was mostly wide does;
    - the ask used for risk and drift is the latest one, since that is where
      a market buy would fill;
    - the risk basis is the smaller of the TradingView entry and the latest
      ask minus the stop, so a drop towards the stop makes the spread look
      bigger relative to the risk, never smaller;
    - if the ask has run above the TradingView entry by more than the
      allowed share of the risk, the setup is no longer the one that fired.

    A limit of 0 disables the matching percent-of-risk check.
    """
    valid = [s for s in samples if s.bid > 0 and s.ask >= s.bid]
    if not valid:
        return _rejected("no_valid_quotes", len(samples))

    latest = valid[-1]
    spreads = [s.ask - s.bid for s in valid]
    median_spread = statistics.median(spreads)
    latest_spread = latest.ask - latest.bid
    decision_spread = max(median_spread, latest_spread)
    mid = (latest.bid + latest.ask) / 2
    spread_pct = decision_spread / mid * 100

    def result(reason, risk_per_share=0.0, spread_pct_of_risk=0.0, slippage_pct_of_risk=0.0):
        return QuoteAssessment(
            ok=reason is None,
            reason=reason,
            bid=latest.bid,
            ask=latest.ask,
            decision_spread=decision_spread,
            median_spread=median_spread,
            latest_spread=latest_spread,
            max_spread=max(spreads),
            spread_pct=spread_pct,
            risk_per_share=risk_per_share,
            spread_pct_of_risk=spread_pct_of_risk,
            slippage_pct_of_risk=slippage_pct_of_risk,
            samples=len(valid),
        )

    if entry <= stop:
        return result("stop_not_below_entry")

    risk_per_share = min(entry, latest.ask) - stop
    if risk_per_share <= 0:
        return result("price_at_or_below_stop")

    spread_pct_of_risk = decision_spread / risk_per_share * 100
    slippage_pct_of_risk = max(0.0, latest.ask - entry) / (entry - stop) * 100

    def finish(reason):
        return result(reason, risk_per_share, spread_pct_of_risk, slippage_pct_of_risk)

    if spread_pct > max_spread_pct:
        return finish("spread_too_wide")
    if max_spread_pct_of_risk > 0 and spread_pct_of_risk > max_spread_pct_of_risk:
        return finish("spread_too_wide_for_risk")
    if max_slippage_pct_of_risk > 0 and slippage_pct_of_risk > max_slippage_pct_of_risk:
        return finish("price_ran_away")
    return finish(None)


def _rejected(reason: str, sample_count: int) -> QuoteAssessment:
    return QuoteAssessment(
        ok=False, reason=reason, bid=0.0, ask=0.0, decision_spread=0.0,
        median_spread=0.0, latest_spread=0.0, max_spread=0.0, spread_pct=0.0,
        risk_per_share=0.0, spread_pct_of_risk=0.0, slippage_pct_of_risk=0.0,
        samples=sample_count,
    )
