"""What gets stored, and what must not overwrite it.

`price_points` is keyed (card_id, date, grade) and last write wins, so the
order rows are built in decides which measurement survives.
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gapscan import db, ingest


def _record(sale_date: str, sale_price: float, blended: float) -> dict:
    """One card with a real PSA 9 sale on a date, and a blended spot price."""
    return {
        "externalCatalogId": "base2-12", "name": "Vaporeon", "setName": "Jungle",
        "cardNumber": "12", "rarity": "Rare Holo",
        "prices": {"market": 60.0},
        "ebay": {
            "salesByGrade": {
                "psa9": {"count": 114,
                         "smartMarketPrice": {"price": blended, "confidence": "medium"}},
            },
            "priceHistory": {"psa9": {sale_date: {"average": sale_price, "count": 1}}},
        },
    }


class SnapshotCollisionTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)

    def _points(self):
        return [(r["price"], r["origin"]) for r in db.series_detail(
            self.conn, "base2-12", "psa9")]

    def test_a_snapshot_does_not_replace_a_sale_on_the_same_day(self):
        """A computed average must not stand in for a transaction."""
        ingest.ingest_record(self.conn, _record("2026-09-05", 347.50, 510.50),
                             today="2026-09-05")
        self.assertEqual(self._points(), [(347.50, "history")])

    def test_a_snapshot_is_still_stored_when_no_sale_covers_the_day(self):
        """Cards with no graded history rely on these entirely."""
        ingest.ingest_record(self.conn, _record("2026-09-01", 347.50, 510.50),
                             today="2026-09-05")
        self.assertEqual(self._points(),
                         [(347.50, "history"), (510.50, "snapshot")])

    def test_repeated_runs_do_not_bury_sales_under_snapshots(self):
        for day in ("2026-09-03", "2026-09-04", "2026-09-05"):
            ingest.ingest_record(self.conn, _record("2026-09-05", 347.50, 510.50),
                                 today=day)
        prices = self._points()
        self.assertIn((347.50, "history"), prices)
        # Three runs, three days, but the sale's day is not one of the snapshots.
        self.assertEqual(sum(1 for _, o in prices if o == "snapshot"), 2)

    def test_analysis_reads_the_sales_not_the_blend(self):
        ingest.ingest_record(self.conn, _record("2026-09-01", 347.50, 510.50),
                             today="2026-09-05")
        points, origin = db.sales_series(self.conn, "base2-12", "psa9")
        self.assertEqual(origin, "history")
        self.assertEqual([v for _, v in points], [347.50])

    def test_a_card_with_no_sale_history_falls_back_to_its_snapshots(self):
        """Otherwise these cards lose every trend they have."""
        bare = _record("2026-09-01", 347.50, 510.50)
        bare["ebay"]["priceHistory"] = {}
        ingest.ingest_record(self.conn, bare, today="2026-09-05")
        points, origin = db.sales_series(self.conn, "base2-12", "psa9")
        self.assertEqual(origin, "snapshot")
        self.assertEqual([v for _, v in points], [510.50])


if __name__ == "__main__":
    unittest.main()
