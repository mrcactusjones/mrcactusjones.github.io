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
        self.credits_used = 0

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
        from datetime import timedelta  # noqa: F401  (used below too)
        n9, n10 = rng.randint(0, 40), rng.randint(0, 25)
        age9 = rng.choice([2, 9, 20, 45, 95, 140])
        confidence = "low" if n9 <= 2 else rng.choice(["medium", "high", "high"])
        last9 = iso(utcnow() - timedelta(days=age9))
        last10 = iso(utcnow() - timedelta(days=age9 + rng.randint(0, 30)))

        return Quote(
            raw=round(raw, 2),
            psa9=round(raw * mult9, 2),
            # No PSA 10 sales means no PSA 10 price -- real for scarce cards,
            # and the case where the model has to refuse to invent upside.
            psa10=round(raw * mult10, 2) if n10 else None,
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
            sales_window_start=iso(utcnow() - timedelta(days=rng.choice([30, 90, 180]))),
            sales_window_end=iso(utcnow()),
            tcgplayer_id=str(rng.randint(10000, 99999)),
            as_of=iso(utcnow()),
            source="mock",
        )

    # The live API's set ids are opaque numbers. Ours encodes the name so a
    # sweep by id and a sweep by name produce the same cards -- otherwise the
    # demo's output would change the moment set ids got pinned.
    SET_ID_PREFIX = "mockset-"

    def fetch_batch(self, set_name: str | None = None, days: int = 180, limit: int = 100,
                    offset: int = 0, min_price: float | None = None,
                    max_price: float | None = None,
                    set_id: str | None = None) -> tuple[list[dict], int]:
        """Fake set page, shaped like the real response including history."""
        from datetime import timedelta

        from ..store import utcnow
        if set_id:
            set_name = set_id[len(self.SET_ID_PREFIX):] if \
                set_id.startswith(self.SET_ID_PREFIX) else set_id
        if not set_name:
            raise ValueError("fetch_batch needs a set_id or a set_name")
        if offset:
            return [], 0
        rng = self._rng(f"{set_name}|batch")
        records = []
        for index in range(1, min(limit, 12) + 1):
            base = rng.uniform(10, 300)
            mult9, mult10 = rng.uniform(1.3, 3.5), rng.uniform(3, 9)
            def walk(start, points=days // 3):
                series, value = {}, start
                for step in range(points):
                    value *= 1 + rng.uniform(-0.03, 0.035)
                    stamp = (utcnow() - timedelta(days=(points - step) * 3)).date()
                    series[stamp.isoformat()] = round(value, 2)
                return series
            def pooled(start, points=days // 3):
                """Two printings' sales interleaved under one grade."""
                cheap, dear = walk(start), walk(start * 2.8)
                return {stamp: (dear[stamp] if i % 2 else price)
                        for i, (stamp, price) in enumerate(sorted(cheap.items()))}

            records.append({
                "id": f"mock{index}", "externalCatalogId": f"{set_name}-{index}",
                "setName": set_name, "cardNumber": str(index),
                "setId": f"{self.SET_ID_PREFIX}{set_name}",
                "name": f"Mock {index}", "rarity": "Rare Holo",
                "tcgPlayerId": str(10000 + index),
                "prices": {"market": round(base, 2)},
                "priceHistory": walk(base),
                "ebay": {
                    "salesByGrade": {
                        "psa9": {"count": rng.randint(3, 30),
                                 "smartMarketPrice": {"price": round(base * mult9, 2),
                                                      "confidence": "medium"},
                                 "lastSaleDate": self._recent(rng)},
                        "psa10": {"count": rng.randint(1, 20),
                                  "smartMarketPrice": {"price": round(base * mult10, 2),
                                                       "confidence": "medium"},
                                  "lastSaleDate": self._recent(rng)},
                        "psa8": {"count": rng.randint(0, 6),
                                 "medianPrice": round(base * mult9 * 0.5, 2)},
                    },
                    "priceHistory": {
                        # Every third card's PSA 9 sales are two printings
                        # pooled, the way a WOTC holo's 1st Edition and
                        # Unlimited sales arrive under one title-parsed grade.
                        # Without this the demo never exercises the split
                        # detector and the page could drift from the model
                        # unnoticed.
                        "psa9": (pooled(base * mult9) if index % 3 == 0
                                 else walk(base * mult9)),
                        "psa10": walk(base * mult10),
                    },
                    "dateRangeStart": (utcnow() - timedelta(
                        days=rng.choice([45, 90, 180]))).isoformat(),
                    "dateRangeEnd": utcnow().isoformat(),
                },
            })
        cost = limit * 3
        self.credits_used += cost
        return records, cost

    @staticmethod
    def _recent(rng) -> str:
        from datetime import timedelta

        from ..store import iso, utcnow
        return iso(utcnow() - timedelta(days=rng.randint(1, 60)))
