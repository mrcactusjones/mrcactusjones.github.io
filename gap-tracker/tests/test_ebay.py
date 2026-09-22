"""The eBay client, as far as it can be checked without credentials."""
from __future__ import annotations

import base64
import sys
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gapscan.providers.ebay import (SCOPE, USER_FIELDS, basic_auth,
                                    persistable)


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
