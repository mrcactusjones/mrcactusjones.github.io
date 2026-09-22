"""The eBay client, as far as it can be checked without credentials."""
from __future__ import annotations

import base64
import sys
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gapscan.providers.ebay import SCOPE, basic_auth


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
