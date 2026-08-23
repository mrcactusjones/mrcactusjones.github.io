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

    def test_resolved_entry_survives_a_round_trip(self, ):
        import tempfile
        watchlist.apply_resolution(self.entry, record("Portal", "094/106", "Clefairy"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "w.json"
            watchlist.save({"cards": [self.entry]}, path)
            again = watchlist.load(path)
        self.assertTrue(watchlist.is_resolved(again["cards"][0]))
        self.assertEqual(again["cards"][0]["set_name"], "Portal")


if __name__ == "__main__":
    unittest.main()
