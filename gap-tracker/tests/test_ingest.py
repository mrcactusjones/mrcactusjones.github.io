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


class SnapshotReaderTest(unittest.TestCase):
    """Day-over-day comparison reads the dated ranking files.

    `daily_metrics` existed for this and was never written to, so it always
    read back empty; the dated snapshots `rank` already saves hold the same
    fields and years of history.
    """

    def setUp(self):
        import tempfile
        from gapscan.store import Store
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name))
        self.store.history.mkdir(parents=True, exist_ok=True)

    def _write(self, day, rows):
        import json
        (self.store.history / f"{day}.json").write_text(
            json.dumps({"date": day, "rows": rows}))

    def test_dates_come_back_oldest_first(self):
        for day in ("2026-09-05", "2026-09-01", "2026-09-03"):
            self._write(day, [])
        self.assertEqual(self.store.snapshot_dates(),
                         ["2026-09-01", "2026-09-03", "2026-09-05"])

    def test_a_snapshot_keeps_the_verdict(self):
        """load_history drops it; a diff needs it."""
        self._write("2026-09-05", [{"id": "base1-4", "verdict": "no_brainer",
                                    "floor_profit": 120.0}])
        snap = self.store.load_snapshot("2026-09-05")
        self.assertEqual(snap["base1-4"]["verdict"], "no_brainer")

    def test_a_missing_or_corrupt_day_is_empty_not_an_error(self):
        self.assertEqual(self.store.load_snapshot("2026-01-01"), {})
        (self.store.history / "2026-09-06.json").write_text("{not json")
        self.assertEqual(self.store.load_snapshot("2026-09-06"), {})

    def test_rows_without_an_id_are_skipped(self):
        self._write("2026-09-05", [{"verdict": "dead"}, {"id": "ok", "verdict": "dead"}])
        self.assertEqual(list(self.store.load_snapshot("2026-09-05")), ["ok"])


class CreditLedgerTest(unittest.TestCase):
    """The allowance is account-wide; each run only knew its own spend.

    Three sweeps in one day each started believing the whole 20,000 was free,
    and the third ran the account dry mid-task with the watchlist still
    unresolved.
    """

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)

    def test_the_day_s_runs_add_up(self):
        db.record_run(self.conn, "2026-09-05T09:00:00+00:00", 6825, 1767, "backfill")
        db.record_run(self.conn, "2026-09-05T13:00:00+00:00", 7200, 1869, "backfill")
        self.assertEqual(
            db.credits_spent_since(self.conn, "2026-09-05T00:00:00+00:00"), 14025)

    def test_yesterday_does_not_count_against_today(self):
        db.record_run(self.conn, "2026-09-04T22:00:00+00:00", 8400, 604, "backfill")
        db.record_run(self.conn, "2026-09-05T09:00:00+00:00", 6825, 1767, "backfill")
        self.assertEqual(
            db.credits_spent_since(self.conn, "2026-09-05T00:00:00+00:00"), 6825)

    def test_an_empty_ledger_is_zero_not_an_error(self):
        self.assertEqual(
            db.credits_spent_since(self.conn, "2026-09-05T00:00:00+00:00"), 0)

    def test_re_recording_a_run_replaces_it(self):
        """A run logs once; a retry of the same start must not double-count."""
        db.record_run(self.conn, "2026-09-05T09:00:00+00:00", 100, 1, "backfill")
        db.record_run(self.conn, "2026-09-05T09:00:00+00:00", 250, 3, "backfill")
        self.assertEqual(
            db.credits_spent_since(self.conn, "2026-09-05T00:00:00+00:00"), 250)

    def test_the_allowance_day_is_utc_not_local(self):
        """The provider resets at 00:00 UTC; snapshots use the local date.

        West of Greenwich an evening run is tomorrow's allowance while still
        being today locally, so these two boundaries must not be confused.
        """
        from datetime import datetime, timezone
        evening = datetime(2026, 9, 5, 23, 30, tzinfo=timezone.utc)
        self.assertEqual(db.allowance_day_start(evening).isoformat(),
                         "2026-09-05T00:00:00+00:00")
        self.assertEqual(db.allowance_resets_at(evening).isoformat(),
                         "2026-09-06T00:00:00+00:00")
