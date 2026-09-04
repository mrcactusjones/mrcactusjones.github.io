"""The conviction score. It must never override the floor test, and must not
reward cards simply for having more data behind them."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gapscan.scoring import Scoring, components, score


def row(**over):
    base = dict(floor_profit=120.0, all_in=260.0, floor_durability=0.85,
                sales_9=18, psa9_sale_age_days=6.0, sales_per_month=3.5,
                divergence_30d=0.08, confident=True)
    base.update(over)
    return base


class TestFloorGate(unittest.TestCase):
    def test_a_card_the_nine_does_not_pay_scores_zero(self):
        for floor in (0.0, -50.0):
            got = score(row(floor_profit=floor, floor_durability=1.0, sales_9=99,
                            psa9_sale_age_days=0, sales_per_month=50,
                            divergence_30d=1.0))
            self.assertEqual(got["conviction"], 0.0)
            self.assertIn("does not clear", got["reason"])

    def test_perfect_everything_else_cannot_rescue_it(self):
        strong = score(row(floor_profit=-1.0))
        weak = score(row(floor_profit=1.0, floor_durability=0.0, sales_9=0,
                         psa9_sale_age_days=90, sales_per_month=0,
                         divergence_30d=-0.3))
        self.assertLess(strong["conviction"], weak["conviction"])


class TestComponents(unittest.TestCase):
    def setUp(self):
        self.cfg = Scoring()

    def test_size_is_capped_so_it_cannot_dominate(self):
        modest = components(row(floor_profit=130.0, all_in=260.0), self.cfg)["size"]
        absurd = components(row(floor_profit=2600.0, all_in=260.0), self.cfg)["size"]
        self.assertAlmostEqual(modest, 1.0)
        self.assertAlmostEqual(absurd, 1.0, msg="10x the ROI must not score 10x")

    def test_depth_is_log_scaled(self):
        parts = lambda n: components(row(sales_9=n), self.cfg)["depth"]
        early = parts(5) - parts(1)
        late = parts(40) - parts(36)
        self.assertGreater(early, late, "1->5 comps matters more than 36->40")

    def test_freshness_decays_to_zero_at_the_age_limit(self):
        self.assertAlmostEqual(components(row(psa9_sale_age_days=0), self.cfg)["freshness"], 1.0)
        self.assertAlmostEqual(components(row(psa9_sale_age_days=90), self.cfg)["freshness"], 0.0)
        self.assertAlmostEqual(components(row(psa9_sale_age_days=200), self.cfg)["freshness"], 0.0)

    def test_direction_is_centred_on_no_movement(self):
        self.assertAlmostEqual(components(row(divergence_30d=0.0), self.cfg)["direction"], 0.5)
        self.assertAlmostEqual(components(row(divergence_30d=0.3), self.cfg)["direction"], 1.0)
        self.assertAlmostEqual(components(row(divergence_30d=-0.3), self.cfg)["direction"], 0.0)

    def test_missing_inputs_give_missing_components(self):
        parts = components(row(floor_durability=None, sales_per_month=None,
                               divergence_30d=None), self.cfg)
        self.assertIsNone(parts["durability"])
        self.assertIsNone(parts["liquidity"])
        self.assertIsNone(parts["direction"])
        self.assertIsNotNone(parts["depth"])


class TestRenormalisation(unittest.TestCase):
    """A card with less history must not be penalised for it."""

    def test_missing_components_are_dropped_not_zeroed(self):
        full = score(row())
        partial = score(row(floor_durability=None, divergence_30d=None))
        self.assertLess(partial["coverage"], full["coverage"])
        # Same quality on what we can see -> comparable score, not a collapse.
        self.assertGreater(partial["conviction"], full["conviction"] * 0.8)

    def test_coverage_reports_how_much_was_measurable(self):
        self.assertAlmostEqual(score(row())["coverage"], 1.0)
        thin = score(row(floor_durability=None, sales_per_month=None,
                         divergence_30d=None, psa9_sale_age_days=None))
        self.assertAlmostEqual(thin["coverage"], 0.35, places=2)

    def test_a_zero_component_still_hurts(self):
        """Absent is not the same as bad."""
        absent = score(row(floor_durability=None))
        bad = score(row(floor_durability=0.0))
        self.assertGreater(absent["conviction"], bad["conviction"])


class TestConfidence(unittest.TestCase):
    def test_thin_comps_cut_the_score_without_vetoing_it(self):
        confident = score(row())["conviction"]
        flagged = score(row(confident=False))["conviction"]
        self.assertAlmostEqual(flagged, round(confident * 0.6, 1), places=0)
        self.assertGreater(flagged, 0)


class TestWeightsAreConfigurable(unittest.TestCase):
    def test_reweighting_changes_the_ordering(self):
        steady = row(floor_profit=60.0, all_in=260.0, floor_durability=0.95, sales_9=30)
        big = row(floor_profit=260.0, all_in=260.0, floor_durability=0.30, sales_9=4)

        durability_first = Scoring(weights={"size": 0.05, "durability": 0.60, "depth": 0.15,
                                            "freshness": 0.10, "liquidity": 0.05,
                                            "direction": 0.05})
        size_first = Scoring(weights={"size": 0.70, "durability": 0.05, "depth": 0.10,
                                      "freshness": 0.05, "liquidity": 0.05,
                                      "direction": 0.05})
        self.assertGreater(score(steady, durability_first)["conviction"],
                           score(big, durability_first)["conviction"])
        self.assertGreater(score(big, size_first)["conviction"],
                           score(steady, size_first)["conviction"])


class TestTheMotivatingExample(unittest.TestCase):
    def test_a_big_thin_gap_loses_to_a_smaller_solid_one(self):
        big_thin = row(floor_profit=320.0, all_in=290.0, floor_durability=0.12,
                       sales_9=1, psa9_sale_age_days=74.0, sales_per_month=0.3,
                       divergence_30d=-0.02, confident=False)
        small_solid = row(floor_profit=48.0, all_in=220.0, floor_durability=0.94,
                          sales_9=31, psa9_sale_age_days=3.0, sales_per_month=6.0,
                          divergence_30d=0.19, confident=True)
        self.assertGreater(small_solid["floor_profit"] * 0 + score(small_solid)["conviction"],
                           score(big_thin)["conviction"])
