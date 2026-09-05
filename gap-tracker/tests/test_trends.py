"""Trend maths. These decide which gaps look real, so they must not flatter noise."""
from __future__ import annotations

import random
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gapscan import rank, trends

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

    def test_the_same_move_reads_the_same_however_many_comps_a_card_has(self):
        """The defect that made trend figures incomparable.

        Ends used to be a fixed three points, so on a four-point series the two
        medians shared two values and cancelled the move: a clean +30% climb
        read +9% on a thin card and +26% on a well-comped one, while the
        ranking sorted on those numbers.
        """
        def climb(n, move=0.30):
            return line([100.0 * (1 + move * i / (n - 1)) for i in range(n)])

        readings = [trends.change_pct(climb(n), 9999, TODAY)
                    for n in (4, 6, 10, 20, 40, 90)]
        self.assertLess(max(readings) - min(readings), 0.06,
                        f"sample size still moves the answer: {readings}")
        # Medians sit inside the window, so a steady climb reads short of the
        # full move -- consistently, and never over it.
        for reading in readings:
            self.assertLess(reading, 0.30)
            self.assertGreater(reading, 0.15)

    def test_a_step_change_is_reported_at_its_true_size(self):
        """No extrapolation: a move that happened is not scaled up."""
        for n in (4, 6, 12, 30):
            pts = line([100.0] * (n // 2) + [120.0] * (n - n // 2))
            self.assertAlmostEqual(trends.change_pct(pts, 9999, TODAY), 0.20,
                                   msg=f"n={n}")

    def test_one_wild_sale_at_the_end_cannot_carry_the_trend(self):
        """The end is where an outlier does the most damage."""
        pts = line([100.0] * 11 + [900.0])
        self.assertAlmostEqual(trends.change_pct(pts, 9999, TODAY), 0.0)

    def test_a_new_sale_does_not_lurch_a_thin_card(self):
        """One arrival used to double a thin card's reported trend.

        Four points with the old fixed ends read +9.1%; a fifth point 1.5%
        above the last took it to +18.2%. Nothing about the market had
        changed. At the shortest series a new sale still moves the number --
        two points is half the end median -- but it may not swamp it.
        """
        before = trends.change_pct(line([100, 110, 120, 130]), 9999, TODAY)
        after = trends.change_pct(line([100, 110, 120, 130, 132]), 9999, TODAY)
        self.assertLess(after / before, 1.5, f"{before:.3f} -> {after:.3f}")

    def test_a_new_sale_barely_registers_once_a_card_is_well_comped(self):
        values = [100.0 + i for i in range(30)]
        before = trends.change_pct(line(values), 9999, TODAY)
        after = trends.change_pct(line(values + [130.0]), 9999, TODAY)
        self.assertLess(abs(after - before), 0.01, f"{before:.3f} -> {after:.3f}")


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


# Vaporeon (Jungle 12), every PSA 9 sale the tracker had stored on 2026-09-05.
# Two printings under one title-parsed grade: this is the case the detector
# exists for, so it is checked in rather than described.
VAPOREON_PSA9 = [
    312.50, 500.00, 356.45, 1093.00, 660.25, 499.99, 300.00, 795.77, 599.99,
    999.00, 390.00, 299.00, 1165.00, 300.00, 1026.00, 502.00, 791.67, 798.88,
    789.99, 867.11, 340.00, 1029.00, 1200.00, 305.00, 354.49, 424.99, 345.00,
    400.00, 399.99, 890.00, 329.99, 1200.00, 330.00, 1000.00, 325.00, 764.22,
    349.99, 334.36, 280.00, 1100.00, 337.26, 375.00, 394.58, 899.00, 1150.00,
    850.00, 1000.00, 871.10, 379.99, 906.51, 656.00, 1067.50, 1050.00, 365.00,
    306.26,
]


def _noisy(n, spread, seed, base=400.0, climb=0.0):
    """One card's sales: lognormal noise, optionally trending."""
    rng = random.Random(seed)
    return [base * (1 + climb * i / max(1, n - 1)) * rng.lognormvariate(0, spread)
            for i in range(n)]


class CompsSplitTest(unittest.TestCase):
    """Two cards' sales pooled under one grade.

    PPT reads grades off eBay listing titles, and a title carries the grade but
    not the printing. So a WOTC holo's PSA 9 "price" can be its 1st Edition and
    Unlimited sales averaged into a number no copy sells for -- and the floor
    is computed from it.
    """

    def test_the_card_that_exposed_this(self):
        split = trends.comps_split(VAPOREON_PSA9)
        self.assertIsNotNone(split, "55 real sales, 2.6x apart, must split")
        self.assertAlmostEqual(split.low, 347.50, delta=5)
        self.assertAlmostEqual(split.high, 906.51, delta=20)
        self.assertEqual([split.low_count, split.high_count], [28, 27])
        # The provider reported $510.50 -- between the two, and neither.
        self.assertLess(split.low, 510.50)
        self.assertGreater(split.high, 510.50)

    def test_one_card_with_ordinary_noise_does_not_split(self):
        for spread in (0.15, 0.25, 0.35):
            for seed in range(12):
                self.assertIsNone(trends.comps_split(_noisy(40, spread, seed)),
                                  f"spread={spread} seed={seed}")

    def test_a_climbing_card_does_not_split(self):
        """A trend widens the spread without there being two cards."""
        for seed in range(12):
            self.assertIsNone(
                trends.comps_split(_noisy(40, 0.25, seed, climb=0.6)), seed)

    def test_two_cards_far_apart_are_caught(self):
        for mult in (2.5, 3.0, 4.0):
            for seed in range(8):
                pooled = (_noisy(22, 0.20, seed)
                          + _noisy(18, 0.20, seed + 100, base=400.0 * mult))
                split = trends.comps_split(pooled)
                self.assertIsNotNone(split, f"{mult}x seed={seed}")
                # The cheap side must be recovered near its true $400 median.
                self.assertAlmostEqual(split.low, 400.0, delta=60,
                                       msg=f"{mult}x seed={seed}")

    def test_too_few_sales_to_judge(self):
        self.assertIsNone(trends.comps_split([100, 100, 400, 400]))
        self.assertIsNone(trends.comps_split(VAPOREON_PSA9[:7]))

    def test_a_lone_outlier_cannot_be_a_cluster(self):
        """One absurd sale is not a second card; min_share keeps it out."""
        self.assertIsNone(trends.comps_split([100.0] * 30 + [9999.0]))

    def test_zero_and_missing_prices_are_ignored(self):
        self.assertIsNone(trends.comps_split([0, None, 0, None] * 4))

    def test_the_threshold_is_honoured(self):
        """Below min_spread nothing fires, however it is configured."""
        pooled = _noisy(20, 0.10, 3) + _noisy(20, 0.10, 4, base=1200.0)
        self.assertIsNotNone(trends.comps_split(pooled, min_spread=2.0))
        self.assertIsNone(trends.comps_split(pooled, min_spread=99.0))


class CheapVariantPriceTest(unittest.TestCase):
    """The floor of a two-printing card is priced off the cheap one."""

    @staticmethod
    def _split(low, high=1000.0):
        return trends.CompsSplit(boundary=(low + high) / 2, low=low, high=high,
                                 low_count=20, high_count=18, spread=2.6)

    def test_the_cheap_cluster_replaces_the_blend(self):
        # Vaporeon: the provider blends two printings into $510.50.
        self.assertEqual(rank.cheap_variant_price(510.50, self._split(347.50)),
                         347.50)

    def test_it_never_raises_a_price(self):
        """A contamination warning must not make a card look better.

        The cluster is a median of past sales; the quote is today's figure. On
        a card whose price has run up, the cluster can sit above the quote.
        """
        self.assertEqual(rank.cheap_variant_price(300.00, self._split(347.50)),
                         300.00)

    def test_it_rounds_to_cents(self):
        self.assertEqual(rank.cheap_variant_price(999.0, self._split(347.499)),
                         347.50)


class SnapshotAccumulationTest(unittest.TestCase):
    """A blended snapshot is not a sale, and one lands on every run.

    `price_points` stores both under one grade. Mixing them meant each daily
    run appended another copy of the provider's blended figure, which wandered
    the trend with no market movement behind it and -- after about a month --
    packed the middle of the distribution until the two-printings check
    stopped firing altogether.
    """

    SALES = [(f"2026-{6 + i // 15:02d}-{1 + (i * 2) % 28:02d}", v)
             for i, v in enumerate(VAPOREON_PSA9[:33])]
    BLEND = 510.50

    def _with_snapshots(self, runs):
        from datetime import timedelta
        start = date(2026, 9, 4)
        return sorted(self.SALES) + [
            ((start + timedelta(days=i)).isoformat(), self.BLEND) for i in range(runs)]

    def test_snapshots_eventually_hide_the_split(self):
        """The failure this guards against, asserted directly."""
        mixed = self._with_snapshots(40)
        vals = [v for _, v in mixed]
        self.assertIsNone(trends.comps_split(vals),
                          "if this starts passing, mixing has stopped hiding splits")

    def test_the_sales_alone_still_split_however_many_runs_have_happened(self):
        sales_only = [v for _, v in self.SALES]
        for runs in (0, 7, 30, 60, 200):
            # Runs add snapshots, never sales, so the answer must not move.
            self.assertIsNotNone(trends.comps_split(sales_only), f"{runs} runs")

    def test_the_cut_lands_on_a_real_sale(self):
        """The boundary was landing on $510.50 -- a figure nobody traded at."""
        split = trends.comps_split([v for _, v in self.SALES])
        self.assertIn(split.boundary, [v for _, v in self.SALES])
        self.assertNotEqual(split.boundary, self.BLEND)
