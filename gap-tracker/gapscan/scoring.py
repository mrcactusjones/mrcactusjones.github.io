"""Conviction score: one number for "how much do I believe this trade".

The floor decides *whether* a card qualifies; this decides the order among
those that do. It is a weighted summary of judgements you would otherwise make
in your head, made explicit so they are consistent and arguable.

Two rules keep it honest:

* A card whose PSA 9 does not pay scores zero, whatever else is true of it. The
  score can never promote a card past the floor test.
* A component with no data is dropped and the remaining weights are
  renormalised, rather than scoring zero. Otherwise the score would rank cards
  by how much history we happen to hold, which is a different question.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


@dataclass
class Scoring:
    """Weights and the scales each component is measured against."""

    weights: dict = field(default_factory=lambda: {
        "size": 0.20,        # how much it pays
        "durability": 0.25,  # how much of that survived the worst day
        "depth": 0.15,       # how many comps stand behind the price
        "freshness": 0.15,   # how recently the market spoke
        "liquidity": 0.15,   # how fast capital comes back
        "direction": 0.10,   # whether the gap is opening or closing
    })
    # Deliberately capped: past these points, more does not make a card more of
    # a no-brainer, and letting size run would just rediscover "biggest gap".
    roi_full: float = 0.50
    depth_full: int = 20
    liquidity_full: float = 4.0
    direction_span: float = 0.30
    max_sale_age_days: float = 90.0
    unconfident_multiplier: float = 0.60


def components(row: dict, cfg: Scoring) -> dict[str, Optional[float]]:
    """Each component as 0-1, or None where the data does not support one."""
    floor = row.get("floor_profit")
    all_in = row.get("all_in") or 0

    size = None
    if floor is not None and all_in > 0:
        size = _clamp((floor / all_in) / cfg.roi_full)

    durability = row.get("floor_durability")

    sales_9 = row.get("sales_9")
    depth = None
    if sales_9 is not None:
        depth = _clamp(math.log1p(sales_9) / math.log1p(cfg.depth_full))

    age = row.get("psa9_sale_age_days")
    freshness = None if age is None else _clamp(1 - age / cfg.max_sale_age_days)

    rate = row.get("sales_per_month")
    liquidity = None if rate is None else _clamp(rate / cfg.liquidity_full)

    divergence = row.get("divergence_30d")
    direction = (None if divergence is None
                 else _clamp((divergence + cfg.direction_span) / (2 * cfg.direction_span)))

    return {"size": size, "durability": durability, "depth": depth,
            "freshness": freshness, "liquidity": liquidity, "direction": direction}


def score(row: dict, cfg: Scoring | None = None) -> dict:
    """Conviction 0-100, with the parts that produced it."""
    cfg = cfg or Scoring()
    floor = row.get("floor_profit")
    if floor is None or floor <= 0:
        # The floor test is a gate, not one input among several.
        return {"conviction": 0.0, "parts": {}, "coverage": 0.0,
                "reason": "the PSA 9 does not clear costs"}

    parts = components(row, cfg)
    available = {k: v for k, v in parts.items() if v is not None}
    total_weight = sum(cfg.weights.get(k, 0) for k in available)
    if total_weight <= 0:
        return {"conviction": 0.0, "parts": {}, "coverage": 0.0,
                "reason": "nothing measurable yet"}

    # Renormalise over what we can actually measure.
    value = sum(available[k] * cfg.weights.get(k, 0) for k in available) / total_weight
    if not row.get("confident", True):
        value *= cfg.unconfident_multiplier

    return {
        "conviction": round(value * 100, 1),
        "parts": {k: round(v, 3) for k, v in available.items()},
        "coverage": round(total_weight / sum(cfg.weights.values()), 3),
        "reason": None,
    }
