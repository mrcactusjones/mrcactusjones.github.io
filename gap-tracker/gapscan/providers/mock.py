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

        return Quote(
            raw=round(raw, 2),
            psa9=round(raw * mult9, 2),
            psa10=round(raw * mult10, 2),
            psa8=round(raw * mult9 * 0.55, 2),
            sales_9=rng.randint(0, 40),
            sales_10=rng.randint(0, 25),
            as_of=iso(utcnow()),
            source="mock",
        )
