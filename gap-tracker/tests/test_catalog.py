"""Catalog build must survive a flaky set instead of losing the whole run."""
from __future__ import annotations

import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gapscan import catalog
from gapscan.config import Thresholds


def _http_error(code: int, headers: dict | None = None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x", code, "boom", headers or {}, None)


def _card(set_id: str, num: int, price: float = 50.0) -> dict:
    return {"id": f"{set_id}-{num}", "name": f"Card{num}", "number": str(num),
            "rarity": "Rare Holo", "set": {"id": set_id, "name": set_id.title()},
            "images": {"small": ""},
            "tcgplayer": {"prices": {"holofoil": {"market": price}}}}


SEEDS = {
    "sets": [{"id": "good1", "priority": 5}, {"id": "flaky", "priority": 5},
             {"id": "good2", "priority": 5}],
    "rarities": ["Rare Holo"],
    "cards": [],
}


class TestFetchResilience(unittest.TestCase):
    def test_client_error_is_not_retried(self):
        with mock.patch.object(catalog, "_get", side_effect=_http_error(404)) as get:
            with self.assertRaises(catalog.SetFetchError):
                catalog.fetch_set("nosuchset", retries=4)
        self.assertEqual(get.call_count, 1, "a 404 means a bad set id; don't retry it")

    def test_server_error_is_retried_then_reported(self):
        with mock.patch.object(catalog, "_get", side_effect=_http_error(500)) as get, \
             mock.patch.object(catalog.time, "sleep"):
            with self.assertRaises(catalog.SetFetchError) as ctx:
                catalog.fetch_set("base5", retries=4)
        self.assertEqual(get.call_count, 4)
        self.assertIn("500", str(ctx.exception))

    def test_retry_after_header_is_honoured_without_burning_an_attempt(self):
        responses = [_http_error(429, {"Retry-After": "5"}),
                     {"data": [_card("base5", 1)]}]
        def side_effect(*_a, **_k):
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        with mock.patch.object(catalog, "_get", side_effect=side_effect), \
             mock.patch.object(catalog.time, "sleep") as sleep:
            cards = catalog.fetch_set("base5", retries=4)
        self.assertEqual(len(cards), 1)
        self.assertIn(5, [c.args[0] for c in sleep.call_args_list],
                      "should wait exactly what Retry-After asked for")

    def test_absurd_retry_after_is_capped(self):
        with mock.patch.object(catalog, "_get",
                               side_effect=_http_error(429, {"Retry-After": "9999"})), \
             mock.patch.object(catalog.time, "sleep") as sleep:
            with self.assertRaises(catalog.SetFetchError):
                catalog.fetch_set("base5", retries=2)
        self.assertTrue(all(c.args[0] <= 60 for c in sleep.call_args_list))

    def test_retry_succeeds_after_a_transient_failure(self):
        responses = [_http_error(500), {"data": [_card("base5", 1)]}]
        def side_effect(*_a, **_k):
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        with mock.patch.object(catalog, "_get", side_effect=side_effect), \
             mock.patch.object(catalog.time, "sleep"):
            cards = catalog.fetch_set("base5", retries=4)
        self.assertEqual(len(cards), 1)


class TestBuildSkipsFailures(unittest.TestCase):
    def test_one_bad_set_does_not_sink_the_build(self):
        def fake_fetch(set_id, retries=4):
            if set_id == "flaky":
                raise catalog.SetFetchError("HTTP 500 Internal Server Error")
            return [_card(set_id, 1), _card(set_id, 2)]

        with mock.patch.object(catalog, "fetch_set", side_effect=fake_fetch):
            universe, meta = catalog.build(SEEDS, Thresholds())

        self.assertEqual(len(universe), 4, "both healthy sets should be present")
        self.assertIn("flaky", meta["failed_sets"])
        self.assertNotIn("good1", meta["failed_sets"])
        self.assertTrue(all(e["source"] == "api" for e in universe.values()))

    def test_only_filter_limits_the_build(self):
        with mock.patch.object(catalog, "fetch_set",
                               side_effect=lambda s, retries=4: [_card(s, 1)]) as fetch:
            universe, meta = catalog.build(SEEDS, Thresholds(), only={"good2"})
        self.assertEqual([c[0][0] for c in fetch.call_args_list], ["good2"])
        self.assertEqual(len(universe), 1)
        self.assertEqual(meta["sets_requested"], 1)

    def test_price_band_filters_out_of_range_cards(self):
        th = Thresholds(raw_price_min=10.0, raw_price_max=100.0)
        def fake_fetch(set_id, retries=4):
            return [_card(set_id, 1, price=5.0), _card(set_id, 2, price=50.0),
                    _card(set_id, 3, price=5000.0)]
        with mock.patch.object(catalog, "fetch_set", side_effect=fake_fetch):
            universe, meta = catalog.build(SEEDS, th)
        self.assertTrue(all(10.0 <= e["raw_hint"] <= 100.0 for e in universe.values()))
        self.assertEqual(meta["skipped"]["price_band"], 6)


if __name__ == "__main__":
    unittest.main()


class TestCreditAccounting(unittest.TestCase):
    """Cost is billed on the requested limit, so it is knowable in advance."""

    def _provider(self, **kw):
        from gapscan.providers import ppt
        p = ppt.PPTProvider.__new__(ppt.PPTProvider)
        ppt.PPTProvider.__init__(p, api_key="k", **kw)
        return p

    def test_next_cost_matches_the_limit(self):
        self.assertEqual(self._provider(search_limit=1).next_cost(), 2)
        self.assertEqual(self._provider(search_limit=5).next_cost(), 10)
        self.assertEqual(self._provider(search_limit=5, include_graded=False).next_cost(), 5)

    def test_a_miss_costs_the_same_as_a_hit(self):
        provider = self._provider(search_limit=1, wide_limit=0)
        with mock.patch.object(provider, "raw_response", return_value={"data": []}):
            self.assertIsNone(provider.fetch(
                {"id": "base1-4", "name": "Charizard", "set_name": "Base Set", "number": "4"}))
        self.assertEqual(provider.credits_used, 2, "an empty result is still billed")

    def test_widening_is_tried_once_when_the_narrow_hit_is_wrong(self):
        provider = self._provider(search_limit=1, wide_limit=5)
        want = {"id": "ecard2-19", "name": "Kingdra",
                "set_name": "Aquapolis", "number": "19"}
        wrong = {"setName": "Aquapolis", "cardNumber": "H14/H32", "name": "Kingdra (H14)",
                 "externalCatalogId": "ecard2-H14",
                 "prices": {"market": 1.0},
                 "ebay": {"salesByGrade": {"psa9": {"count": 1, "medianPrice": 9}}}}
        right = dict(wrong, cardNumber="019/147", externalCatalogId="ecard2-19",
                     name="Kingdra (19)")
        pages = [{"data": [wrong]}, {"data": [wrong, right]}]
        with mock.patch.object(provider, "raw_response", side_effect=lambda *a, **k: pages.pop(0)):
            quote = provider.fetch(want)
        self.assertIsNotNone(quote, "widening should recover the right printing")
        self.assertEqual(provider.credits_used, 12, "2 for the narrow try, 10 for the wide")

    def test_set_filter_is_sent_when_known(self):
        provider = self._provider(search_limit=1, wide_limit=0)
        with mock.patch.object(provider, "_request", return_value={"data": []}) as req:
            provider.fetch({"id": "x", "name": "Kingdra",
                            "set_name": "Aquapolis", "number": "19"})
        params = req.call_args[0][1]
        self.assertEqual(params["set"], "Aquapolis")
        self.assertEqual(params["search"], "Kingdra")
        self.assertEqual(params["limit"], 1)


class TestBatchParameters(unittest.TestCase):
    """The API rejects unknown parameters outright, so only send documented ones."""

    def _provider(self):
        from gapscan.providers import ppt
        p = ppt.PPTProvider.__new__(ppt.PPTProvider)
        ppt.PPTProvider.__init__(p, api_key="k", search_limit=1)
        return p

    def test_pagination_is_by_offset_not_page(self):
        provider = self._provider()
        with mock.patch.object(provider, "_request", return_value={"data": []}) as req:
            provider.fetch_batch("Base Set", limit=50, offset=100)
        params = req.call_args[0][1]
        self.assertEqual(params["offset"], 100)
        self.assertNotIn("page", params, "the API 400s on an unknown parameter")

    def test_price_band_is_pushed_to_the_server(self):
        provider = self._provider()
        with mock.patch.object(provider, "_request", return_value={"data": []}) as req:
            provider.fetch_batch("Base Set", min_price=8, max_price=400)
        params = req.call_args[0][1]
        self.assertEqual((params["minPrice"], params["maxPrice"]), (8, 400))

    def test_price_band_is_omitted_when_not_wanted(self):
        provider = self._provider()
        with mock.patch.object(provider, "_request", return_value={"data": []}) as req:
            provider.fetch_batch("Base Set")
        self.assertNotIn("minPrice", req.call_args[0][1])

    def test_only_documented_parameters_are_sent(self):
        allowed = {"tcgPlayerId", "cardId", "setId", "setName", "set", "search",
                   "rarity", "cardType", "artist", "minPrice", "maxPrice", "sortBy",
                   "sortOrder", "limit", "offset", "includeHistory", "includeEbay",
                   "includeBoth", "days", "limitDays", "fetchAllInSet", "language",
                   "lightweight", "printing", "condition", "maxDataPoints",
                   "includeCardmarket"}
        provider = self._provider()
        with mock.patch.object(provider, "_request", return_value={"data": []}) as req:
            provider.fetch_batch("Base Set", min_price=8, max_price=400)
        self.assertTrue(set(req.call_args[0][1]) <= allowed,
                        set(req.call_args[0][1]) - allowed)

    def test_cost_is_still_the_requested_limit(self):
        provider = self._provider()
        with mock.patch.object(provider, "_request", return_value={"data": []}):
            _, cost = provider.fetch_batch("Base Set", limit=20)
        self.assertEqual(cost, 60)
        self.assertEqual(provider.credits_used, 60)


class TestUniverseMerge(unittest.TestCase):
    """A rebuild that deletes cards it didn't build is silent and expensive."""

    def setUp(self):
        self.existing = {
            "base1-4":  {"id": "base1-4", "set_id": "base1", "source": "api"},
            "base2-1":  {"id": "base2-1", "set_id": "base2", "source": "api"},
            "swept-1":  {"id": "swept-1", "set_id": "sweep", "source": "sweep"},
            "manual-1": {"id": "manual-1", "set_id": "manual", "source": "manual"},
            "fix-1":    {"id": "fix-1", "set_id": "base1", "source": "fixture"},
        }

    def test_swept_cards_survive_a_rebuild(self):
        merged = catalog.merge_universe(
            self.existing, {"base1-9": {"id": "base1-9", "set_id": "base1", "source": "api"}},
            "api")
        self.assertIn("swept-1", merged, "credits were spent finding this card")
        self.assertIn("manual-1", merged, "the user chose this card by hand")

    def test_only_rebuilt_sets_are_replaced(self):
        merged = catalog.merge_universe(
            self.existing, {"base1-9": {"id": "base1-9", "set_id": "base1", "source": "api"}},
            "api")
        self.assertNotIn("base1-4", merged, "base1 was rebuilt")
        self.assertIn("base2-1", merged, "base2 was not touched")
        self.assertIn("base1-9", merged)

    def test_fixture_and_live_data_never_mix(self):
        live = catalog.merge_universe(
            self.existing, {"base2-9": {"id": "base2-9", "set_id": "base2", "source": "api"}},
            "api")
        self.assertNotIn("fix-1", live, "demo cards must not join a live ranking")

        fixture = catalog.merge_universe(
            self.existing, {"f-9": {"id": "f-9", "set_id": "base2", "source": "fixture"}},
            "fixture")
        self.assertNotIn("base1-4", fixture)
        self.assertIn("swept-1", fixture, "non-catalog sources survive either way")

    def test_an_empty_build_changes_nothing_but_the_other_source(self):
        merged = catalog.merge_universe(self.existing, {}, "api")
        self.assertEqual(set(merged), {"base1-4", "base2-1", "swept-1", "manual-1"})


class TestBudgetGuardReservesTheRetry(unittest.TestCase):
    """A miss widens the search, so the guard must reserve that cost too."""

    def test_a_card_is_not_started_if_the_widen_cannot_be_afforded(self):
        from gapscan import scan as scan_mod
        from gapscan.config import Config
        from gapscan.providers import ppt

        provider = ppt.PPTProvider.__new__(ppt.PPTProvider)
        ppt.PPTProvider.__init__(provider, api_key="k", search_limit=1, wide_limit=5)
        # Narrow lookup costs 2, a widened retry 10: 12 for the worst case.
        self.assertEqual(provider.next_cost(), 2)

        cfg = Config()
        cfg.budget.daily_credits = 8          # enough for the narrow try, not the widen
        universe = {"a": {"id": "a", "name": "A", "set_name": "S", "number": "1",
                          "tier": "candidate", "priority": 1}}

        class Store:
            cards = history = None
            def load_quote(self, _):
                return None
            def save_quote(self, *a):
                pass
        with mock.patch.object(provider, "fetch") as fetch:
            result = scan_mod.run(universe, Store(), cfg, provider)
        fetch.assert_not_called()
        self.assertEqual(result["scanned"], 0)

    def test_a_card_is_started_when_the_widen_fits(self):
        from gapscan import scan as scan_mod
        from gapscan.config import Config
        from gapscan.providers import ppt

        provider = ppt.PPTProvider.__new__(ppt.PPTProvider)
        ppt.PPTProvider.__init__(provider, api_key="k", search_limit=1, wide_limit=5)
        cfg = Config()
        cfg.budget.daily_credits = 20
        universe = {"a": {"id": "a", "name": "A", "set_name": "S", "number": "1",
                          "tier": "candidate", "priority": 1}}

        saved = {}
        class Store:
            cards = history = None
            def load_quote(self, _):
                return None
            def save_quote(self, cid, rec):
                saved[cid] = rec
        with mock.patch.object(provider, "fetch", return_value=None):
            result = scan_mod.run(universe, Store(), cfg, provider)
        self.assertEqual(result["scanned"], 1)


class TestPopulationHandlesMisses(unittest.TestCase):
    def test_a_cached_miss_does_not_crash_the_lookup(self):
        """scan.py stores a miss as {"quote": None}; .get("quote", {}) is None."""
        records = [("a", {"id": "a", "quote": None, "miss": True}),
                   ("b", {"id": "b", "quote": {"tcgplayer_id": "123"}, "miss": False})]
        priced = [(cid, rec) for cid, rec in records
                  if (rec.get("quote") or {}).get("tcgplayer_id")]
        self.assertEqual([cid for cid, _ in priced], ["b"])


class TestSetMultipleOutlier(unittest.TestCase):
    """A card carrying another card's comps shows up as a multiple far out of
    step with its own set."""

    def setUp(self):
        from gapscan.config import Thresholds
        from gapscan.econ import Quote
        from gapscan.rank import _multiple_reasons, _set_multiples
        self.multiples, self.reasons = _set_multiples, _multiple_reasons
        self.Quote, self.Thresholds = Quote, Thresholds

    def _set(self, multiples, set_name="Aquapolis"):
        return [({"set_name": set_name, "id": f"c{i}"},
                 self.Quote(raw=100.0, psa9=100.0 * m))
                for i, m in enumerate(multiples)]

    def test_median_needs_enough_cards_to_mean_anything(self):
        self.assertEqual(self.multiples(self._set([3, 3, 3]), min_sample=8), {})
        self.assertIn("Aquapolis", self.multiples(self._set([3] * 8), min_sample=8))

    def test_a_wild_multiple_is_flagged_against_its_own_set(self):
        priced = self._set([3, 3, 3, 3, 3, 3, 3, 3])
        medians = self.multiples(priced, min_sample=8)
        th = self.Thresholds(set_multiple_factor=4.0)
        # 3x is typical here, so 40x is carrying someone else's sales.
        wild = ({"set_name": "Aquapolis"}, self.Quote(raw=10.0, psa9=400.0))
        self.assertTrue(self.reasons(wild[0], wild[1], medians, th))
        self.assertFalse(self.reasons(priced[0][0], priced[0][1], medians, th))

    def test_sets_are_judged_against_themselves(self):
        """A 20x set and a 3x set must not police each other."""
        priced = self._set([3] * 8) + self._set([20] * 8, set_name="Skyridge")
        medians = self.multiples(priced, min_sample=8)
        th = self.Thresholds(set_multiple_factor=4.0)
        card = ({"set_name": "Skyridge"}, self.Quote(raw=100.0, psa9=2000.0))
        self.assertFalse(self.reasons(card[0], card[1], medians, th),
                         "20x is normal in this set")

    def test_a_set_with_no_median_flags_nothing(self):
        th = self.Thresholds()
        self.assertEqual(
            self.reasons({"set_name": "Unknown"}, self.Quote(raw=1.0, psa9=999.0), {}, th),
            [])


class SweepTargetTest(unittest.TestCase):
    """One set, one query.

    PPT's set names are not the seed list's -- "Delta Species" and "EX Delta
    Species" are one set -- and a full sweep paid for seven such pairs twice,
    18% of the run. Targets are keyed on PPT's own set id so the aliases
    cannot come apart again.
    """

    SEEDS = [{"name": "Delta Species", "priority": 8},
             {"name": "Expedition", "priority": 8}]

    @staticmethod
    def _entry(set_name, set_id=None, ppt_name=None, priority=0):
        entry = {"set_name": set_name, "priority": priority}
        if set_id:
            entry["ppt_set_id"] = set_id
            entry["ppt_set_name"] = ppt_name or set_name
        return entry

    def test_two_names_for_one_set_become_one_target(self):
        universe = {
            "a": self._entry("Delta Species", "1450", "EX Delta Species", 8),
            "b": self._entry("EX Delta Species", "1450", "EX Delta Species"),
        }
        targets = catalog.sweep_targets(universe, [])
        self.assertEqual([(t.set_id, t.set_name) for t in targets], [("1450", None)])

    def test_a_pinned_set_is_never_also_swept_by_name(self):
        universe = {"a": self._entry("Aquapolis", "1397", priority=10)}
        targets = catalog.sweep_targets(universe, [])
        self.assertEqual(len(targets), 1)
        self.assertIsNone(targets[0].set_name, "the name query is the fuzzy one")

    def test_an_unfetched_set_still_falls_back_to_its_name(self):
        universe = {"a": self._entry("Shining Fates", priority=3)}
        targets = catalog.sweep_targets(universe, [])
        self.assertEqual([(t.set_id, t.set_name) for t in targets],
                         [(None, "Shining Fates")])

    def test_a_seeded_set_is_swept_even_with_no_cards_for_it(self):
        """Expedition was seeded, catalogued nothing, and silently never swept."""
        targets = catalog.sweep_targets({}, self.SEEDS)
        self.assertIn("Expedition", [t.set_name for t in targets])

    def test_a_seed_does_not_re_add_a_set_already_pinned(self):
        universe = {"a": self._entry("Delta Species", "1450", "EX Delta Species", 8)}
        targets = catalog.sweep_targets(universe, self.SEEDS)
        self.assertEqual(sorted(t.label for t in targets),
                         ["EX Delta Species", "Expedition"])

    def test_the_filter_accepts_either_spelling(self):
        universe = {"a": self._entry("Delta Species", "1450", "EX Delta Species", 8)}
        for spelling in ("Delta Species", "ex delta species"):
            targets = catalog.sweep_targets(universe, [], [spelling])
            self.assertEqual([t.set_id for t in targets], ["1450"], spelling)

    def test_priority_leads_and_ties_are_stable(self):
        universe = {
            "a": self._entry("Zzz", priority=1),
            "b": self._entry("Aquapolis", "1397", priority=10),
            "c": self._entry("Aaa", priority=1),
        }
        targets = catalog.sweep_targets(universe, [])
        self.assertEqual([t.label for t in targets], ["Aquapolis", "Aaa", "Zzz"])

    def test_a_set_takes_the_highest_priority_among_its_cards(self):
        universe = {
            "a": self._entry("Delta Species", "1450", "EX Delta Species", 0),
            "b": self._entry("Delta Species", "1450", "EX Delta Species", 100),
        }
        self.assertEqual(catalog.sweep_targets(universe, [])[0].priority, 100)

    def test_a_pinned_set_keeps_the_priority_claimed_under_its_other_names(self):
        """Cards a sweep discovers sit at priority 0 and are the ones that
        carry the id. Without folding in the names, pinning a set silently
        demoted it to the back of the crawl."""
        universe = {
            "seeded": self._entry("Base Set", priority=10),      # catalogued, unpinned
            "swept": self._entry("Base Set", "1", "Base", 0),    # discovered, pinned
        }
        targets = catalog.sweep_targets(universe, [])
        self.assertEqual([(t.set_id, t.priority) for t in targets], [("1", 10)])

    def test_a_seed_priority_reaches_a_set_that_is_already_pinned(self):
        universe = {"a": self._entry("Delta Species", "1450", "EX Delta Species", 0)}
        seeds = [{"name": "Delta Species", "priority": 8}]
        self.assertEqual(catalog.sweep_targets(universe, seeds)[0].priority, 8)
