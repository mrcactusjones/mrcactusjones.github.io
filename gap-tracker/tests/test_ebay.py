"""The eBay client, as far as it can be checked without credentials."""
from __future__ import annotations

import base64
import sys
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gapscan.providers.ebay import (SCOPE, USER_FIELDS, basic_auth,
                                    is_same_card, parse_title,
                                    delivered, persistable, search_text,
                                    spread_of)


class BasicAuthTest(unittest.TestCase):
    """eBay answered `invalid_client` to well-formed production credentials,
    which makes "are we encoding them correctly" a question worth settling."""

    def test_it_matches_the_rfc_7617_worked_example(self):
        # RFC 7617 section 2: "Aladdin" / "open sesame".
        self.assertEqual(basic_auth("Aladdin", "open sesame"),
                         "Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ==")

    def test_the_separator_is_the_first_colon(self):
        """A secret containing a colon must not shift the split."""
        header = basic_auth("app-PRD-1", "PRD-a:b:c")
        decoded = base64.b64decode(header.split()[1]).decode()
        self.assertEqual(decoded.split(":", 1), ["app-PRD-1", "PRD-a:b:c"])

    def test_it_is_not_url_encoded(self):
        """Basic auth takes the raw values; encoding them is a real way to
        produce exactly the error we are chasing."""
        header = basic_auth("a b", "c+d")
        self.assertEqual(base64.b64decode(header.split()[1]).decode(), "a b:c+d")

    def test_no_newline_survives_into_the_header(self):
        """b64encode of a long value used to wrap; a header with a newline in
        it is rejected before it reaches eBay."""
        header = basic_auth("x" * 200, "y" * 200)
        self.assertNotIn("\n", header)


class ScopeTest(unittest.TestCase):
    def test_the_scope_is_form_encoded_not_sent_raw(self):
        """The token body must carry the scope percent-encoded."""
        body = urllib.parse.urlencode(
            {"grant_type": "client_credentials", "scope": SCOPE})
        self.assertIn("scope=https%3A%2F%2Fapi.ebay.com%2Foauth%2Fapi_scope", body)
        self.assertIn("grant_type=client_credentials", body)


class NoUserDataOnDiskTest(unittest.TestCase):
    """eBay's Marketplace Account Deletion exemption is for applications that
    do not store eBay users' data. This tool claims that exemption, so the
    claim has to stay true as the listing code grows."""

    ROW = {"title": "Rayquaza ex delta PSA 9", "price": 840.0, "currency": "USD",
           "shipping": 0.0, "condition": "Used", "seller": "somebody_1997",
           "feedback": "99.8", "buying": ["FIXED_PRICE"],
           "url": "https://www.ebay.com/itm/1", "image": "https://x/y.jpg"}

    def test_the_seller_is_dropped_before_anything_is_stored(self):
        kept = persistable(self.ROW)
        self.assertNotIn("seller", kept)
        self.assertNotIn("feedback", kept)

    def test_the_card_fields_all_survive(self):
        kept = persistable(self.ROW)
        for field in ("title", "price", "currency", "shipping", "condition",
                      "buying", "url", "image"):
            self.assertIn(field, kept, field)

    def test_no_value_in_a_persisted_row_carries_the_username(self):
        """A field added later that happens to embed the seller would defeat
        the filter without failing the checks above."""
        blob = repr(persistable(self.ROW))
        self.assertNotIn("somebody_1997", blob)

    def test_user_fields_are_declared_not_guessed(self):
        self.assertEqual(set(USER_FIELDS), {"seller", "feedback"})


class ParseTitleTest(unittest.TestCase):
    """Reading the printing back out of a listing title.

    PPT matches graded sales from these same titles and keeps only the grade,
    which is how two printings end up inside one PSA 9 figure. The titles below
    are real, from a live Browse response.
    """

    def _p(self, title, condition="Graded"):
        return parse_title(title, condition)

    def test_the_real_titles_from_the_first_live_search(self):
        cases = [
            ("1999 Base Set Holo Charizard #4 PSA 9", "PSA", 9.0, ["Holo"]),
            ("1999 POKEMON BASE SET UNLIMITED #4 CHARIZARD-HOLO PSA 9",
             "PSA", 9.0, ["Unlimited", "Holo"]),
            ("1999 POKEMON GAME BASE SET UNLIMITED (ENGLISH) #4 CHARIZARD "
             "HOLO MINT PSA 9 !", "PSA", 9.0, ["Unlimited", "Holo"]),
        ]
        for title, grader, grade, printings in cases:
            got = self._p(title)
            self.assertEqual((got["grader"], got["grade"]), (grader, grade), title)
            self.assertEqual(got["printings"], printings, title)

    def test_it_reads_graders_other_than_psa(self):
        self.assertEqual(self._p("Jolteon Skyridge BGS 9.5")["grade"], 9.5)
        # No space is common and must not defeat it.
        self.assertEqual(self._p("Umbreon Neo Discovery CGC9")["grader"], "CGC")

    def test_first_edition_and_shadowless_are_both_kept(self):
        """One card can be both, and 1st Edition is what moves the price --
        reporting only the first match would lose half the story."""
        got = self._p("Charizard 4/102 1st Edition Shadowless PSA 10")
        self.assertEqual(got["printings"], ["1st Edition", "Shadowless"])
        self.assertEqual(got["grade"], 10.0)

    def test_reverse_holo_does_not_also_report_holo(self):
        self.assertEqual(self._p("Rayquaza Reverse Holo NM", "Used")["printings"],
                         ["Reverse Holo"])

    def test_a_raw_card_is_not_graded(self):
        got = self._p("Charizard Base Set Unlimited 4/102 - raw", "Used")
        self.assertFalse(got["graded"])
        self.assertIsNone(got["grader"])

    def test_a_slab_with_no_grade_in_the_title_is_still_graded(self):
        """eBay's condition field settles this better than the title does, and
        'graded but unreadable' is worth telling apart from 'raw'."""
        got = self._p("Blastoise Base Set - graded slab, grade in photos")
        self.assertTrue(got["graded"])
        self.assertIsNone(got["grade"])

    def test_1st_ed_abbreviations(self):
        for text in ("1st Ed", "1st-Edition", "First Edition", "1ST ED"):
            self.assertIn("1st Edition", self._p(f"Charizard {text} PSA 9")["printings"],
                          text)

    def test_a_bare_number_is_not_mistaken_for_a_grade(self):
        """Card numbers sit next to names constantly; only a grader prefix
        makes a number a grade."""
        got = self._p("Charizard 4/102 Base Set", "Used")
        self.assertIsNone(got["grade"])

    def test_empty_and_missing_titles_do_not_raise(self):
        for title in ("", None):
            got = parse_title(title)
            self.assertEqual(got["printings"], [])
            self.assertFalse(got["graded"])


class WrongCardTest(unittest.TestCase):
    """eBay keyword search is generous, and the generosity was being ranked.

    A live query for Rayquaza ex delta returned a $20,250 PSA 9 -- thirteen
    times PPT's figure -- and listings marked 1st Edition for a 2006 set that
    never had one. Grouped by printing, those produced a confident and false
    "the printings ask within 1.3x of each other".
    """

    def check(self, title):
        return is_same_card(title, "97/101", "Dragon Frontiers")

    def test_the_card_itself_matches(self):
        self.assertTrue(self.check(
            "Rayquaza ex \u03b4 (Delta Species) EX Dragon Frontiers 97/101 2006"))
        self.assertTrue(self.check("Rayquaza ex #97 Dragon Frontiers PSA 9"))

    def test_a_different_card_entirely_does_not(self):
        self.assertFalse(self.check("1999 Base Set #4 Charizard PSA 9"))

    def test_the_gold_star_that_polluted_the_group(self):
        """107/107, a genuinely different and genuinely $20k card."""
        self.assertFalse(self.check("Rayquaza Gold Star 107/107 EX Deoxys PSA 9"))

    def test_a_year_is_not_a_card_number(self):
        """The trap a substring test falls into: 97 inside 1997."""
        self.assertFalse(self.check("1997 Rayquaza Dragon Frontiers promo"))

    def test_a_bare_number_needs_the_set_to_corroborate_it(self):
        self.assertTrue(self.check("Rayquaza ex 97 Dragon Frontiers holo"))
        self.assertFalse(self.check("Rayquaza ex 97 Japanese Miracle Crystal"))

    def test_no_number_means_no_opinion(self):
        self.assertTrue(is_same_card("anything at all", None))


class SearchTextTest(unittest.TestCase):
    def test_non_ascii_is_dropped_from_the_query(self):
        """"Rayquaza ex delta" percent-encodes into the URL and returns one
        raw match; without it the card is found."""
        self.assertEqual(search_text("Rayquaza ex \u03b4", "Dragon Frontiers", "97/101"),
                         "Rayquaza ex Dragon Frontiers 97")

    def test_the_number_loses_its_set_total(self):
        self.assertTrue(search_text("Jolteon", "Skyridge", "H12/H32").endswith("H12"))


class SpreadTest(unittest.TestCase):
    def test_it_catches_the_group_that_was_not_one_population(self):
        self.assertGreater(spread_of([2006, 4478, 6500, 20250]), 3.0)

    def test_a_real_printing_stays_tight(self):
        self.assertLess(spread_of([1450, 1536, 1610, 1720]), 3.0)

    def test_one_value_has_no_spread(self):
        self.assertIsNone(spread_of([1500]))


class DeliveredCostTest(unittest.TestCase):
    """Unknown postage must not be quietly priced at zero.

    A listing with no shipping cost crashed the display, which was the small
    half of the bug. The large half was `total()` folding None in as 0 --
    turning "we do not know" into the cheapest possible answer, on the exact
    number that decides whether a card is worth buying.
    """

    def test_a_quoted_cost_is_added(self):
        self.assertEqual(delivered({"price": 199.50, "shipping": 10.69}),
                         (210.19, True))

    def test_free_postage_is_known_and_zero(self):
        self.assertEqual(delivered({"price": 199.50, "shipping": 0.0}),
                         (199.50, True))

    def test_missing_postage_is_flagged_not_assumed_free(self):
        value, known = delivered({"price": 199.50, "shipping": None})
        self.assertFalse(known)
        self.assertEqual(value, 199.50)

    def test_it_does_not_raise_on_a_listing_with_no_price(self):
        self.assertEqual(delivered({"price": None, "shipping": None}), (0.0, False))

    def test_an_unpriced_post_never_undercuts_a_known_one(self):
        """The ordering property that matters: a listing hiding its postage
        must not sort ahead of one that quotes it."""
        cheap_unknown = delivered({"price": 200.0, "shipping": None})
        dearer_known = delivered({"price": 205.0, "shipping": 5.0})
        self.assertLess(cheap_unknown[0], dearer_known[0])
        self.assertFalse(cheap_unknown[1])   # so the caller must handle it
