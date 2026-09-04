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
        # Flat fee and round numbers so the arithmetic is checkable by hand.
        self.econ = Economics(grading_fee=20.0, sub_ship_per_card=5.0,
                              sale_fee_pct=0.10, ship_out=5.0, raw_premium_pct=0.0,
                              use_fee_tiers=False)
        self.th = Thresholds()

    def test_all_in_and_net(self):
        self.assertAlmostEqual(self.econ.all_in(100.0), 125.0)
        self.assertAlmostEqual(self.econ.net_proceeds(100.0), 85.0)

    def test_raw_premium_raises_cost(self):
        econ = Economics(grading_fee=20.0, sub_ship_per_card=5.0, sale_fee_pct=0.10,
                         ship_out=5.0, raw_premium_pct=0.20, use_fee_tiers=False)
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


class TestErrorSurfacing(unittest.TestCase):
    """A 4xx body explains the problem; losing it costs a debugging round-trip."""

    def test_http_error_body_is_kept(self):
        import io
        import urllib.error
        from unittest import mock
        from gapscan.providers import ppt

        err = urllib.error.HTTPError(
            "http://x", 400, "Bad Request", {},
            io.BytesIO(b'{"error":"Unknown parameter: includePsa"}'))
        provider = ppt.PPTProvider.__new__(ppt.PPTProvider)
        provider.api_key, provider.base = "k", "http://x"
        provider.min_interval, provider._last_call = 0, 0
        provider.search_limit, provider.include_graded = 1, True
        with mock.patch.object(ppt.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(ppt.PPTError) as ctx:
                provider._request("cards", {"search": "x"})
        self.assertEqual(ctx.exception.code, 400)
        self.assertIn("includePsa", str(ctx.exception))

    def test_only_documented_params_are_sent(self):
        """search+limit were proven by discover; includeEbay is what returns PSA."""
        from unittest import mock
        from gapscan.providers import ppt
        provider = ppt.PPTProvider.__new__(ppt.PPTProvider)
        provider.api_key, provider.base = "k", "http://x"
        provider.min_interval, provider._last_call = 0, 0
        provider.search_limit, provider.include_graded = 1, True
        with mock.patch.object(provider, "_request", return_value={}) as req:
            provider.raw_response({"name": "Kingdra", "set_name": "Aquapolis"})
        self.assertEqual(set(req.call_args[0][1]), {"search", "limit", "includeEbay"})
        self.assertEqual(req.call_args[0][1]["search"], "Kingdra Aquapolis")


class TestRealResponseShape(unittest.TestCase):
    """Built from the actual API response, including the wrong-card near miss."""

    KINGDRA_HOLO = {
        "externalCatalogId": "ecard2-H14", "setName": "Aquapolis",
        "cardNumber": "H14/H32", "name": "Kingdra (H14)",
        "prices": {"market": 214.44, "low": 99.2, "sellers": 9, "listings": 11},
        "ebay": {"salesByGrade": {
            "psa10": {"price": 1250.0, "count": 6},
            "psa9": {"price": 430.0, "count": 14},
            "psa8": {"price": 210.0, "count": 3}}},
    }

    def test_extracts_prices_from_sales_by_grade(self):
        q = extract_quote(self.KINGDRA_HOLO)
        self.assertAlmostEqual(q.raw, 214.44)
        self.assertAlmostEqual(q.psa9, 430.0)
        self.assertAlmostEqual(q.psa10, 1250.0)
        self.assertEqual(q.sales_9, 14)
        self.assertEqual(q.sales_10, 6)

    def test_holo_variant_is_not_accepted_for_the_plain_card(self):
        from gapscan.providers.ppt import pick_match
        plain = {"id": "ecard2-19", "name": "Kingdra",
                 "set_name": "Aquapolis", "number": "19"}
        record, why = pick_match([self.KINGDRA_HOLO], plain)
        self.assertIsNone(record, "H14 holo is a different card from #19")
        self.assertIn("no confident match", why)

    def test_external_catalog_id_matches_exactly(self):
        from gapscan.providers.ppt import pick_match
        holo = {"id": "ecard2-H14", "name": "Kingdra",
                "set_name": "Aquapolis", "number": "H14"}
        record, why = pick_match([self.KINGDRA_HOLO], holo)
        self.assertIsNotNone(record)
        self.assertIn("externalCatalogId", why)

    def test_slashed_numbers_compare_on_the_first_part(self):
        from gapscan.providers.ppt import pick_match
        record, why = pick_match(
            [{"setName": "151", "cardNumber": "25/165", "name": "Pikachu"}],
            {"id": "sv3pt5-25", "name": "Pikachu", "set_name": "151", "number": "25"})
        self.assertIsNotNone(record, "25 should match 25/165")

    def test_graded_prices_are_requested(self):
        from gapscan.providers import ppt
        self.assertEqual(ppt.PPTProvider.EXTRA_PARAMS.get("includeEbay"), "true")


class TestDataQualityGating(unittest.TestCase):
    """A price from one old sale must not be presented as a sure thing."""

    def setUp(self):
        # Flat fee and round numbers so the arithmetic is checkable by hand.
        self.econ = Economics(grading_fee=20.0, sub_ship_per_card=5.0,
                              sale_fee_pct=0.10, ship_out=5.0, raw_premium_pct=0.0,
                              use_fee_tiers=False)
        self.th = Thresholds()

    def _good(self, **over):
        from gapscan.store import iso, utcnow
        base = dict(raw=50, psa9=200, psa10=600, sales_9=20, sales_10=10,
                    psa9_last_sale=iso(utcnow()), psa9_confidence="high")
        base.update(over)
        return Quote(**base)

    def test_fresh_deep_comps_are_a_no_brainer(self):
        self.assertEqual(evaluate(self._good(), self.econ, self.th).verdict, "no_brainer")

    def test_stale_comps_lose_confidence(self):
        from datetime import timedelta
        from gapscan.store import iso, utcnow
        old = iso(utcnow() - timedelta(days=120))
        v = evaluate(self._good(psa9_last_sale=old), self.econ, self.th)
        self.assertFalse(v.confident)
        self.assertNotEqual(v.verdict, "no_brainer")
        self.assertTrue(any("120 days ago" in r for r in v.reasons))

    def test_provider_low_confidence_is_respected(self):
        v = evaluate(self._good(psa9_confidence="low"), self.econ, self.th)
        self.assertFalse(v.confident)
        self.assertTrue(any("low-confidence" in r for r in v.reasons))

    def test_outlier_flag_is_respected(self):
        v = evaluate(self._good(psa9_outlier=True), self.econ, self.th)
        self.assertFalse(v.confident)

    def test_missing_last_sale_is_not_treated_as_stale(self):
        v = evaluate(self._good(psa9_last_sale=None), self.econ, self.th)
        self.assertTrue(v.confident, "absent date is unknown, not old")

    def test_days_since_handles_zulu_and_garbage(self):
        from gapscan.econ import days_since
        self.assertIsNone(days_since(None))
        self.assertIsNone(days_since("not-a-date"))
        self.assertGreater(days_since("2020-01-01T00:00:00.000Z"), 1000)


class TestSmartPricePreference(unittest.TestCase):
    def test_smart_market_price_wins_over_average(self):
        from gapscan.providers.ppt import extract_quote
        rec = {"prices": {"market": 10.0}, "ebay": {"salesByGrade": {"psa9": {
            "count": 4, "averagePrice": 999.0, "medianPrice": 500.0,
            "smartMarketPrice": {"price": 120.0, "confidence": "medium"}}}}}
        q = extract_quote(rec)
        self.assertAlmostEqual(q.psa9, 120.0)
        self.assertEqual(q.psa9_confidence, "medium")

    def test_falls_back_to_median_then_average(self):
        from gapscan.providers.ppt import extract_quote
        rec = {"prices": {"market": 10.0}, "ebay": {"salesByGrade": {
            "psa9": {"count": 2, "averagePrice": 999.0, "medianPrice": 500.0},
            "psa10": {"count": 1, "averagePrice": 42.0}}}}
        q = extract_quote(rec)
        self.assertAlmostEqual(q.psa9, 500.0)
        self.assertAlmostEqual(q.psa10, 42.0)

    def test_cgc_is_captured(self):
        from gapscan.providers.ppt import extract_quote
        rec = {"prices": {"market": 10.0}, "ebay": {"salesByGrade": {
            "psa9": {"count": 3, "medianPrice": 100.0},
            "cgc9": {"count": 2, "medianPrice": 80.0},
            "cgc10": {"count": 1, "medianPrice": 210.0}}}}
        q = extract_quote(rec)
        self.assertAlmostEqual(q.cgc9, 80.0)
        self.assertAlmostEqual(q.cgc10, 210.0)
        self.assertEqual(q.cgc9_sales, 2)


class TestCreditCost(unittest.TestCase):
    """Billing is per card RETURNED. Getting this wrong burned a whole day."""

    def test_cost_scales_with_the_limit_not_the_request(self):
        from gapscan.config import ScanBudget
        self.assertEqual(ScanBudget(search_limit=1, include_graded=True).credits_per_call, 2)
        self.assertEqual(ScanBudget(search_limit=1, include_graded=False).credits_per_call, 1)
        # The setting that quietly cost 50 credits a call:
        self.assertEqual(ScanBudget(search_limit=25, include_graded=True).credits_per_call, 50)

    def test_default_limit_is_one(self):
        from gapscan.config import ScanBudget
        b = ScanBudget()
        self.assertEqual(b.search_limit, 1)
        self.assertEqual(b.daily_credits // b.credits_per_call, 50)

    def test_provider_derives_its_own_cost(self):
        from gapscan.providers import ppt
        p = ppt.PPTProvider.__new__(ppt.PPTProvider)
        ppt.PPTProvider.__init__(p, api_key="k", search_limit=5, include_graded=True)
        self.assertEqual(p.credits_per_card, 10)

    def test_credits_from_error_reads_the_429_body(self):
        from gapscan.providers.ppt import credits_from_error
        body = ('{"error":"Daily credit limit exceeded","available":19,'
                '"resetsAt":"2026-08-24T00:00:00.000Z"}')
        msg = credits_from_error(body)
        self.assertIn("19", msg)
        self.assertIn("2026-08-24", msg)
        self.assertIsNone(credits_from_error("not json"))


class TestGradeMix(unittest.TestCase):
    """EV is only as good as the grade mix, so the mix must refuse thin data."""

    def setUp(self):
        from gapscan.econ import mix_from_population, mix_from_sales
        self.from_pop, self.from_sales = mix_from_population, mix_from_sales

    def test_population_mix(self):
        mix = self.from_pop({"grades": {"8": 100, "9": 300, "10": 100}})
        self.assertAlmostEqual(mix.p10, 0.2)
        self.assertAlmostEqual(mix.p9, 0.6)
        self.assertAlmostEqual(mix.p_low, 0.2)
        self.assertEqual(mix.source, "population")
        self.assertEqual(mix.sample, 500)

    def test_sales_mix_needs_a_sample(self):
        # The Kingdra case: one sale per grade. A "33% gem rate" here is noise.
        self.assertIsNone(self.from_sales({"8": 1, "9": 1, "10": 1}, min_sample=5))
        mix = self.from_sales({"8": 4, "9": 10, "10": 6}, min_sample=5)
        self.assertAlmostEqual(mix.p10, 0.3)
        self.assertEqual(mix.source, "sales")

    def test_probabilities_sum_to_one(self):
        for mix in (self.from_pop({"grades": {"7": 5, "9": 3, "10": 2}}),
                    self.from_sales({"6": 5, "9": 3, "10": 2}, min_sample=5)):
            self.assertAlmostEqual(mix.p10 + mix.p9 + mix.p_low, 1.0)

    def test_empty_inputs(self):
        self.assertIsNone(self.from_pop(None))
        self.assertIsNone(self.from_pop({"grades": {}}))
        self.assertIsNone(self.from_sales(None))


class TestExpectedValue(unittest.TestCase):
    def setUp(self):
        # Flat fee and round numbers so the arithmetic is checkable by hand.
        self.econ = Economics(grading_fee=20.0, sub_ship_per_card=5.0,
                              sale_fee_pct=0.10, ship_out=5.0, raw_premium_pct=0.0,
                              use_fee_tiers=False)
        self.th = Thresholds()

    def test_ev_is_absent_without_a_mix(self):
        v = evaluate(Quote(raw=50, psa9=200, psa10=600, sales_9=20, sales_10=10),
                     self.econ, self.th)
        self.assertIsNone(v.ev_profit)
        self.assertIsNone(v.gem_rate)

    def test_ev_is_weighted_by_the_mix(self):
        from gapscan.econ import GradeMix
        # all-in 75. net9 175, net10 535, net(psa8=100) 85.
        quote = Quote(raw=50, psa9=200, psa10=600, psa8=100, sales_9=20, sales_10=10)
        mix = GradeMix(p10=0.25, p9=0.5, p_low=0.25, source="population", sample=400)
        v = evaluate(quote, self.econ, self.th, mix=mix)
        expected = 0.25 * 535 + 0.5 * 175 + 0.25 * 85 - 75
        self.assertAlmostEqual(v.ev_profit, round(expected, 2))
        self.assertAlmostEqual(v.gem_rate, 0.25)
        self.assertEqual(v.mix_source, "population")

    def test_the_floor_verdict_ignores_the_mix(self):
        """The whole point: ranking must not depend on gem-rate guesswork."""
        from gapscan.econ import GradeMix
        quote = Quote(raw=50, psa9=200, psa10=600, sales_9=20, sales_10=10,
                      psa9_confidence="high")
        bare = evaluate(quote, self.econ, self.th)
        withmix = evaluate(quote, self.econ, self.th,
                           mix=GradeMix(0.01, 0.5, 0.49, "sales", 40))
        self.assertEqual(bare.verdict, withmix.verdict)
        self.assertEqual(bare.floor_profit, withmix.floor_profit)


class TestPopulationParsing(unittest.TestCase):
    def test_nested_and_flat_shapes(self):
        from gapscan.providers.ppt import parse_population
        nested = {"data": {"populationByGrader": {"PSA": {
            "grades": {"g8": 50, "g9": 200, "g10": 90}, "total": 340}}}}
        got = parse_population(nested)
        self.assertEqual(got["grades"]["10"], 90)
        self.assertAlmostEqual(got["gem_rate"], 90 / 340)

        flat = {"PSA": {"g9": 10, "g10": 10}}
        self.assertAlmostEqual(parse_population(flat)["gem_rate"], 0.5)

    def test_unusable_shapes_return_none(self):
        from gapscan.providers.ppt import parse_population
        self.assertIsNone(parse_population({}))
        self.assertIsNone(parse_population({"data": {"note": "no population"}}))


class TestSalesMixDownside(unittest.TestCase):
    """Sales under-report low grades; an EV with no downside would mislead."""

    def setUp(self):
        from gapscan.econ import mix_from_sales
        self.mix = mix_from_sales

    def test_absent_low_grades_do_not_mean_zero_risk(self):
        mix = self.mix({"9": 10, "10": 10}, min_sample=5, min_low=0.15)
        self.assertAlmostEqual(mix.p_low, 0.15)
        self.assertAlmostEqual(mix.p10 + mix.p9 + mix.p_low, 1.0)
        self.assertAlmostEqual(mix.p10, 0.425, places=3)

    def test_observed_low_grades_are_left_alone(self):
        mix = self.mix({"7": 5, "8": 5, "9": 5, "10": 5}, min_sample=5, min_low=0.15)
        self.assertAlmostEqual(mix.p_low, 0.5, msg="a measured 50% must not be reduced")

    def test_ratio_between_nine_and_ten_is_preserved(self):
        mix = self.mix({"9": 30, "10": 10}, min_sample=5, min_low=0.20)
        self.assertAlmostEqual(mix.p9 / mix.p10, 3.0)

    def test_population_mixes_are_untouched(self):
        from gapscan.econ import mix_from_population
        mix = mix_from_population({"grades": {"9": 10, "10": 10}})
        self.assertAlmostEqual(mix.p_low, 0.0, msg="a real report needs no assumption")


class TestFeeTiers(unittest.TestCase):
    """PSA paused its Value tiers in June 2026; the floor moved by ~$60 a card."""

    def setUp(self):
        self.econ = Economics()

    def test_cheapest_tier_is_regular(self):
        self.assertAlmostEqual(self.econ.fee_for(50), 79.99)
        self.assertAlmostEqual(self.econ.fee_for(499), 79.99)

    def test_insurance_surcharge_above_the_threshold(self):
        self.assertAlmostEqual(self.econ.fee_for(999), 79.99 + 500 * 0.02)

    def test_higher_declared_value_moves_up_a_tier(self):
        self.assertGreater(self.econ.fee_for(2000), self.econ.fee_for(1400))

    def test_declared_value_defaults_to_raw_when_unknown(self):
        expected = (100 * (1 + self.econ.raw_premium_pct)
                    + self.econ.fee_for(100) + self.econ.sub_ship_per_card)
        self.assertAlmostEqual(self.econ.all_in(100), expected)

    def test_slabbed_value_drives_the_fee_not_the_raw_price(self):
        # A $60 raw card that grades to $1,800 is not a $79.99 submission.
        cheap_raw = self.econ.all_in(60, declared_value=1800)
        by_raw = self.econ.all_in(60, declared_value=60)
        self.assertGreater(cheap_raw, by_raw + 90)

    def test_flat_fee_mode_ignores_tiers(self):
        flat = Economics(grading_fee=19.0, use_fee_tiers=False)
        self.assertAlmostEqual(flat.fee_for(5000), 19.0)

    def test_verdicts_reflect_the_new_cost(self):
        """A card that cleared at the old bulk rate can fail at the real one."""
        quote = Quote(raw=35.19, psa9=149.25, psa10=976.89, sales_9=12, sales_10=4,
                      psa9_confidence="high")
        cheap = evaluate(quote, Economics(grading_fee=19.0, use_fee_tiers=False),
                         Thresholds())
        real = evaluate(quote, Economics(), Thresholds())
        self.assertGreater(cheap.floor_profit, 50)
        self.assertLess(real.floor_profit, 10)
        self.assertEqual(real.verdict, "floor_positive")
