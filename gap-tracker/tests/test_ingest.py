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


class RefusalRecordTest(unittest.TestCase):
    """The provider's own answer outranks our estimate of it.

    A ledger that had never seen the day's earlier spending reported
    "0 spent today, 20,000 of 20,000 left" one command before the API refused
    with limitType daily. The sum was right; the claim was not.
    """

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)

    @staticmethod
    def _at(hour, day=5):
        from datetime import datetime, timezone
        return datetime(2026, 9, day, hour, tzinfo=timezone.utc)

    def test_a_refusal_holds_until_its_reset(self):
        db.record_limit(self.conn, "daily", "2026-09-06T00:00:00.000Z", "exhausted")
        self.assertIsNotNone(db.limit_active(self.conn, "daily", self._at(16)))

    def test_it_stops_holding_afterwards(self):
        db.record_limit(self.conn, "daily", "2026-09-06T00:00:00.000Z", "exhausted")
        self.assertIsNone(db.limit_active(self.conn, "daily", self._at(1, day=6)))

    def test_no_refusal_recorded_is_not_a_refusal(self):
        self.assertIsNone(db.limit_active(self.conn, "daily", self._at(16)))

    def test_the_latest_refusal_replaces_the_last(self):
        db.record_limit(self.conn, "daily", "2026-09-06T00:00:00.000Z", "first")
        db.record_limit(self.conn, "daily", "2026-09-07T00:00:00.000Z", "second")
        row = db.limit_active(self.conn, "daily", self._at(1, day=6))
        self.assertEqual(row["detail"], "second")

    def test_an_unparseable_reset_is_not_treated_as_active(self):
        """Better to let a run try and be refused than to block on nonsense."""
        db.record_limit(self.conn, "daily", "not a timestamp", "odd")
        self.assertIsNone(db.limit_active(self.conn, "daily", self._at(16)))

    def test_the_server_s_facts_are_pulled_out_of_the_body(self):
        from gapscan.providers.ppt import limit_from_error
        body = ('{"error":"Daily rate limit exceeded","retryAfter":26311,'
                '"resetsAt":"2026-09-06T00:00:00.000Z","limitType":"daily"}')
        self.assertEqual(limit_from_error(body),
                         {"kind": "daily", "resets_at": "2026-09-06T00:00:00.000Z",
                          "available": None})

    def test_a_body_that_is_not_json_yields_nothing(self):
        from gapscan.providers.ppt import limit_from_error
        self.assertEqual(limit_from_error("<html>502</html>"), {})

    def test_out_of_credits_carries_the_reset(self):
        """So the caller can store it without re-parsing the body."""
        from gapscan.providers.ppt import OutOfCredits
        exc = OutOfCredits("gone", "2026-09-06T00:00:00.000Z", "detail")
        self.assertEqual(exc.resets_at, "2026-09-06T00:00:00.000Z")


class RetireStaleTest(unittest.TestCase):
    """Dropping the catalogue must not drop a ranked card.

    pokemontcg.io's ids disagree with PPT's, so a catalogued card and its
    swept twin were two entries and only the swept one was ever pinned. The
    catalogued ones are being retired -- but losing a card `rank` is ranking
    would be far worse than carrying a few dead rows.
    """

    def setUp(self):
        import tempfile
        from gapscan.store import Store
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name))

    def _retire(self, universe, dry_run=False):
        import run as cli
        return cli._retire_stale(universe, self.store, dry_run=dry_run)

    def test_a_catalogued_card_no_sweep_reached_is_dropped(self):
        n, kept = self._retire({"ex11-1": {"source": "api"}})
        self.assertEqual((n, kept), (1, {}))

    def test_a_priced_card_survives_whatever_its_source(self):
        self.store.save_quote("ex11-3", {"id": "ex11-3", "quote": {"raw": 10.0}})
        n, kept = self._retire({"ex11-3": {"source": "api"}})
        self.assertEqual(n, 0)
        self.assertIn("ex11-3", kept)

    def test_a_pinned_card_survives(self):
        """A sweep reached it, so it is a real card in PPT's catalogue."""
        n, kept = self._retire({"ex11-2": {"source": "api", "ppt_set_id": "1450"}})
        self.assertEqual(n, 0)
        self.assertIn("ex11-2", kept)

    def test_swept_cards_are_never_touched(self):
        n, kept = self._retire({"ppt-9": {"source": "sweep"}})
        self.assertEqual(n, 0)
        self.assertIn("ppt-9", kept)

    def test_a_dry_run_counts_without_removing(self):
        universe = {"ex11-1": {"source": "api"}, "ppt-9": {"source": "sweep"}}
        n, kept = self._retire(universe, dry_run=True)
        self.assertEqual(n, 1)
        self.assertEqual(len(kept), 2)

    def test_watchlist_cards_are_not_catalogued_and_stay(self):
        n, kept = self._retire({"me02-106": {"source": "manual"}})
        self.assertEqual(n, 0)
        self.assertIn("me02-106", kept)
