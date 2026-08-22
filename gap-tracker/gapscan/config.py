"""Tunables for the scanner and the money math.

Everything here is deliberately overridable from the CLI or config.json so the
economics can be re-run without touching code.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field, fields
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SEEDS = ROOT / "seeds"
FIXTURES = ROOT / "fixtures"


@dataclass
class Economics:
    """Cost model for buy raw -> grade -> sell slabbed."""

    grading_fee: float = 19.0        # PSA bulk/value tier, per card
    sub_ship_per_card: float = 4.0   # round-trip shipping + insurance, amortised
    sale_fee_pct: float = 0.1325     # marketplace take incl. promoted listings
    ship_out: float = 5.00           # shipping the slab to the buyer
    raw_premium_pct: float = 0.15    # you rarely buy at guide; pad the raw price

    def all_in(self, raw_price: float) -> float:
        """Total sunk cost per card before it sells."""
        return raw_price * (1 + self.raw_premium_pct) + self.grading_fee + self.sub_ship_per_card

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


@dataclass
class ScanBudget:
    """Free-tier credit accounting.

    PokemonPriceTracker charges per card: 1 credit base + 1 for the PSA/eBay
    block. The free tier is 100 credits/day, so ~50 cards/day is the hard
    ceiling. Discovery and watchlist refresh compete for the same pool.
    """

    daily_credits: int = 100
    credits_per_card: int = 2
    watchlist_size: int = 50
    watchlist_ttl_days: int = 7      # graded comps move on weeks, not days
    candidate_ttl_days: int = 30
    rejected_ttl_days: int = 90      # cards that failed badly; check back rarely
    watchlist_share: float = 0.5     # cap on budget spent refreshing knowns


@dataclass
class Config:
    econ: Economics = field(default_factory=Economics)
    thresholds: Thresholds = field(default_factory=Thresholds)
    budget: ScanBudget = field(default_factory=ScanBudget)

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or ROOT / "config.json"
        cfg = cls()
        if not path.exists():
            return cfg
        blob = json.loads(path.read_text())
        for section in ("econ", "thresholds", "budget"):
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
                "budget": asdict(self.budget)}
