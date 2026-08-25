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
