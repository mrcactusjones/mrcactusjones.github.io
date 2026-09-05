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
    psa_sales_mix: Optional[dict] = None   # {"8": 3, "9": 11, "10": 4}
    sales_window_start: Optional[str] = None   # span the sale counts cover
    sales_window_end: Optional[str] = None
    sales_velocity_month: Optional[float] = None  # provider's own figure
    tcgplayer_id: Optional[str] = None     # needed for a population lookup
    printings: Optional[list] = None       # e.g. ["Normal", "Reverse Holofoil"]
    variant_spread: Optional[float] = None # dearest printing / cheapest
    population: Optional[dict] = None      # {"grades": {...}, "gem_rate": ...}


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


DAYS_PER_MONTH = 30.44


def sales_per_month(quote: "Quote", min_window_days: float = 21.0) -> Optional[float]:
    """PSA 9 sales per month, from the counts and the window they cover.

    A count is meaningless without knowing how long it took to accumulate: 12
    sales over three months and 12 over three weeks are different markets.
    """
    if quote.sales_velocity_month:
        return float(quote.sales_velocity_month)
    if not quote.sales_9:
        return 0.0
    span = None
    start, end = days_since(quote.sales_window_start), days_since(quote.sales_window_end)
    if start is not None and end is not None:
        span = start - end
    if span is None or span < min_window_days:
        return None
    return quote.sales_9 / (span / DAYS_PER_MONTH)


def months_to_sell(rate_per_month: Optional[float]) -> Optional[float]:
    """Expected wait for one sale at that rate.

    Optimistic: it assumes yours is the next copy to sell, when in reality you
    queue behind other listings. Treat it as a lower bound on the wait.
    """
    if rate_per_month is None:
        return None
    if rate_per_month <= 0:
        return float("inf")
    return 1 / rate_per_month


@dataclass
class GradeMix:
    """How likely each outcome is when you submit this card.

    `source` matters as much as the numbers:
      population - PSA's own report. The real thing (Business plan only).
      sales      - inferred from the mix of graded sales. Free, and weaker: it
                   reflects what people *sell*, not what they *get*.
    Both overstate P(10) for a random raw copy, because submitters send their
    best copies. Treat either as a ceiling, not a forecast.
    """

    p10: float
    p9: float
    p_low: float
    source: str
    sample: int

    @property
    def gem_rate(self) -> float:
        return self.p10


def mix_from_population(pop: dict | None) -> GradeMix | None:
    """Grade distribution from a PSA population report."""
    if not pop:
        return None
    grades = pop.get("grades") or {}
    total = sum(v for v in grades.values() if isinstance(v, (int, float)))
    if total <= 0:
        return None
    tens = float(grades.get("10", 0) or 0)
    nines = float(grades.get("9", 0) or 0)
    return GradeMix(p10=tens / total, p9=nines / total,
                    p_low=max(0.0, 1 - (tens + nines) / total),
                    source="population", sample=int(total))


def mix_from_sales(counts: dict | None, min_sample: int = 5,
                   min_low: float = 0.15) -> GradeMix | None:
    """Grade distribution inferred from the mix of graded sales.

    Requires a minimum sample: with three sales the mix is noise, and a
    confident-looking 33% gem rate would be worse than no number at all.

    Sales are a biased view of outcomes. People list 9s and 10s and sit on the
    8s, so a card with no low-grade sales looks like it can never grade below a
    9 -- an EV with no downside at all. `min_low` holds back a floor of
    probability for that outcome, scaled out of the observed grades. It is an
    assumption, not a measurement, which is why population data supersedes it.
    """
    if not counts:
        return None
    total = sum(int(v or 0) for v in counts.values())
    if total < min_sample:
        return None
    tens = int(counts.get("10", 0) or 0)
    nines = int(counts.get("9", 0) or 0)
    p10, p9 = tens / total, nines / total
    p_low = max(0.0, 1 - p10 - p9)

    if p_low < min_low:
        # Reserve min_low for sub-9 outcomes, taking it proportionally from the
        # grades we did see rather than from one of them.
        keep = 1 - min_low
        observed = p10 + p9
        if observed > 0:
            p10, p9 = p10 / observed * keep, p9 / observed * keep
        p_low = min_low
    return GradeMix(p10=p10, p9=p9, p_low=p_low, source="sales", sample=total)


@dataclass
class Verdict:
    verdict: str
    all_in: float
    floor_profit: float          # profit if it comes back a 9 -- the headline
    floor_roi: float
    upside_profit: float         # profit if it comes back a 10
    upside_roi: float
    upside_known: bool           # False when there are no PSA 10 comps at all,
                                 # in which case upside_* repeat the floor
    breakeven_p10: Optional[float]  # P(10) needed to break even when the 9 loses
    confident: bool
    reasons: list[str]
    ev_profit: Optional[float] = None    # probability-weighted, when a mix is known
    sales_per_month: Optional[float] = None
    months_to_sell: Optional[float] = None
    capital_months: Optional[float] = None   # grading turnaround plus time to sell
    floor_per_month: Optional[float] = None  # the floor, per month of tied-up capital
    gem_rate: Optional[float] = None
    mix_source: Optional[str] = None
    mix_sample: Optional[int] = None


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


def graded_sales(quote: Quote) -> int:
    """Total graded sales behind this card, across every PSA grade."""
    if quote.psa_sales_mix:
        return sum(int(count or 0) for count in quote.psa_sales_mix.values())
    return int(quote.sales_9 or 0) + int(quote.sales_10 or 0)


def evaluate(quote: Quote, econ: Economics, thresholds: Thresholds,
             mix: GradeMix | None = None,
             extra_reasons: list[str] | None = None) -> Optional[Verdict]:
    """Score one card. Returns None when there isn't enough data to judge.

    `mix` is optional: the floor-at-9 ranking never depends on it. It only adds
    a probability-weighted EV alongside, for cards where the 9 doesn't pay.

    `extra_reasons` carries findings the caller can see and this function
    cannot -- how a card compares with the rest of its set, for instance.
    """
    if quote.raw is None or quote.psa9 is None:
        return None

    # A card nobody grades has no graded market, and a deeply negative floor
    # for it is not a finding -- it is the absence of one. Unjudgeable, the
    # same as a missing price, rather than ranked as a terrible trade.
    if graded_sales(quote) < thresholds.min_graded_sales:
        return None

    all_in = econ.all_in(quote.raw, declared_value=quote.psa9)
    if all_in <= 0:
        return None

    net9 = econ.net_proceeds(quote.psa9)
    net10 = econ.net_proceeds(quote.psa10) if quote.psa10 is not None else net9

    floor_profit = net9 - all_in
    upside_profit = net10 - all_in

    reasons: list[str] = list(extra_reasons or [])
    # A 9 worth more than a 10 cannot happen in a market that grades honestly;
    # it means sales from different cards landed in one bucket.
    if quote.psa10 is not None:
        if quote.psa9 > quote.psa10 * thresholds.grade_inversion_slack:
            reasons.append(
                f"PSA 9 (${quote.psa9:,.0f}) priced above PSA 10 (${quote.psa10:,.0f})")
        elif quote.psa9 == quote.psa10:
            # Not missing data -- one price wearing two grade labels, which is
            # what pooled comps look like when every sale lands in one bucket.
            reasons.append(f"PSA 9 and PSA 10 both ${quote.psa9:,.0f}; "
                           "the grades are not being told apart")
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
    # Graded sales are pooled across printings -- the eBay block carries no
    # printing at all -- so when a card's printings are worth very different
    # amounts, the graded price cannot be trusted against any one raw price.
    if (quote.variant_spread
            and quote.variant_spread > thresholds.variant_spread_factor):
        reasons.append(
            f"printings differ {quote.variant_spread:.1f}x "
            f"({', '.join(quote.printings or [])}); graded comps pool them")
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

    rate = sales_per_month(quote, thresholds.min_window_days)
    wait = months_to_sell(rate)
    capital = per_month = None
    if wait is not None and wait != float("inf"):
        capital = thresholds.grading_turnaround_days / DAYS_PER_MONTH + wait
        per_month = round(floor_profit / capital, 2) if capital > 0 else None

    ev_profit = gem_rate = mix_source = mix_sample = None
    if mix is not None:
        # What a lower grade recovers: the PSA 8 price if we have one, else a
        # haircut on raw, since a slabbed 7 still sells for something.
        net_low = econ.net_proceeds(quote.psa8) if quote.psa8 else \
            econ.net_proceeds(quote.raw * thresholds.low_grade_recovery)
        ev_profit = round(mix.p10 * net10 + mix.p9 * net9 + mix.p_low * net_low - all_in, 2)
        gem_rate = round(mix.p10, 4)
        mix_source, mix_sample = mix.source, mix.sample

    # Floor and all-in are left unrounded: the conviction score divides them,
    # and the page does the same at full precision. Rounding here made the two
    # disagree by a hair -- enough to fail the parity check. Display rounds.
    return Verdict(
        sales_per_month=round(rate, 2) if rate is not None else None,
        months_to_sell=(round(wait, 2) if wait is not None and wait != float("inf")
                        else None),
        # Full precision: the page divides by this to recompute the monthly
        # return, and a rounded divisor puts it a few cents out.
        capital_months=capital,
        floor_per_month=per_month,
        ev_profit=ev_profit, gem_rate=gem_rate,
        mix_source=mix_source, mix_sample=mix_sample,
        verdict=verdict,
        all_in=all_in,
        floor_profit=floor_profit,
        floor_roi=floor_roi,
        upside_profit=upside_profit,
        upside_roi=upside_profit / all_in,
        upside_known=quote.psa10 is not None,
        breakeven_p10=breakeven_probability(all_in, net9, net10),
        confident=confident,
        reasons=reasons,
    )
