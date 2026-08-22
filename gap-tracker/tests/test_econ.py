"""Sanity tests for the money math -- the one part that must not be subtly wrong."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gapscan.config import Economics, Thresholds
from gapscan.econ import Quote, breakeven_probability, evaluate
from gapscan.providers.ppt import extract_quote


class TestEconomics(unittest.TestCase):
    def setUp(self):
        # Round numbers so the assertions are checkable by hand.
        self.econ = Economics(grading_fee=20.0, sub_ship_per_card=5.0,
                              sale_fee_pct=0.10, ship_out=5.0, raw_premium_pct=0.0)
        self.th = Thresholds()

    def test_all_in_and_net(self):
        self.assertAlmostEqual(self.econ.all_in(100.0), 125.0)
        self.assertAlmostEqual(self.econ.net_proceeds(100.0), 85.0)

    def test_raw_premium_raises_cost(self):
        econ = Economics(grading_fee=20.0, sub_ship_per_card=5.0, sale_fee_pct=0.10,
                         ship_out=5.0, raw_premium_pct=0.20)
        self.assertAlmostEqual(econ.all_in(100.0), 145.0)

    def test_no_brainer_needs_profit_and_confidence(self):
        # raw 50 -> all-in 75; PSA 9 at 200 nets 175. Clears easily.
        good = Quote(raw=50, psa9=200, psa10=600, sales_9=20, sales_10=10)
        v = evaluate(good, self.econ, self.th)
        self.assertEqual(v.verdict, "no_brainer")
        self.assertAlmostEqual(v.floor_profit, 100.0)
        self.assertTrue(v.confident)

    def test_thin_comps_downgrade_the_verdict(self):
        thin = Quote(raw=50, psa9=200, psa10=600, sales_9=1, sales_10=0)
        v = evaluate(thin, self.econ, self.th)
        self.assertEqual(v.verdict, "floor_positive")  # same money, less trust
        self.assertFalse(v.confident)
        self.assertTrue(any("PSA 9 comps" in r for r in v.reasons))

    def test_ten_or_bust_when_the_nine_loses(self):
        q = Quote(raw=50, psa9=60, psa10=400, sales_9=20, sales_10=10)
        v = evaluate(q, self.econ, self.th)
        self.assertEqual(v.verdict, "ten_or_bust")
        self.assertLess(v.floor_profit, 0)
        self.assertGreater(v.upside_profit, 0)
        self.assertIsNotNone(v.breakeven_p10)

    def test_dead_when_even_the_ten_loses(self):
        q = Quote(raw=100, psa9=90, psa10=110, sales_9=20, sales_10=10)
        v = evaluate(q, self.econ, self.th)
        self.assertEqual(v.verdict, "dead")

    def test_missing_prices_are_unjudgeable(self):
        self.assertIsNone(evaluate(Quote(raw=None, psa9=100), self.econ, self.th))
        self.assertIsNone(evaluate(Quote(raw=100, psa9=None), self.econ, self.th))

    def test_breakeven_probability(self):
        # all-in 100, a 9 nets 50, a 10 nets 150 -> need half to come back 10.
        self.assertAlmostEqual(breakeven_probability(100, 50, 150), 0.5)
        # Already profitable at a 9.
        self.assertEqual(breakeven_probability(100, 120, 200), 0.0)
        # Even 100% tens can't cover it.
        self.assertIsNone(breakeven_probability(500, 50, 150))
        # No spread between the grades.
        self.assertIsNone(breakeven_probability(100, 50, 50))

    def test_psa10_absent_falls_back_to_the_nine(self):
        q = Quote(raw=50, psa9=200, psa10=None, sales_9=20)
        v = evaluate(q, self.econ, self.th)
        self.assertAlmostEqual(v.upside_profit, v.floor_profit)


class TestProviderExtraction(unittest.TestCase):
    """The PPT response shape is unverified, so the extractor must be forgiving."""

    def test_nested_shape(self):
        blob = {"data": [{"prices": {
            "raw": {"market": 42.5},
            "psa9": {"average": 130.0, "salesCount": 12},
            "psa10": {"average": 410.0, "salesCount": 7}}}]}
        q = extract_quote(blob)
        self.assertAlmostEqual(q.raw, 42.5)
        self.assertAlmostEqual(q.psa9, 130.0)
        self.assertAlmostEqual(q.psa10, 410.0)
        self.assertEqual(q.sales_9, 12)
        self.assertEqual(q.sales_10, 7)

    def test_flat_shape_with_string_prices(self):
        blob = {"ungradedPrice": "$42.50", "psa_9_price": "130.00",
                "psa_10_price": "410.00"}
        q = extract_quote(blob)
        self.assertAlmostEqual(q.raw, 42.5)
        self.assertAlmostEqual(q.psa9, 130.0)
        self.assertAlmostEqual(q.psa10, 410.0)

    def test_graded_field_is_never_mistaken_for_raw(self):
        blob = {"psa10": {"marketPrice": 999.0}, "rawMarket": 20.0}
        self.assertAlmostEqual(extract_quote(blob).raw, 20.0)

    def test_documented_v2_shape(self):
        """The shape their API docs advertise: flat, `market` for raw."""
        blob = {"data": [{"name": "Magikarp", "set": "Paldea Evolved",
                          "market": 264.46, "psa10": 1356.83, "psa9": 410.0,
                          "roi": "+109%", "delta7d": "+6.1%"}]}
        q = extract_quote(blob)
        self.assertAlmostEqual(q.raw, 264.46)
        self.assertAlmostEqual(q.psa9, 410.0)
        self.assertAlmostEqual(q.psa10, 1356.83)

    def test_psa9_pattern_does_not_swallow_psa10(self):
        blob = {"psa10": {"market": 400.0}, "psa9": {"market": 100.0}}
        q = extract_quote(blob)
        self.assertAlmostEqual(q.psa9, 100.0)
        self.assertAlmostEqual(q.psa10, 400.0)


if __name__ == "__main__":
    unittest.main()


class TestCardMatching(unittest.TestCase):
    """Their search is fuzzy; taking the first hit would price the wrong card."""

    def setUp(self):
        from gapscan.providers.ppt import pick_match, results_of
        self.pick, self.results_of = pick_match, results_of
        self.card = {"id": "base1-4", "name": "Charizard",
                     "set_name": "Base Set", "number": "4"}

    def test_matches_on_set_and_name(self):
        hits = [{"name": "Charizard EX - XY29", "setName": "XY Promos", "cardNumber": "XY29"},
                {"name": "Charizard", "setName": "Base Set", "cardNumber": "4"}]
        record, why = self.pick(hits, self.card)
        self.assertEqual(record["setName"], "Base Set")
        self.assertIn("number", why)

    def test_rejects_a_wrong_card_rather_than_guessing(self):
        hits = [{"name": "Charizard EX - XY29", "setName": "XY Promos", "cardNumber": "XY29"}]
        record, why = self.pick(hits, self.card)
        self.assertIsNone(record, "a same-name card from another set is not a match")
        self.assertIn("no confident match", why)

    def test_empty_results(self):
        self.assertEqual(self.pick([], self.card)[0], None)

    def test_envelope_shapes(self):
        rows = [{"name": "x", "cardNumber": "1"}]
        self.assertEqual(self.results_of({"data": rows}), rows)
        self.assertEqual(self.results_of(rows), rows)
        self.assertEqual(self.results_of({"cards": rows}), rows)
        self.assertEqual(self.results_of({"name": "x"}), [{"name": "x"}])
        self.assertEqual(self.results_of({"nope": 1}), [])

    def test_number_mismatch_alone_is_not_enough(self):
        # Right set, wrong card entirely.
        hits = [{"name": "Blastoise", "setName": "Base Set", "cardNumber": "2"}]
        self.assertIsNone(self.pick(hits, self.card)[0])
