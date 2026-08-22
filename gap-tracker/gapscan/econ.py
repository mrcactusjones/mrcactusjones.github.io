"""The money math: does this card clear its costs at a PSA 9?

The whole point of the project lives in `evaluate`. Everything else is
plumbing to feed it prices.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .config import Economics, Thresholds

# Ranked worst -> best, so a caller can reason about downside.
VERDICTS = ("dead", "ten_or_bust", "floor_positive", "no_brainer")


@dataclass
class Quote:
    """Prices for one card. Any field may be None if there's no data.

    The confidence/last-sale fields matter as much as the prices: a graded
    "price" from one sale four months ago is not a price.
    """

    raw: Optional[float] = None
    psa9: Optional[float] = None
    psa10: Optional[float] = None
    psa8: Optional[float] = None
    sales_9: int = 0
    sales_10: int = 0
    as_of: Optional[str] = None
    source: Optional[str] = None
    psa9_confidence: Optional[str] = None
    psa10_confidence: Optional[str] = None
    psa9_last_sale: Optional[str] = None
    psa10_last_sale: Optional[str] = None
    psa9_outlier: bool = False
    psa10_outlier: bool = False
    cgc9: Optional[float] = None
    cgc10: Optional[float] = None
    cgc9_sales: int = 0
    cgc10_sales: int = 0


def days_since(stamp: Optional[str]) -> Optional[float]:
    """Age of an ISO timestamp in days, or None if unparseable."""
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds() / 86400.0


@dataclass
class Verdict:
    verdict: str
    all_in: float
    floor_profit: float          # profit if it comes back a 9 -- the headline
    floor_roi: float
    upside_profit: float         # profit if it comes back a 10
    upside_roi: float
    breakeven_p10: Optional[float]  # P(10) needed to break even when the 9 loses
    confident: bool
    reasons: list[str]


def breakeven_probability(all_in: float, net9: float, net10: float) -> Optional[float]:
    """Share of 10s needed for EV=0, assuming every other card grades 9.

    Deliberately optimistic (it ignores 8s and below), so treat it as a lower
    bound on the gem rate you need, not a forecast.
    """
    spread = net10 - net9
    if spread <= 0:
        return None
    p = (all_in - net9) / spread
    if p <= 0:
        return 0.0
    return p if p <= 1 else None  # >1 means even 100% tens lose money


def evaluate(quote: Quote, econ: Economics, thresholds: Thresholds) -> Optional[Verdict]:
    """Score one card. Returns None when there isn't enough data to judge."""
    if quote.raw is None or quote.psa9 is None:
        return None

    all_in = econ.all_in(quote.raw)
    if all_in <= 0:
        return None

    net9 = econ.net_proceeds(quote.psa9)
    net10 = econ.net_proceeds(quote.psa10) if quote.psa10 is not None else net9

    floor_profit = net9 - all_in
    upside_profit = net10 - all_in

    reasons: list[str] = []
    if quote.sales_9 < thresholds.min_sales_9:
        reasons.append(f"only {quote.sales_9} PSA 9 comps (want {thresholds.min_sales_9}+)")
    if quote.psa10 is not None and quote.sales_10 < thresholds.min_sales_10:
        reasons.append(f"only {quote.sales_10} PSA 10 comps (want {thresholds.min_sales_10}+)")
    stale = days_since(quote.psa9_last_sale)
    if stale is not None and stale > thresholds.max_sale_age_days:
        reasons.append(f"last PSA 9 sale was {stale:.0f} days ago")
    if quote.psa9_confidence and quote.psa9_confidence.lower() == "low":
        reasons.append("provider rates the PSA 9 price low-confidence")
    if quote.psa9_outlier:
        reasons.append("PSA 9 price flagged as an outlier")
    if quote.raw < thresholds.raw_price_min:
        reasons.append(f"raw ${quote.raw:.2f} below floor of ${thresholds.raw_price_min:.0f}")
    if quote.raw > thresholds.raw_price_max:
        reasons.append(f"raw ${quote.raw:.2f} above cap of ${thresholds.raw_price_max:.0f}")
    confident = not reasons

    floor_roi = floor_profit / all_in
    if floor_profit >= thresholds.min_floor_profit and floor_roi >= thresholds.min_floor_roi:
        verdict = "no_brainer" if confident else "floor_positive"
    elif floor_profit > 0:
        verdict = "floor_positive"
    elif upside_profit > 0:
        verdict = "ten_or_bust"
    else:
        verdict = "dead"

    return Verdict(
        verdict=verdict,
        all_in=round(all_in, 2),
        floor_profit=round(floor_profit, 2),
        floor_roi=round(floor_roi, 4),
        upside_profit=round(upside_profit, 2),
        upside_roi=round(upside_profit / all_in, 4),
        breakeven_p10=breakeven_probability(all_in, net9, net10),
        confident=confident,
        reasons=reasons,
    )
