"""Deterministic fake provider.

Lets the whole pipeline -- scanner, ranking, history, dashboard -- be exercised
end to end before an API key exists. Seeded off the card id so runs are stable
and day-over-day history shows believable drift rather than noise.
"""
from __future__ import annotations

import hashlib
import random

from ..econ import Quote
from ..store import iso, utcnow


class MockProvider:
    name = "mock"
    credits_per_card = 0

    def __init__(self, drift_seed: str = ""):
        self.drift_seed = drift_seed

    def _rng(self, card_id: str) -> random.Random:
        digest = hashlib.sha256(f"{card_id}|{self.drift_seed}".encode()).hexdigest()
        return random.Random(int(digest[:16], 16))

    def fetch(self, card: dict) -> Quote | None:
        rng = self._rng(card["id"])
        raw = float(card.get("raw_hint") or rng.uniform(10, 150))

        # Vintage sets get fatter graded multiples than modern, matching the
        # pattern the seed file is built around.
        vintage = card.get("set_id", "") in {
            "base1", "base2", "base3", "base4", "base5", "gym1", "gym2",
            "neo1", "neo2", "neo3", "neo4", "ecard1", "ecard2", "ecard3",
            "ex7", "ex10", "ex11", "ex13", "ex15",
        }
        mult9 = rng.uniform(1.4, 4.2) if vintage else rng.uniform(0.9, 1.8)
        mult10 = mult9 * rng.uniform(1.8, 5.0)

        # Mirror the real feed's thin-and-sometimes-stale character, so the
        # demo exercises the confidence and staleness paths rather than
        # showing a rosier picture than production.
        from datetime import timedelta
        n9, n10 = rng.randint(0, 40), rng.randint(0, 25)
        age9 = rng.choice([2, 9, 20, 45, 95, 140])
        confidence = "low" if n9 <= 2 else rng.choice(["medium", "high", "high"])
        last9 = iso(utcnow() - timedelta(days=age9))
        last10 = iso(utcnow() - timedelta(days=age9 + rng.randint(0, 30)))

        return Quote(
            raw=round(raw, 2),
            psa9=round(raw * mult9, 2),
            psa10=round(raw * mult10, 2),
            psa8=round(raw * mult9 * 0.55, 2) if rng.random() < 0.7 else None,
            sales_9=n9,
            sales_10=n10,
            psa9_confidence=confidence,
            psa10_confidence=confidence,
            psa9_last_sale=last9,
            psa10_last_sale=last10,
            cgc9=round(raw * mult9 * 0.8, 2) if rng.random() < 0.4 else None,
            cgc9_sales=rng.randint(0, 5),
            psa_sales_mix={"8": rng.randint(0, 8), "9": n9, "10": n10} if n9 + n10 else None,
            tcgplayer_id=str(rng.randint(10000, 99999)),
            as_of=iso(utcnow()),
            source="mock",
        )
