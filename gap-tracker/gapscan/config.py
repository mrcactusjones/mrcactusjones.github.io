"""Tunables for the scanner and the money math.

Everything here is deliberately overridable from the CLI or config.json so the
economics can be re-run without touching code.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field, fields
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _scoring_default():
    # Imported lazily: scoring imports nothing from config, and this keeps it
    # that way rather than creating a cycle.
    from .scoring import Scoring
    return Scoring()
DATA = ROOT / "data"
SEEDS = ROOT / "seeds"
FIXTURES = ROOT / "fixtures"


@dataclass
class Economics:
    """Cost model for buy raw -> grade -> sell slabbed."""

    # PSA paused every Value tier on 2026-06-02 (backlog), so the cheapest
    # service is Regular at $79.99. Tiers are (declared-value cap, fee); the
    # first tier whose cap covers the card is used.
    #
    # The top two are PSA's premium service levels and are the least certain
    # figures in this file -- check them against PSA's current price list
    # before acting on a card that reaches them. Below $5,000 the first three
    # are the ones that matter, and `raw_price_max` at $400 means a card needs
    # a 12x multiple to get past them at all. A declared value above the last
    # cap is flagged rather than costed, because there the fee is a guess.
    fee_tiers: tuple = ((1499.0, 79.99), (2499.0, 149.0), (4999.0, 299.0),
                        (9999.0, 499.0), (24999.0, 999.0))
    insurance_threshold: float = 499.0   # above this PSA adds a surcharge
    insurance_pct: float = 0.02
    grading_fee: float = 79.99       # flat fallback when tiers are disabled
    use_fee_tiers: bool = True
    sub_ship_per_card: float = 4.0   # round-trip shipping + insurance, amortised
    sale_fee_pct: float = 0.1325     # marketplace take incl. promoted listings
    ship_out: float = 5.00           # shipping the slab to the buyer
    raw_premium_pct: float = 0.15    # you rarely buy at guide; pad the raw price

    def fee_for(self, declared_value: float | None = None) -> float:
        """Grading fee for a card, including PSA's insurance surcharge.

        Declared value drives both the service tier and the surcharge, so a
        card that grades into four figures costs materially more to submit than
        the headline bulk rate suggests.
        """
        if not self.use_fee_tiers:
            return self.grading_fee
        value = float(declared_value or 0)
        fee = self.fee_tiers[-1][1]
        for cap, tier_fee in self.fee_tiers:
            if value <= cap:
                fee = tier_fee
                break
        if value > self.insurance_threshold:
            fee += (value - self.insurance_threshold) * self.insurance_pct
        return fee

    def above_modelled_range(self, declared_value: float | None = None) -> bool:
        """True when the card is worth more than the top tier covers.

        `fee_for` falls back to the last tier's fee there, which understates a
        real submission by hundreds of dollars. Better to say the cost is
        unknown than to quietly under-cost the trade.
        """
        if not self.use_fee_tiers or not self.fee_tiers:
            return False
        return float(declared_value or 0) > self.fee_tiers[-1][0]

    def tier_headroom(self, declared_value: float | None = None) -> float | None:
        """How close the card sits to the top of its fee tier, as a fraction.

        0.05 means a 5% rise in the slabbed price moves it into the next tier
        -- $69 more on the $1,499 boundary, on an otherwise identical trade.
        None when tiers are off, or the value is above the modelled range.
        """
        if not self.use_fee_tiers or not self.fee_tiers:
            return None
        value = float(declared_value or 0)
        for cap, _ in self.fee_tiers:
            if value <= cap:
                return (cap - value) / cap if cap > 0 else None
        return None

    def all_in(self, raw_price: float, declared_value: float | None = None) -> float:
        """Total sunk cost per card before it sells.

        `declared_value` is what the card is worth once slabbed -- the PSA 9
        price where we know it -- because that is what PSA prices the service
        and the insurance on.
        """
        return (raw_price * (1 + self.raw_premium_pct)
                + self.fee_for(declared_value if declared_value is not None else raw_price)
                + self.sub_ship_per_card)

    def net_proceeds(self, sale_price: float) -> float:
        """What actually lands in your pocket on a sale."""
        return sale_price * (1 - self.sale_fee_pct) - self.ship_out


@dataclass
class Thresholds:
    """What counts as a real signal rather than a data artefact."""

    min_floor_profit: float = 25.0   # $ cleared at PSA 9 to call it a no-brainer
    min_floor_roi: float = 0.25      # and it must beat this return on capital
    min_sales_9: int = 5             # PSA 9 comps in the window, or it's noise
    min_sales_10: int = 3
    raw_price_min: float = 8.0       # below this the 9 can't clear grading cost
    raw_price_max: float = 400.0     # above this you can't buy volume anyway
    max_sale_age_days: float = 90.0  # a comp older than this isn't a live market
    low_grade_recovery: float = 0.80  # fraction of raw recovered on a sub-9 grade
    grading_turnaround_days: float = 35.0  # PSA Regular: ~25 business days
    min_window_days: float = 21.0    # shorter sale windows can't imply a rate
    min_graded_sales: int = 3        # below this there is no graded market to price
    grade_inversion_slack: float = 1.0   # PSA 9 above PSA 10 x this is contamination
    variant_spread_factor: float = 2.5   # printings differing by more than this
                                     # make a pooled graded price untrustworthy
    min_set_sample: int = 8          # cards needed before a set median means anything
    set_multiple_factor: float = 4.0 # graded/raw multiple this far past the set's
                                     # median marks comps from another card
    # PPT reads grades off eBay titles, which carry no printing, so one card's
    # graded "price" can be two printings averaged. p75/p25 this wide means two
    # populations; below ~2x a real card's noise is indistinguishable.
    comps_split_spread: float = 2.0
    # p90/p10. The middle-half test only sees a split near 50/50; a dear
    # printing that is a quarter of the sales sits entirely above p75.
    comps_split_tail_spread: float = 4.0
    comps_split_min_share: float = 0.25  # each side must hold this much of the sales
    comps_split_min_sample: int = 8      # fewer sales than this cannot show a split
    min_mix_sample: int = 5          # graded sales needed before inferring a grade mix
    sales_mix_min_low: float = 0.15  # assumed floor on grading below a 9, when
                                     # the mix comes from sales rather than a
                                     # population report


@dataclass
class ScanBudget:
    """Free-tier credit accounting.

    PokemonPriceTracker charges per card: 1 credit base + 1 for the PSA/eBay
    block. The free tier is 100 credits/day, so ~50 cards/day is the hard
    ceiling. Discovery and watchlist refresh compete for the same pool.
    """

    daily_credits: int = 100
    # The API bills per card RETURNED, not per request: a call costs
    # search_limit x (1 base + 1 if the graded block is included). A limit of 25
    # therefore cost 50 credits a call, not 2.
    search_limit: int = 1
    include_graded: bool = True
    credits_per_card: int = 2  # derived; kept for config compatibility
    watchlist_size: int = 50
    watchlist_ttl_days: int = 7      # graded comps move on weeks, not days
    candidate_ttl_days: int = 30
    rejected_ttl_days: int = 90      # cards that failed badly; check back rarely
    watchlist_share: float = 0.5     # cap on budget spent refreshing knowns


    @property
    def credits_per_call(self) -> int:
        return max(1, self.search_limit) * (2 if self.include_graded else 1)


@dataclass
class Config:
    econ: Economics = field(default_factory=Economics)
    thresholds: Thresholds = field(default_factory=Thresholds)
    budget: ScanBudget = field(default_factory=ScanBudget)
    scoring: "Scoring" = field(default_factory=lambda: _scoring_default())

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or ROOT / "config.json"
        cfg = cls()
        if not path.exists():
            return cfg
        blob = json.loads(path.read_text())
        for section in ("econ", "thresholds", "budget", "scoring"):
            overrides = blob.get(section) or {}
            target = getattr(cfg, section)
            known = {f.name for f in fields(target)}
            for key, value in overrides.items():
                if key in known:
                    setattr(target, key, value)
                else:
                    raise SystemExit(f"config.json: unknown {section} key {key!r}")
        return cfg

    def to_dict(self) -> dict:
        return {"econ": asdict(self.econ), "thresholds": asdict(self.thresholds),
                "budget": asdict(self.budget), "scoring": asdict(self.scoring)}
