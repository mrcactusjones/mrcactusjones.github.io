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
    tcgplayer_id: Optional[str] = None     # needed for a population lookup
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
    breakeven_p10: Optional[float]  # P(10) needed to break even when the 9 loses
    confident: bool
    reasons: list[str]
    ev_profit: Optional[float] = None    # probability-weighted, when a mix is known
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


def evaluate(quote: Quote, econ: Economics, thresholds: Thresholds,
             mix: GradeMix | None = None) -> Optional[Verdict]:
    """Score one card. Returns None when there isn't enough data to judge.

    `mix` is optional: the floor-at-9 ranking never depends on it. It only adds
    a probability-weighted EV alongside, for cards where the 9 doesn't pay.
    """
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

    ev_profit = gem_rate = mix_source = mix_sample = None
    if mix is not None:
        # What a lower grade recovers: the PSA 8 price if we have one, else a
        # haircut on raw, since a slabbed 7 still sells for something.
        net_low = econ.net_proceeds(quote.psa8) if quote.psa8 else \
            econ.net_proceeds(quote.raw * thresholds.low_grade_recovery)
        ev_profit = round(mix.p10 * net10 + mix.p9 * net9 + mix.p_low * net_low - all_in, 2)
        gem_rate = round(mix.p10, 4)
        mix_source, mix_sample = mix.source, mix.sample

    return Verdict(
        ev_profit=ev_profit, gem_rate=gem_rate,
        mix_source=mix_source, mix_sample=mix_sample,
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
