"""Hand-picked cards must be pinned to an exact record before being tracked."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gapscan import watchlist


def record(set_name, number, name, rarity="Illustration Rare", ext=None):
    return {"setName": set_name, "cardNumber": number, "name": name,
            "rarity": rarity, "id": "ppt123",
            "externalCatalogId": ext or f"{set_name.lower()}-{number}"}


class TestResolution(unittest.TestCase):
    def setUp(self):
        self.entry = {"name": "Clefairy", "number": "094", "set_hint": "POR 2026",
                      "note": "Illustration Rare"}

    def test_unresolved_entries_are_not_tracked(self):
        self.assertFalse(watchlist.is_resolved(self.entry))
        self.assertEqual(watchlist.to_universe({"cards": [self.entry]}), {})

    def test_number_match_selects_the_candidate(self):
        hits = watchlist.candidates_for(self.entry, [
            record("Some Other Set", "012", "Clefairy"),
            record("Portal", "094/106", "Clefairy"),
        ])
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["setName"], "Portal")

    def test_ambiguity_is_surfaced_not_guessed(self):
        hits = watchlist.candidates_for(self.entry, [
            record("Portal", "094/106", "Clefairy"),
            record("Portal Reprint", "094/200", "Clefairy"),
        ])
        self.assertEqual(len(hits), 2, "two candidates must stay two, for a human to pick")

    def test_applied_resolution_makes_it_trackable(self):
        watchlist.apply_resolution(self.entry, record("Portal", "094/106", "Clefairy"))
        self.assertTrue(watchlist.is_resolved(self.entry))
        universe = watchlist.to_universe({"cards": [self.entry]})
        row = next(iter(universe.values()))
        self.assertEqual(row["set_name"], "Portal")
        self.assertEqual(row["tier"], "watchlist")
        self.assertEqual(row["source"], "manual")
        self.assertEqual(row["priority"], 100)
        self.assertIsNone(row["raw_hint"], "the provider supplies the raw price")

    def test_the_shipped_watchlist_file_is_valid(self):
        blob = watchlist.load()
        self.assertTrue(blob["cards"], "watchlist should not be empty")
        for entry in blob["cards"]:
            self.assertIn("name", entry)
            self.assertIn("number", entry)

    def test_resolution_persists_without_touching_the_tracked_file(self):
        import json as _json
        import tempfile
        watchlist.apply_resolution(self.entry, record("Portal", "094/106", "Clefairy"))
        with tempfile.TemporaryDirectory() as tmp:
            tracked = Path(tmp) / "watchlist.json"
            local = Path(tmp) / "local.json"
            # The tracked file holds only the request, never the answer.
            tracked.write_text(_json.dumps(
                {"cards": [{"name": "Clefairy", "number": "094", "set_hint": "POR 2026"}]}))
            before = tracked.read_text()

            watchlist.save({"cards": [self.entry]}, local)
            self.assertEqual(tracked.read_text(), before,
                             "saving must not modify the tracked list")

            again = watchlist.load(tracked, local)
        self.assertTrue(watchlist.is_resolved(again["cards"][0]))
        self.assertEqual(again["cards"][0]["set_name"], "Portal")

    def test_resolution_left_in_the_tracked_file_is_migrated(self):
        import json as _json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tracked = Path(tmp) / "watchlist.json"
            local = Path(tmp) / "local.json"
            resolved = dict(self.entry, set_name="Portal", external_id="por-094")
            tracked.write_text(_json.dumps({"cards": [resolved]}))

            blob = watchlist.load(tracked, local)
            self.assertTrue(watchlist.is_resolved(blob["cards"][0]))
            watchlist.save(blob, local)
            # Now it lives locally, so the tracked file can be reverted safely.
            tracked.write_text(_json.dumps({"cards": [self.entry]}))
            again = watchlist.load(tracked, local)
        self.assertEqual(again["cards"][0]["set_name"], "Portal")


if __name__ == "__main__":
    unittest.main()


class FindByNumberTest(unittest.TestCase):
    """Resolution matches on card number, so it has to see enough results.

    Every 2026 card on the list failed to resolve because the search asked for
    one result -- a leftover from the 100-credit free tier -- and one result
    for "Clefairy" is whichever Clefairy the API ranks first out of hundreds.
    """

    ENTRY = {"name": "Clefairy", "number": "094"}

    @staticmethod
    def _rec(number, set_name="Some Set"):
        return {"cardNumber": number, "setName": set_name, "name": "Clefairy"}

    def _pager(self, pages):
        """A fetch_page that serves fixed pages and counts the calls."""
        self.calls = []

        def fetch(offset):
            self.calls.append(offset)
            index = offset // 25
            return pages[index] if index < len(pages) else []
        return fetch

    def test_a_match_on_a_later_page_is_found(self):
        pages = [[self._rec(str(i)) for i in range(25)],
                 [self._rec(str(i)) for i in range(25, 50)],
                 [self._rec("094", "Prismatic")] + [self._rec(str(i)) for i in range(24)]]
        records, hits = watchlist.find_by_number(self._pager(pages), self.ENTRY, 4, 25)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["setName"], "Prismatic")
        self.assertEqual(len(records), 75)

    def test_it_stops_as_soon_as_it_matches(self):
        pages = [[self._rec("094")], [self._rec("094")]]
        watchlist.find_by_number(self._pager(pages), self.ENTRY, 4, 25)
        self.assertEqual(self.calls, [0], "paid for a page it did not need")

    def test_a_short_page_ends_the_scan(self):
        """Results ran out; more pages cost credits and return nothing."""
        pages = [[self._rec(str(i)) for i in range(3)]]
        watchlist.find_by_number(self._pager(pages), self.ENTRY, 4, 25)
        self.assertEqual(self.calls, [0])

    def test_it_never_scans_more_pages_than_asked(self):
        full = [self._rec(str(i)) for i in range(25)]
        watchlist.find_by_number(self._pager([full] * 10), self.ENTRY, 3, 25)
        self.assertEqual(self.calls, [0, 25, 50])

    def test_no_match_returns_everything_seen(self):
        """The caller reports which sets came back, so the miss is diagnosable."""
        pages = [[self._rec("1", "Base")], []]
        records, hits = watchlist.find_by_number(self._pager(pages), self.ENTRY, 4, 25)
        self.assertEqual(hits, [])
        self.assertEqual(len(records), 1)

    def test_one_page_is_still_honoured(self):
        full = [self._rec(str(i)) for i in range(25)]
        watchlist.find_by_number(self._pager([full] * 4), self.ENTRY, 1, 25)
        self.assertEqual(self.calls, [0])
