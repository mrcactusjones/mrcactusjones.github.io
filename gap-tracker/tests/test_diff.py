"""Day-over-day reporting.

`diff` exists to say what changed. Its first real run reported "437 card(s)
entered the ranking" when every one of them was a card the older snapshot had
simply never priced -- including three sitting in the top five of both
rankings. The numbers were right and the reading was wrong.
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run as cli
from gapscan.config import Config
from gapscan.store import Store


class _Args:
    def __init__(self, **kw):
        self.date = self.against = None
        self.top = 5
        self.min_move = 25.0
        self.__dict__.update(kw)


class DiffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name))
        self.store.history.mkdir(parents=True, exist_ok=True)
        self.store.save_universe({})

    def _snapshot(self, day, rows):
        (self.store.history / f"{day}.json").write_text(
            json.dumps({"date": day, "rows": rows}))

    @staticmethod
    def _row(card_id, verdict="ten_or_bust", floor=10.0):
        return {"id": card_id, "verdict": verdict, "floor_profit": floor}

    def _run(self, **kw):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.cmd_diff(_Args(**kw), Config(), self.store)
        return code, out.getvalue()

    def test_both_snapshot_sizes_are_reported(self):
        """The reader should never have to work out coverage from arithmetic."""
        self._snapshot("2026-09-04", [self._row(f"c{i}") for i in range(20)])
        self._snapshot("2026-09-05", [self._row(f"c{i}") for i in range(60)])
        _, out = self._run()
        self.assertIn("2026-09-04 (20 cards) -> 2026-09-05 (60 cards)", out)

    def test_a_coverage_jump_is_named_as_one(self):
        self._snapshot("2026-09-04", [self._row(f"c{i}") for i in range(20)])
        self._snapshot("2026-09-05", [self._row(f"c{i}") for i in range(60)])
        _, out = self._run()
        self.assertIn("coverage grew by 40", out)

    def test_a_steady_run_gets_no_coverage_warning(self):
        self._snapshot("2026-09-04", [self._row(f"c{i}") for i in range(100)])
        self._snapshot("2026-09-05", [self._row(f"c{i}") for i in range(101)])
        _, out = self._run()
        self.assertNotIn("coverage", out)

    def test_a_card_in_both_never_reads_as_having_entered(self):
        """Ho-Oh ex was in the top five of both and listed as a new arrival."""
        self._snapshot("2026-09-04", [self._row("ex10-104", "no_brainer", 700.0)])
        self._snapshot("2026-09-05", [self._row("ex10-104", "no_brainer", 713.0),
                                      self._row("new-1")])
        _, out = self._run()
        entered = out.split("entered the ranking")[1]
        self.assertNotIn("ex10-104", entered)
        self.assertIn("new-1", entered)

    def test_entrants_are_grouped_best_verdict_first(self):
        """A new no-brainer must not be buried under hundreds of dead cards."""
        self._snapshot("2026-09-04", [self._row("old")])
        self._snapshot("2026-09-05", [self._row("old")]
                       + [self._row(f"d{i}", "dead") for i in range(30)]
                       + [self._row("gem", "no_brainer", 500.0)])
        _, out = self._run()
        entered = out.split("entered the ranking")[1]
        self.assertLess(entered.index("no_brainer"), entered.index("dead"))
        self.assertIn("gem", entered)

    def test_verdict_moves_are_reported_with_direction(self):
        self._snapshot("2026-09-04", [self._row("a", "no_brainer", 500.0),
                                      self._row("b", "dead", -50.0)])
        self._snapshot("2026-09-05", [self._row("a", "ten_or_bust", 20.0),
                                      self._row("b", "floor_positive", 60.0)])
        _, out = self._run()
        self.assertIn("1 moved up", out)
        self.assertIn("1 moved down", out)

    def test_floor_moves_under_the_threshold_are_not_reported(self):
        self._snapshot("2026-09-04", [self._row("a", floor=100.0)])
        self._snapshot("2026-09-05", [self._row("a", floor=105.0)])
        _, out = self._run()
        self.assertIn("nothing moved", out)

    def test_one_snapshot_is_not_a_diff(self):
        self._snapshot("2026-09-05", [self._row("a")])
        code, out = self._run()
        self.assertEqual(code, 1)
        self.assertIn("needs two", out)

    def test_an_unknown_date_says_what_it_has(self):
        self._snapshot("2026-09-04", [self._row("a")])
        self._snapshot("2026-09-05", [self._row("a")])
        code, out = self._run(date="2020-01-01")
        self.assertEqual(code, 1)
        self.assertIn("2026-09-05", out)


class SnapshotDateTest(unittest.TestCase):
    def test_a_snapshot_is_filed_under_the_local_date(self):
        """Filed by UTC, an evening run west of Greenwich lands on tomorrow."""
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            # 23:00 local on the 4th is already the 5th in UTC.
            with mock.patch("gapscan.store.date") as fake:
                fake.today.return_value = date(2026, 9, 4)
                path = store.save_snapshot({"rows": []})
            self.assertEqual(path.stem, "2026-09-04")


if __name__ == "__main__":
    unittest.main()
