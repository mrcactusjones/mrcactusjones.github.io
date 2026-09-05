"""Paging and rate limits -- the two ways this adapter quietly wastes money.

Billing is per card *requested*, so a page size the server will not honour is
paid for and never delivered; and a per-minute 429 that reads as fatal costs a
whole set for want of a two-second wait. Both were live bugs.
"""
from __future__ import annotations

import io
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gapscan.providers import ppt
from gapscan.providers.ppt import PAGE_MAX, PPTError, PPTProvider, RateLimited


def _http_error(code: int, body: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://x", code, "boom", {}, io.BytesIO(body.encode()))


def _provider(**kw) -> PPTProvider:
    kw.setdefault("api_key", "test-key")
    kw.setdefault("min_interval", 0.0)
    return PPTProvider(**kw)


class PageSizeTest(unittest.TestCase):
    def test_batch_never_asks_for_more_than_the_server_returns(self):
        provider = _provider()
        with mock.patch.object(provider, "_request", return_value={"data": []}) as req:
            _, cost = provider.fetch_batch("Aquapolis", limit=100)
        self.assertEqual(req.call_args.args[1]["limit"], PAGE_MAX)
        # Billed on the clamped limit, not the one asked for.
        self.assertEqual(cost, PAGE_MAX * 3)

    def test_smaller_limits_pass_through(self):
        provider = _provider()
        with mock.patch.object(provider, "_request", return_value={"data": []}) as req:
            _, cost = provider.fetch_batch("Aquapolis", limit=10)
        self.assertEqual(req.call_args.args[1]["limit"], 10)
        self.assertEqual(cost, 30)


class RateLimitTest(unittest.TestCase):
    def test_minute_limit_waits_and_retries(self):
        provider = _provider()
        answers = [RateLimited(3.0, "minute"), {"data": [{"id": "x"}]}]
        with mock.patch.object(ppt.time, "sleep") as slept, \
                mock.patch.object(provider, "_request_once", side_effect=answers):
            blob = provider._request("cards", {})
        self.assertEqual(blob["data"][0]["id"], "x")
        slept.assert_called_once_with(3.0)

    def test_minute_limit_gives_up_after_repeated_failures(self):
        provider = _provider()
        with mock.patch.object(ppt.time, "sleep"), \
                mock.patch.object(provider, "_request_once",
                                  side_effect=RateLimited(2.0, "minute")):
            with self.assertRaises(RateLimited):
                provider._request("cards", {})

    def test_minute_429_is_classified_apart_from_the_daily_one(self):
        provider = _provider()
        body = '{"error":"Minute rate limit exceeded","retryAfter":2}'
        with mock.patch.object(ppt.urllib.request, "urlopen",
                               side_effect=_http_error(429, body)):
            with self.assertRaises(RateLimited) as caught:
                provider._request_once("cards", {})
        # One second of headroom over what the server asks for.
        self.assertEqual(caught.exception.retry_after, 3.0)

    def test_daily_limit_is_not_retried(self):
        provider = _provider()
        body = '{"error":"Daily credit limit exceeded","creditsRemaining":0}'
        with mock.patch.object(ppt.urllib.request, "urlopen",
                               side_effect=_http_error(429, body)):
            with self.assertRaises(PPTError) as caught:
                provider._request_once("cards", {})
        self.assertEqual(caught.exception.code, 429)

    def test_unparseable_minute_body_still_waits(self):
        provider = _provider()
        with mock.patch.object(ppt.urllib.request, "urlopen",
                               side_effect=_http_error(429, "minute limit, sorry")):
            with self.assertRaises(RateLimited) as caught:
                provider._request_once("cards", {})
        self.assertEqual(caught.exception.retry_after, 5.0)


if __name__ == "__main__":
    unittest.main()
