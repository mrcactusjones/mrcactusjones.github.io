"""Trend maths. These decide which gaps look real, so they must not flatter noise."""
from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gapscan import trends

TODAY = date(2026, 9, 4)


def line(values, end=TODAY, step=1):
    """A series ending today, one point every `step` days."""
    return [((end - timedelta(days=step * (len(values) - 1 - i))).isoformat(), v)
            for i, v in enumerate(values)]


class TestChange(unittest.TestCase):
    def test_rising_series(self):
        pts = line([100, 100, 100, 120, 120, 120])
        self.assertAlmostEqual(trends.change_pct(pts, 30, TODAY), 0.2)

    def test_falling_series(self):
        pts = line([200, 200, 200, 100, 100, 100])
        self.assertAlmostEqual(trends.change_pct(pts, 30, TODAY), -0.5)

    def test_two_points_is_not_a_trend(self):
        self.assertIsNone(trends.change_pct(line([100, 500]), 30, TODAY))

    def test_a_single_spike_does_not_set_the_trend(self):
        # One absurd sale in the middle must not register as a move.
        steady = trends.change_pct(line([100, 100, 100, 100, 100, 100]), 30, TODAY)
        spiked = trends.change_pct(line([100, 100, 9999, 100, 100, 100]), 30, TODAY)
        self.assertEqual(steady, spiked)

    def test_window_excludes_old_points(self):
        old = line([500, 500, 500], end=TODAY - timedelta(days=200))
        new = line([100, 100, 100, 100])
        self.assertAlmostEqual(trends.change_pct(old + new, 30, TODAY), 0.0)


class TestVolatility(unittest.TestCase):
    def test_flat_series_has_no_volatility(self):
        self.assertAlmostEqual(trends.volatility(line([50] * 10), 90, TODAY), 0.0)

    def test_choppy_series_is_more_volatile_than_smooth(self):
        smooth = trends.volatility(line([100, 101, 102, 103, 104, 105]), 90, TODAY)
        choppy = trends.volatility(line([100, 140, 90, 150, 80, 145]), 90, TODAY)
        self.assertGreater(choppy, smooth)

    def test_needs_enough_points(self):
        self.assertIsNone(trends.volatility(line([1, 2, 3]), 90, TODAY))


class TestGapSeries(unittest.TestCase):
    def setUp(self):
        # Signature mirrors Economics.all_in(raw, declared_value).
        self.all_in = lambda raw, declared=None: raw + 25
        self.net = lambda price: price * 0.9

    def test_only_days_with_both_prices_count(self):
        raw = line([100, 100], end=TODAY - timedelta(days=5))
        graded = line([300, 300])
        gaps = trends.gap_series(raw, graded, self.all_in, self.net)
        # Raw carries forward to the graded days, but nothing before the first raw.
        self.assertEqual(len(gaps), 2)
        self.assertAlmostEqual(gaps[0][1], 300 * 0.9 - 125)

    def test_declared_value_is_the_graded_price(self):
        """History must be costed the way a live floor is, or the past looks cheap."""
        seen = []
        def all_in(raw, declared=None):
            seen.append((raw, declared))
            return raw + 25
        trends.gap_series(line([100, 100]), line([300, 300]), all_in, self.net)
        self.assertTrue(all(d == 300 for _, d in seen), seen)

    def test_graded_before_any_raw_is_skipped(self):
        graded = line([300, 300], end=TODAY - timedelta(days=10))
        raw = line([100, 100])
        self.assertEqual(trends.gap_series(raw, graded, self.all_in, self.net), [])


class TestPersistence(unittest.TestCase):
    def test_days_held_counts_only_the_window(self):
        pts = line([100] * 5, end=TODAY - timedelta(days=200)) + line([100] * 4)
        self.assertEqual(trends.held_days(pts, 50, 90, TODAY), 4)

    def test_streak_breaks_on_a_dip(self):
        # Three daily points clearing the bar span three calendar days.
        self.assertEqual(trends.current_streak(line([90, 10, 90, 90, 90]), 50), 3)

    def test_streak_is_zero_when_the_latest_fails(self):
        self.assertEqual(trends.current_streak(line([90, 90, 10]), 50), 0)

    def test_streak_counts_days_not_observations(self):
        """A price every third day must not claim a longer streak than elapsed."""
        sparse = line([90, 90, 90, 90], step=3)   # 4 points spanning 10 days
        self.assertEqual(trends.current_streak(sparse, 50), 10)
        dense = line([90, 90, 90, 90], step=1)
        self.assertEqual(trends.current_streak(dense, 50), 4)

    def test_observations_gives_the_denominator(self):
        pts = line([90, 10, 90], step=1)
        self.assertEqual(trends.held_days(pts, 50, 90, TODAY), 2)
        self.assertEqual(trends.observations(pts, 90, TODAY), 3)


class TestDivergence(unittest.TestCase):
    def test_graded_pulling_away_is_positive(self):
        raw = line([100, 100, 100, 100, 100, 100])
        psa9 = line([200, 200, 200, 300, 300, 300])
        self.assertAlmostEqual(trends.divergence(raw, psa9, 30, TODAY), 0.5)

    def test_raw_catching_up_is_negative(self):
        raw = line([100, 100, 100, 200, 200, 200])
        psa9 = line([300, 300, 300, 300, 300, 300])
        self.assertAlmostEqual(trends.divergence(raw, psa9, 30, TODAY), -1.0)

    def test_missing_data_yields_none(self):
        self.assertIsNone(trends.divergence(line([100, 200]), line([1, 2, 3, 4]), 30, TODAY))


class TestSummary(unittest.TestCase):
    def test_summary_keys_present(self):
        raw = line([100] * 8)
        psa9 = line([300] * 8)
        floor = line([150] * 8)
        got = trends.summarise(raw, psa9, [], floor, threshold=100, today=TODAY)
        self.assertEqual(got["floor_streak"], 8)
        self.assertEqual(got["floor_days_held_90d"], 8)
        self.assertIsNone(got["psa10_30d"], "no PSA 10 series means no PSA 10 trend")
        self.assertEqual(got["history_days"], 8)


if __name__ == "__main__":
    unittest.main()


class TestWorstCaseFloor(unittest.TestCase):
    """The point of the tool is the bad case, so the bad case must be measured."""

    def test_worst_finds_the_dip(self):
        self.assertAlmostEqual(trends.worst(line([120, 40, 130, 125]), 90, TODAY), 40)

    def test_percentile_ignores_a_lone_bad_print(self):
        pts = line([100, 100, 100, 100, 100, 100, 100, 100, 100, 5])
        self.assertAlmostEqual(trends.worst(pts, 90, TODAY), 5)
        self.assertAlmostEqual(trends.percentile(pts, 0.10, 90, TODAY), 100)

    def test_durability_is_worst_over_current(self):
        # Current 100, worst 80 -> four fifths of the floor survived.
        self.assertAlmostEqual(trends.durability(line([100, 80, 90, 100]), 90, TODAY), 0.8)

    def test_a_floor_that_went_negative_has_no_durability(self):
        self.assertAlmostEqual(trends.durability(line([100, -20, 50, 100]), 90, TODAY), 0.0)

    def test_durability_of_a_flat_floor_is_one(self):
        self.assertAlmostEqual(trends.durability(line([100] * 5), 90, TODAY), 1.0)

    def test_too_few_points_is_unknown_not_perfect(self):
        self.assertIsNone(trends.durability(line([100, 100]), 90, TODAY))

    def test_window_bounds_the_worst_case(self):
        old_crash = line([-500], end=TODAY - timedelta(days=200))
        recent = line([100, 100, 100])
        self.assertAlmostEqual(trends.worst(old_crash + recent, 90, TODAY), 100)


class DurabilityOrderTest(unittest.TestCase):
    """`trends` ranked durable losses beside durable wins.

    Eight of the first full sweep's top twenty had a negative floor. Sorting
    on days-held alone made "held 36/38" read as an endorsement of a card that
    is under water today.
    """

    @staticmethod
    def _row(name, floor, held):
        return {"name": name, "floor_profit": floor, "floor_days_held_90d": held}

    def test_a_live_floor_outranks_a_longer_held_dead_one(self):
        rows = [self._row("collapsed", -154.14, 36), self._row("live", 45.40, 20)]
        self.assertEqual([r["name"] for r in trends.by_durability(rows)],
                         ["live", "collapsed"])

    def test_among_live_floors_the_better_held_one_wins(self):
        rows = [self._row("thin", 500.0, 5), self._row("durable", 100.0, 35)]
        self.assertEqual([r["name"] for r in trends.by_durability(rows)],
                         ["durable", "thin"])

    def test_days_held_ties_break_on_the_floor(self):
        rows = [self._row("small", 10.0, 30), self._row("big", 900.0, 30)]
        self.assertEqual([r["name"] for r in trends.by_durability(rows)],
                         ["big", "small"])

    def test_underwater_rows_are_kept_not_dropped(self):
        """A collapse is often the more interesting story; it just isn't a buy."""
        rows = [self._row("collapsed", -154.14, 36)]
        self.assertEqual(len(trends.by_durability(rows)), 1)

    def test_a_zero_floor_is_not_live(self):
        rows = [self._row("zero", 0.0, 40), self._row("live", 1.0, 1)]
        self.assertEqual([r["name"] for r in trends.by_durability(rows)][0], "live")
