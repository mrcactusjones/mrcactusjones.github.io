"""Offline tests for disctracker.ebay. A fake eBay sits behind httpx.MockTransport, waiting goes
through an injected sleeper and the token clock is a plain variable, so nothing here touches the
network or really sleeps."""
from __future__ import annotations

import base64
import email.utils
import json
import traceback
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from disctracker import db, ebay
from disctracker.ebay import (EBAY_STORE, EbayAuthError, EbayClient, EbayError, EbayQuotaError,
                              SearchResult)

FIXTURES = Path(__file__).parent / "fixtures"
DATA = Path(ebay.__file__).parent / "data"
ID = "TestApp-PRD-1a2b3c4d5e6f"
SECRET = "PRD-9f8e7d6c5b4a-hunter2secret"
BASIC = base64.b64encode(f"{ID}:{SECRET}".encode()).decode()
BASE_ID = 100_000_000_000
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
ALL_TABLES = ("stores", "runs", "listings", "variants", "observations", "ebay_queries", "sales")


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def pid(i: int) -> int:
    return BASE_ID + i


# --- a fake eBay -------------------------------------------------------------------------------

MISSING = object()


def summary(i: int, **over) -> dict:
    """A Browse API itemSummary; pass key=MISSING to drop a field."""
    s = {
        "itemId": f"v1|{pid(i)}|0", "title": f"Innova Star Destroyer {i}",
        "price": {"value": "24.99", "currency": "USD"}, "condition": "New", "conditionId": "1000",
        "itemWebUrl": f"https://www.ebay.com/itm/{pid(i)}?hash=item{i}&amdata=enc%3AAQ",
        "buyingOptions": ["FIXED_PRICE"], "seller": {"username": "seller"},
    }
    s.update(over)
    return {k: v for k, v in s.items() if v is not MISSING}


class Virtual:
    """`total` results made on demand; only the first `available` can actually be paged."""

    def __init__(self, total, make=summary, available=None):
        self.total, self.make = total, make
        self.available = total if available is None else available


def status(code: int, retry_after=None, body=None):
    """A scripted response step (a callable, so every use builds a fresh Response)."""
    headers = {} if retry_after is None else {"Retry-After": str(retry_after)}
    return lambda request: httpx.Response(code, headers=headers, json=body if body is not None else {})


class Ebay:
    """Fake eBay token + search endpoints that record every request.

    results: query text -> list of itemSummaries (or Virtual); a query not listed has no matches.
    token_script / search_script: the n-th (0-based) request of that kind is answered by
    script[n] - an Exception (raised), a callable(request) or an httpx.Response; None or a
    missing entry falls through to normal behaviour. The real API's limits are enforced:
    offset + limit may not exceed 10,000 and the bearer token must be one we issued.
    """

    def __init__(self, results=None, token_script=(), search_script=(), expires_in=7200, rejected=None):
        self.results = dict(results or {})
        self.rejected = dict(rejected or {})  # query text -> HTTP status eBay always answers it with
        self.token_script, self.search_script = list(token_script), list(search_script)
        self.expires_in = expires_in
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []
        self.now = 0.0
        self.issued: set[str] = set()
        self.http = httpx.Client(transport=httpx.MockTransport(self._handle))

    @property
    def token_requests(self):
        return [r for r in self.requests if r.url.path == "/identity/v1/oauth2/token"]

    @property
    def search_requests(self):
        return [r for r in self.requests if r.url.path == "/buy/browse/v1/item_summary/search"]

    @property
    def queries_searched(self):
        return [r.url.params["q"] for r in self.search_requests]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/identity/v1/oauth2/token":
            kind, script, default = "token", self.token_script, self._token
        elif request.url.path == "/buy/browse/v1/item_summary/search":
            kind, script, default = "search", self.search_script, self._search
        else:
            return httpx.Response(404)
        self.requests.append(request)
        n = sum(1 for r in self.requests if r.url.path == request.url.path) - 1
        step = script[n] if n < len(script) else None
        if isinstance(step, Exception):
            raise step
        if callable(step):
            return step(request)
        return step if step is not None else default(request)

    def _token(self, request):
        token = f"tok-{len(self.token_requests)}"
        self.issued.add(token)
        body = {"access_token": token, "token_type": "Application Access Token"}
        if self.expires_in is not None:
            body["expires_in"] = self.expires_in
        return httpx.Response(200, json=body)

    def _search(self, request):
        p = request.url.params
        if request.headers.get("authorization", "").removeprefix("Bearer ") not in self.issued:
            return httpx.Response(401, json={"errors": [{"errorId": 1001, "message": "Invalid access token"}]})
        if p["q"] in self.rejected:
            return httpx.Response(self.rejected[p["q"]], json={"errors": [{"message": "query not accepted"}]})
        limit, offset = int(p["limit"]), int(p["offset"])
        if offset + limit > 10_000:
            return httpx.Response(400, json={"errors": [{"errorId": 12023, "message": "offset too large"}]})
        spec = self.results.get(p["q"], [])
        if not isinstance(spec, Virtual):
            spec = Virtual(len(spec), spec.__getitem__)
        page = [spec.make(i) for i in range(offset, min(offset + limit, spec.available))]
        body = {"href": str(request.url), "total": spec.total, "limit": limit, "offset": offset}
        if page:
            body["itemSummaries"] = page
        return httpx.Response(200, json=body)


def make_client(e: Ebay, **kw) -> EbayClient:
    client = EbayClient(ID, SECRET, e.http, sleeper=e.sleeps.append, **kw)
    client._clock = lambda: e.now
    return client


def search(e: Ebay, query="q", key="k", max_calls=1000, **kw) -> SearchResult:
    return make_client(e, **kw).search(query, key, max_calls)


def offsets(e: Ebay) -> list[int]:
    return [int(r.url.params["offset"]) for r in e.search_requests]


# --- credentials ----------------------------------------------------------------------------------

def test_load_credentials_reads_both_variables():
    assert ebay.load_credentials({"EBAY_CLIENT_ID": "id", "EBAY_CLIENT_SECRET": "secret"}) == ("id", "secret")


def test_load_credentials_defaults_to_the_process_environment(monkeypatch):
    monkeypatch.setenv("EBAY_CLIENT_ID", "from-env")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "env-secret")
    assert ebay.load_credentials() == ("from-env", "env-secret")
    monkeypatch.delenv("EBAY_CLIENT_SECRET")
    assert ebay.load_credentials() is None


@pytest.mark.parametrize("env", [
    {}, {"EBAY_CLIENT_ID": "id"}, {"EBAY_CLIENT_SECRET": "secret"},
    {"EBAY_CLIENT_ID": "", "EBAY_CLIENT_SECRET": "secret"},
    {"EBAY_CLIENT_ID": "id", "EBAY_CLIENT_SECRET": ""},
    {"EBAY_CLIENT_ID": "   ", "EBAY_CLIENT_SECRET": "secret"},
    {"EBAY_CLIENT_ID": "id", "EBAY_CLIENT_SECRET": " \n\t"},
    {"EBAY_CLIENT_ID": None, "EBAY_CLIENT_SECRET": "secret"},
])
def test_load_credentials_missing_or_blank_is_none(env):
    assert ebay.load_credentials(env) is None


def test_load_credentials_strips_surrounding_whitespace():
    env = {"EBAY_CLIENT_ID": "  id\n", "EBAY_CLIENT_SECRET": "secret \n"}
    assert ebay.load_credentials(env) == ("id", "secret")


def test_client_refuses_empty_credentials():
    with pytest.raises(EbayAuthError):
        EbayClient("", "secret")
    with pytest.raises(EbayAuthError):
        EbayClient("id", "")


def test_client_owns_and_closes_its_own_http_client():
    with EbayClient(ID, SECRET) as client:
        assert not client._http.is_closed
        http = client._http
    assert http.is_closed
    shared = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    EbayClient(ID, SECRET, shared).close()
    assert not shared.is_closed  # an injected client is the caller's to close


# --- token ----------------------------------------------------------------------------------------

def test_token_request_shape():
    e = Ebay()
    search(e)
    assert len(e.token_requests) == 1
    req = e.token_requests[0]
    assert req.method == "POST" and str(req.url) == TOKEN_URL
    assert req.headers["authorization"] == f"Basic {BASIC}"
    assert req.headers["content-type"].startswith("application/x-www-form-urlencoded")
    assert urllib.parse.parse_qs(req.content.decode()) == {
        "grant_type": ["client_credentials"], "scope": ["https://api.ebay.com/oauth/api_scope"]}
    assert "disc-tracker" in req.headers["user-agent"]


def test_a_real_shaped_token_response_is_used_as_the_bearer_token():
    body = fixture("ebay_token.json")
    e = Ebay({"q": [summary(1)]}, token_script=[httpx.Response(200, json=body)])
    e.issued.add(body["access_token"])
    result = search(e)
    assert e.search_requests[0].headers["authorization"] == f"Bearer {body['access_token']}"
    assert len(result.items) == 1 and body["expires_in"] == 7200


def test_token_is_cached_until_a_minute_before_expiry():
    e = Ebay({"q": [summary(1)]})
    client = make_client(e)
    client.search("q", "k", 10)
    e.now = 7199 - 60 - 1  # 7138 s: still inside the cached lifetime (expires_in 7200 - 60 margin)
    client.search("q", "k", 10)
    assert len(e.token_requests) == 1
    assert [r.headers["authorization"] for r in e.search_requests] == ["Bearer tok-1"] * 2
    e.now = 7140  # exactly 60 s before expiry: refreshed
    client.search("q", "k", 10)
    assert len(e.token_requests) == 2
    assert e.search_requests[-1].headers["authorization"] == "Bearer tok-2"
    client.search("q", "k", 10)  # and the new one is cached again
    assert len(e.token_requests) == 2


def test_short_lived_token_is_still_cached_for_half_its_life():
    e = Ebay(expires_in=100)
    client = make_client(e)
    client.search("q", "k", 10)
    e.now = 49
    client.search("q", "k", 10)
    assert len(e.token_requests) == 1
    e.now = 51
    client.search("q", "k", 10)
    assert len(e.token_requests) == 2


def test_token_without_expires_in_gets_a_short_default_lifetime():
    e = Ebay(expires_in=None)
    client = make_client(e)
    client.search("q", "k", 10)
    e.now = 239
    client.search("q", "k", 10)
    assert len(e.token_requests) == 1
    e.now = 241
    client.search("q", "k", 10)
    assert len(e.token_requests) == 2


def test_401_on_search_refreshes_the_token_once_and_retries():
    e = Ebay({"q": [summary(1)]}, search_script=[status(401)])
    result = search(e)
    assert len(e.token_requests) == 2
    assert [r.headers["authorization"] for r in e.search_requests] == ["Bearer tok-1", "Bearer tok-2"]
    assert len(result.items) == 1 and result.complete and result.calls == 2


def test_second_401_raises_auth_error_without_looping():
    e = Ebay({"q": [summary(1)]}, search_script=[status(401), status(401)])
    with pytest.raises(EbayAuthError) as info:
        search(e)
    assert len(e.token_requests) == 2 and len(e.search_requests) == 2
    assert info.value.calls == 2


def test_each_page_gets_its_own_refresh_when_the_token_dies_mid_query():
    e = Ebay({"q": Virtual(450)}, search_script=[None, status(401)])
    result = search(e)
    assert len(e.token_requests) == 2 and result.complete and len(result.items) == 450


@pytest.mark.parametrize("code", [400, 401])
def test_token_endpoint_rejection_is_an_auth_error_and_nothing_is_searched(code):
    e = Ebay(token_script=[httpx.Response(code, json=fixture("ebay_token_error.json"))])
    with pytest.raises(EbayAuthError) as info:
        search(e)
    assert e.search_requests == []
    message = str(info.value)
    assert str(code) in message and "client authentication failed" in message


def test_other_token_endpoint_refusals_are_auth_errors_too():
    e = Ebay(token_script=[status(403)])
    with pytest.raises(EbayAuthError):
        search(e)
    assert len(e.token_requests) == 1 and e.search_requests == []


@pytest.mark.parametrize("body", [{}, {"expires_in": 7200}, {"access_token": "  "}, {"access_token": 5},
                                  ["not", "an", "object"]])
def test_token_response_without_a_token_is_an_auth_error(body):
    e = Ebay(token_script=[httpx.Response(200, json=body)])
    with pytest.raises(EbayAuthError):
        search(e)
    assert e.search_requests == []


def test_token_response_that_is_not_json_is_an_auth_error():
    e = Ebay(token_script=[httpx.Response(200, content=b"<html>maintenance</html>")])
    with pytest.raises(EbayAuthError):
        search(e)


def test_token_endpoint_outage_is_retried_then_a_plain_error_not_an_auth_error():
    e = Ebay(token_script=[status(503)] * 4)
    with pytest.raises(EbayError) as info:
        search(e)
    assert not isinstance(info.value, (EbayAuthError, EbayQuotaError))
    assert len(e.token_requests) == 4 and e.sleeps == [1.0, 2.0, 4.0]
    assert info.value.calls == 0  # token requests are not search calls
    assert e.search_requests == []


def test_token_endpoint_recovers_after_a_5xx():
    e = Ebay({"q": [summary(1)]}, token_script=[status(502)])
    assert len(search(e).items) == 1
    assert e.sleeps == [1.0] and len(e.token_requests) == 2


def test_token_endpoint_429_twice_is_a_quota_error():
    e = Ebay(token_script=[status(429, 1), status(429, 1)])
    with pytest.raises(EbayQuotaError):
        search(e)


# --- credentials never leak ---------------------------------------------------------------------------

def echo(code, token="tok-1"):
    """A server that helpfully repeats everything it was sent in its error text."""
    text = f"bad {SECRET} for {ID} / Basic {BASIC} / Bearer {token} / {token}"
    return lambda request: httpx.Response(code, json={"error": "x", "error_description": text,
                                                       "errors": [{"message": text}]})


LEAK_SCENARIOS = {
    "token 401 echoes credentials": dict(token_script=[echo(401, "no-token-yet")]),
    "token 400 echoes credentials": dict(token_script=[echo(400, "no-token-yet")]),
    "search 400 echoes the token": dict(search_script=[echo(400)]),
    "search 500s echo the token": dict(search_script=[echo(500)] * 4),
    "search 429s echo the token": dict(search_script=[echo(429), echo(429)]),
    "search 401s echo the token": dict(search_script=[echo(401), echo(401, "tok-2")]),
    "transport errors mention the secret": dict(search_script=[httpx.ConnectError(f"proxy said {SECRET}")] * 4),
    "unreadable search body": dict(search_script=[httpx.Response(200, content=f"{SECRET} tok-1".encode())]),
}


def assert_clean(text: str):
    for secret in (ID, SECRET, BASIC, "tok-1", "tok-2"):
        assert secret not in text, f"{secret!r} leaked into {text!r}"


@pytest.mark.parametrize("scenario", sorted(LEAK_SCENARIOS))
def test_credentials_and_tokens_never_appear_in_exceptions(scenario):
    e = Ebay({"q": [summary(1)]}, **LEAK_SCENARIOS[scenario])
    client = make_client(e)
    with pytest.raises(EbayError) as info:
        client.search("q", "k", 100)
    exc = info.value
    for text in (str(exc), repr(exc), "".join(traceback.format_exception(exc)), repr(client), str(client)):
        assert_clean(text)


@pytest.mark.parametrize("scenario", ["token 401 echoes credentials", "token 400 echoes credentials",
                                      "search 400 echoes the token"])
def test_server_text_that_repeats_credentials_is_redacted_not_dropped(scenario):
    e = Ebay({"q": [summary(1)]}, **LEAK_SCENARIOS[scenario])
    with pytest.raises(EbayError) as info:
        make_client(e).search("q", "k", 100)
    assert "***" in str(info.value)  # the useful part of the message survives, the secrets do not
    assert_clean(str(info.value))


@pytest.mark.parametrize("scenario", sorted(LEAK_SCENARIOS))
def test_credentials_and_tokens_never_appear_in_collect_logs_or_the_runs_row(scenario):
    conn = db.connect(":memory:")
    e = Ebay({"q": [summary(1)], "q2": [summary(2)]}, **LEAK_SCENARIOS[scenario])
    logs: list[str] = []
    try:
        ebay.collect(conn, make_client(e), "2026-01-01", queries=[("a", "q"), ("b", "q2")], log=logs.append)
    except EbayAuthError as exc:
        assert_clean(str(exc))
    for line in logs:
        assert_clean(line)
    for row in conn.execute("SELECT error FROM runs"):
        assert_clean(row[0] or "")
    assert_clean("\n".join(conn.iterdump()))


# --- search request -----------------------------------------------------------------------------------

def test_search_request_shape():
    e = Ebay({"star destroyer innova": [summary(1)]})
    search(e, "star destroyer innova", "innova|destroyer|star")
    req = e.search_requests[0]
    assert req.method == "GET" and str(req.url).startswith(SEARCH_URL + "?")
    assert dict(req.url.params) == {
        "q": "star destroyer innova", "category_ids": "184356", "limit": "200", "offset": "0",
        "filter": "buyingOptions:{FIXED_PRICE}"}  # asking prices only, no auctions
    assert req.headers["authorization"] == "Bearer tok-1"
    assert req.headers["x-ebay-c-marketplace-id"] == "EBAY_US"
    assert "fieldgroups" not in req.url.params


def test_every_page_asks_for_fixed_price_only():
    e = Ebay({"q": Virtual(450)})
    search(e)
    assert {r.url.params["filter"] for r in e.search_requests} == {"buyingOptions:{FIXED_PRICE}"}


def test_marketplace_categories_and_base_url_are_configurable():
    e = Ebay({"q": [summary(1)]})
    client = EbayClient(ID, SECRET, e.http, marketplace_id="EBAY_GB", category_ids=("184356", "123"),
                        base_url="https://api.sandbox.ebay.com/", sleeper=e.sleeps.append)
    client.search("q", "k", 5)
    req = e.search_requests[0]
    assert req.url.params["category_ids"] == "184356,123"
    assert req.headers["x-ebay-c-marketplace-id"] == "EBAY_GB"
    assert e.token_requests[0].url.host == req.url.host == "api.sandbox.ebay.com"  # trailing slash tolerated


def test_category_ids_given_as_ints_are_accepted():
    e = Ebay({"q": [summary(1)]})
    client = EbayClient(ID, SECRET, e.http, category_ids=(184356, 99), sleeper=e.sleeps.append)
    assert client.search("q", "k", 5).complete
    assert e.search_requests[0].url.params["category_ids"] == "184356,99"


def test_empty_category_list_omits_the_parameter_and_a_bare_string_is_one_category():
    e = Ebay()
    search(e, category_ids=())
    assert "category_ids" not in e.search_requests[0].url.params
    e = Ebay()
    search(e, category_ids="184356")
    assert e.search_requests[0].url.params["category_ids"] == "184356"


# --- pagination ---------------------------------------------------------------------------------------

def test_empty_result_without_item_summaries_is_valid_and_complete():
    e = Ebay(search_script=[httpx.Response(200, json=fixture("ebay_search_empty.json"))])
    result = search(e)
    assert "itemSummaries" not in fixture("ebay_search_empty.json")
    assert (result.items, result.total, result.complete, result.calls) == ([], 0, True, 1)


def test_empty_result_without_a_total_either_is_a_complete_empty_result():
    e = Ebay(search_script=[httpx.Response(200, json={"href": "x", "limit": 200, "offset": 0})])
    result = search(e)
    assert (result.items, result.total, result.complete) == ([], 0, True)


def test_pagination_walks_200_at_a_time_until_total():
    e = Ebay({"q": Virtual(450)})
    result = search(e, key="innova|destroyer|star")
    assert offsets(e) == [0, 200, 400]
    assert {r.url.params["limit"] for r in e.search_requests} == {"200"}
    assert (result.total, result.complete, result.calls) == (450, True, 3)
    assert [p.product_id for p in result.items] == [pid(i) for i in range(450)]
    assert {p.query_key for p in result.items} == {"innova|destroyer|star"}


@pytest.mark.parametrize("total,calls", [(1, 1), (199, 1), (200, 1), (201, 2), (400, 2), (401, 3)])
def test_pagination_stops_exactly_at_total(total, calls):
    e = Ebay({"q": Virtual(total)})
    result = search(e)
    assert len(e.search_requests) == calls and result.calls == calls
    assert len(result.items) == total and result.complete


def test_total_beyond_the_10000_window_stops_at_the_cap_and_is_incomplete():
    e = Ebay({"q": Virtual(12_000)})
    result = search(e)
    assert offsets(e) == list(range(0, 10_000, 200))  # 50 pages; offset 10,000 is never requested
    assert max(offsets(e)) + 200 == 10_000
    assert len(result.items) == 10_000 and result.total == 12_000
    assert result.complete is False and "window" in result.error and result.calls == 50


def test_exactly_10000_results_are_complete():
    e = Ebay({"q": Virtual(10_000)})
    result = search(e)
    assert result.calls == 50 and len(result.items) == 10_000 and result.complete


def test_call_budget_exhausted_mid_query_is_incomplete_with_what_was_fetched():
    e = Ebay({"q": Virtual(1000)})
    result = search(e, max_calls=2)
    assert offsets(e) == [0, 200]
    assert (len(result.items), result.total, result.complete, result.calls) == (400, 1000, False, 2)
    assert "budget" in result.error


def test_call_budget_that_just_suffices_is_complete():
    e = Ebay({"q": Virtual(450)})
    result = search(e, max_calls=3)
    assert result.complete and result.calls == 3


def test_no_budget_at_all_raises_without_a_request():
    e = Ebay({"q": [summary(1)]})
    with pytest.raises(EbayError) as info:
        search(e, max_calls=0)
    assert e.search_requests == [] and info.value.calls == 0


def test_budget_running_out_during_retries_of_the_first_page_raises():
    e = Ebay(search_script=[status(500)] * 4)
    with pytest.raises(EbayError) as info:
        search(e, max_calls=2)
    assert len(e.search_requests) == 2 and info.value.calls == 2
    assert e.sleeps == [1.0]  # no pointless wait before an attempt that cannot be made


def test_pages_that_run_dry_before_total_stop_and_stay_incomplete():
    e = Ebay({"q": Virtual(600, available=250)})
    result = search(e)
    assert offsets(e) == [0, 200, 400]  # the third page is empty: stop, do not loop
    assert len(result.items) == 250 and not result.complete and "250 of 600" in result.error


def test_items_shifting_between_pages_are_not_double_counted():
    page1 = [summary(i) for i in range(200)]
    page2 = [summary(199), summary(200)]  # 199 slid down a page when a new listing appeared
    body = lambda page: (lambda request: httpx.Response(  # noqa: E731
        200, json={"total": 201, "limit": 200, "offset": 0, "itemSummaries": page}))
    e = Ebay(search_script=[body(page1), body(page2)])
    result = search(e)
    assert len(result.items) == 201 and result.complete and result.skipped_variations == 0


def test_a_page_that_only_repeats_items_cannot_fake_completeness():
    page1 = [summary(i) for i in range(200)]
    repeat = [summary(0), summary(1)]
    body = lambda page: (lambda request: httpx.Response(  # noqa: E731
        200, json={"total": 202, "limit": 200, "offset": 0, "itemSummaries": page}))
    e = Ebay(search_script=[body(page1), body(repeat), body([])])
    result = search(e)
    assert len(result.items) == 200 and not result.complete


# --- retries, rate limits, failures ------------------------------------------------------------------------

def test_429_waits_retry_after_and_retries_once():
    e = Ebay({"q": [summary(1)]}, search_script=[status(429, retry_after=3)])
    result = search(e)
    assert e.sleeps == [3.0] and result.calls == 2 and len(result.items) == 1


def test_second_429_is_a_quota_error():
    e = Ebay({"q": [summary(1)]}, search_script=[status(429, 3), status(429, 3)])
    with pytest.raises(EbayQuotaError) as info:
        search(e)
    assert e.sleeps == [3.0] and len(e.search_requests) == 2 and info.value.calls == 2


def test_429_with_a_huge_retry_after_is_not_waited_for():
    e = Ebay({"q": [summary(1)]}, search_script=[status(429, retry_after=86_400)])
    with pytest.raises(EbayQuotaError) as info:
        search(e)
    assert e.sleeps == [] and len(e.search_requests) == 1 and info.value.calls == 1


def test_retry_after_zero_retries_immediately():
    e = Ebay({"q": [summary(1)]}, search_script=[status(429, retry_after=0), status(503, retry_after=0)])
    result = search(e)
    assert e.sleeps == [] and result.calls == 3 and len(result.items) == 1


def test_429_without_retry_after_uses_a_default_wait():
    e = Ebay({"q": [summary(1)]}, search_script=[status(429)])
    search(e)
    assert e.sleeps == [ebay.RATE_LIMIT_WAIT]


def test_429_retry_after_as_http_date():
    when = datetime.now(timezone.utc) + timedelta(seconds=30)
    stamp = email.utils.format_datetime(when, usegmt=True)
    e = Ebay({"q": [summary(1)]}, search_script=[status(429, retry_after=stamp)])
    search(e)
    assert len(e.sleeps) == 1 and 0 < e.sleeps[0] <= 30


def test_quota_mid_query_discards_nothing_that_was_recorded_before():
    e = Ebay({"q": Virtual(450)}, search_script=[None, status(429, 1), status(429, 1)])
    with pytest.raises(EbayQuotaError) as info:
        search(e)
    assert info.value.calls == 3


def test_5xx_is_retried_with_exponential_backoff_then_succeeds():
    e = Ebay({"q": [summary(1)]}, search_script=[status(500), status(502)])
    result = search(e)
    assert e.sleeps == [1.0, 2.0] and result.calls == 3 and len(result.items) == 1


def test_5xx_exhaustion_raises_a_plain_error_after_three_retries():
    e = Ebay({"q": [summary(1)]}, search_script=[status(503)] * 4)
    with pytest.raises(EbayError) as info:
        search(e)
    assert not isinstance(info.value, (EbayAuthError, EbayQuotaError))
    assert e.sleeps == [1.0, 2.0, 4.0] and len(e.search_requests) == 4 and info.value.calls == 4
    assert "after 3 retries" in str(info.value)


def test_503_retry_after_is_honoured_but_a_huge_one_fails_fast():
    e = Ebay({"q": [summary(1)]}, search_script=[status(503, retry_after=7)])
    search(e)
    assert e.sleeps == [7.0]
    e = Ebay({"q": [summary(1)]}, search_script=[status(503, retry_after=99_999)])
    with pytest.raises(EbayError):
        search(e)
    assert e.sleeps == [] and len(e.search_requests) == 1


def test_timeouts_and_network_errors_are_retried():
    e = Ebay({"q": [summary(1)]}, search_script=[
        httpx.ReadTimeout("slow"), httpx.ConnectError("down"), httpx.RemoteProtocolError("cut")])
    result = search(e)
    assert e.sleeps == [1.0, 2.0, 4.0] and len(result.items) == 1


def test_408_is_retried():
    e = Ebay({"q": [summary(1)]}, search_script=[status(408)])
    assert len(search(e).items) == 1 and e.sleeps == [1.0]


@pytest.mark.parametrize("code", [400, 403, 404, 301])
def test_other_statuses_fail_the_query_without_retrying(code):
    e = Ebay({"q": [summary(1)]}, search_script=[status(code, body=fixture("ebay_rate_limit.json"))])
    with pytest.raises(EbayError) as info:
        search(e)
    assert not isinstance(info.value, (EbayAuthError, EbayQuotaError))
    assert len(e.search_requests) == 1 and e.sleeps == [] and str(code) in str(info.value)


def test_failure_after_the_first_page_returns_an_incomplete_result():
    e = Ebay({"q": Virtual(450)}, search_script=[None] + [status(500)] * 4)
    result = search(e)
    assert len(result.items) == 200 and result.total == 450 and not result.complete
    assert result.calls == 5 and "after 3 retries" in result.error
    assert e.sleeps == [1.0, 2.0, 4.0]


def test_a_bad_page_after_the_first_is_incomplete_but_a_bad_first_page_raises():
    junk = httpx.Response(200, content=b"not json")
    e = Ebay({"q": Virtual(450)}, search_script=[None, junk])
    result = search(e)
    assert len(result.items) == 200 and not result.complete and "unusable" in result.error
    e = Ebay({"q": Virtual(450)}, search_script=[junk])
    with pytest.raises(EbayError):
        search(e)


@pytest.mark.parametrize("body", [
    httpx.Response(200, content=b"<html>"), httpx.Response(200, content=b""),
    httpx.Response(200, json=[1, 2]), httpx.Response(200, json={"total": 1, "itemSummaries": {}}),
    httpx.Response(200, json={"itemSummaries": [{"itemId": "v1|1|0"}]}),  # items but no total
    httpx.Response(200, json={"total": -1}), httpx.Response(200, json={"total": "many"}),
    httpx.Response(200, json={"total": True}), httpx.Response(200, json={"total": 1.5}),
])
def test_unusable_first_page_raises_error(body):
    e = Ebay(search_script=[body])
    with pytest.raises(EbayError):
        search(e)


def test_total_as_digit_string_is_accepted():
    body = {"total": "1", "itemSummaries": [summary(1)]}
    e = Ebay(search_script=[httpx.Response(200, json=body)])
    assert search(e).complete


# --- item mapping -----------------------------------------------------------------------------------------------

def one(**over):
    """Run a search that returns the single summary summary(1, **over); returns the SearchResult."""
    e = Ebay({"q": [summary(1, **over)]})
    return search(e)


def test_fixture_page_maps_to_expected_listings():
    e = Ebay(search_script=[httpx.Response(200, json=fixture("ebay_search.json"))])
    result = search(e, key="innova|destroyer|star")
    assert result.complete and result.total == 7 and result.calls == 1
    assert (result.skipped_currency, result.skipped_variations, result.skipped_other) == (1, 1, 1)
    by_id = {p.product_id: p for p in result.items}
    assert list(by_id) == [110439278622, 225512340987, 334455667788, 667788990011]

    new = by_id[110439278622]
    assert new.title == "NEW Innova Star Destroyer 175g Disc Golf Driver Max Distance!!"
    assert new.url == "https://www.ebay.com/itm/110439278622"
    assert new.tags == ["condition:new"] and new.product_type == "New" and new.vendor == ""
    assert new.ends_at == "2026-11-04T18:21:33.000Z" and new.query_key == "innova|destroyer|star"
    assert [(v.variant_id, v.price_cents, v.available) for v in new.variants] == [(110439278622, 1899, True)]

    used = by_id[225512340987]
    assert used.tags == ["condition:used"] and used.product_type == "Pre-Owned"
    assert used.variants[0].price_cents == 1250

    multi = by_id[334455667788]  # two variations: one listing, the cheaper price
    assert multi.variants[0].price_cents == 1949 and multi.variants[0].variant_id == 334455667788

    bare = by_id[667788990011]  # no condition at all: no tag
    assert bare.tags == [] and bare.product_type == "" and bare.ends_at == ""


def test_product_fields():
    p = one().items[0]
    assert p.product_id == pid(1) and p.handle == str(pid(1))
    assert p.vendor == "" and p.query_key == "k"
    assert len(p.variants) == 1
    v = p.variants[0]
    assert (v.variant_id, v.price_cents, v.compare_at_cents, v.available) == (pid(1), 2499, None, True)


@pytest.mark.parametrize("value,cents", [
    ("24.99", 2499), ("0.10", 10), ("0.01", 1), ("1.15", 115), ("4.35", 435), ("0.29", 29),
    ("19.995", 2000), ("19.994", 1999), ("100", 10000), ("7.5", 750), (" 7.50 ", 750),
    ("1234567.89", 123456789), ("9999999.99", 999999999),
])
def test_price_to_cents_is_exact(value, cents):
    result = one(price={"value": value, "currency": "USD"})
    assert result.items[0].variants[0].price_cents == cents


def test_json_number_prices_stay_exact_decimals():
    # 4.35 * 100 is 434.99999999999994 as a float; eBay sends strings, but numbers must not drift.
    body = json.dumps({"total": 2, "itemSummaries": [
        summary(1, price={"value": 4.35, "currency": "USD"}),
        summary(2, price={"value": 25, "currency": "USD"})]})
    e = Ebay(search_script=[httpx.Response(200, content=body.encode())])
    assert [p.variants[0].price_cents for p in search(e).items] == [435, 2500]


@pytest.mark.parametrize("value", ["", "abc", "-5.00", "0", "0.00", "NaN", "Infinity", "1e5", "1,234.56",
                                   "$5.00", "99999999.00", "5.1234567", None, True, [], {}, 0, -3])
def test_unusable_prices_are_skipped_and_counted(value):
    result = one(price={"value": value, "currency": "USD"})
    assert result.items == [] and result.skipped_other == 1 and result.skipped_currency == 0
    assert result.complete  # the item was fetched, we just cannot use it


@pytest.mark.parametrize("price", [MISSING, None, "24.99", [], {"value": "5.00"}, {"value": "5.00", "currency": ""},
                                   {"value": "5.00", "currency": None}])
def test_missing_price_or_currency_is_skipped(price):
    result = one(price=price)
    assert result.items == [] and result.skipped_other == 1


@pytest.mark.parametrize("currency", ["EUR", "GBP", "CAD", "AUD"])
def test_non_store_currency_is_skipped_and_counted(currency):
    result = one(price={"value": "20.00", "currency": currency})
    assert result.items == [] and result.skipped_currency == 1 and result.skipped_other == 0


def test_currency_check_is_case_insensitive_and_counts_only_foreign_items():
    e = Ebay({"q": [summary(1, price={"value": "5.00", "currency": "usd"}),
                    summary(2, price={"value": "5.00", "currency": "EUR"}),
                    summary(3, price={"value": "5.00", "currency": "EUR"}), summary(4)]})
    result = search(e)
    assert [p.product_id for p in result.items] == [pid(1), pid(4)] and result.skipped_currency == 2


@pytest.mark.parametrize("item_id", [
    "abc", "v1|abc|0", "v1|123", "123", "", None, 12345, ["v1|1|0"], "v2|1|0", "v1|0|0", "V1|1|0",
    "v1|99999999999999999999|0", "v1|1|x", "v1|1|0|9", " v1|1|0", "v1|1|0\n",
])
def test_unreadable_item_ids_are_skipped(item_id):
    result = one(itemId=item_id)
    assert result.items == [] and result.skipped_other == 1


def test_item_without_an_id_field_is_skipped_and_counts_as_fetched():
    e = Ebay({"q": [summary(1, itemId=MISSING), summary(2)]})
    result = search(e)
    assert [p.product_id for p in result.items] == [pid(2)] and result.skipped_other == 1 and result.complete


def test_non_object_entries_are_skipped():
    body = {"total": 3, "itemSummaries": ["junk", None, summary(1)]}
    e = Ebay(search_script=[httpx.Response(200, json=body)])
    result = search(e)
    assert len(result.items) == 1 and result.skipped_other == 2 and result.complete


@pytest.mark.parametrize("item_id", ["v1|123456789012|0", "v1|123456789012|98765"])
def test_item_id_becomes_legacy_product_and_variant_id(item_id):
    p = one(itemId=item_id).items[0]
    assert p.product_id == 123456789012 and p.variants[0].variant_id == 123456789012


@pytest.mark.parametrize("order", [(1, 2), (2, 1)])
def test_variations_of_one_item_make_one_listing_with_the_cheapest_price(order):
    variations = {1: summary(1, itemId="v1|555|9001", price={"value": "21.00", "currency": "USD"}),
                  2: summary(2, itemId="v1|555|9002", price={"value": "19.49", "currency": "USD"})}
    e = Ebay({"q": [variations[i] for i in order]})
    result = search(e)
    assert len(result.items) == 1 and result.skipped_variations == 1 and result.complete
    p = result.items[0]
    assert (p.product_id, p.variants[0].variant_id, p.variants[0].price_cents) == (555, 555, 1949)


def test_equal_priced_variations_pick_deterministically():
    mk = lambda var: summary(1, itemId=f"v1|555|{var}", title=f"variation {var}")  # noqa: E731
    first = search(Ebay({"q": [mk(2), mk(1)]})).items[0].title
    second = search(Ebay({"q": [mk(1), mk(2)]})).items[0].title
    assert first == second == "variation 1"


def test_the_same_item_id_twice_is_a_repeat_not_a_variation():
    e = Ebay({"q": [summary(1), summary(1)]})
    result = search(e)
    assert len(result.items) == 1 and result.skipped_variations == 0


@pytest.mark.parametrize("raw,expected", [
    ("https://www.ebay.com/itm/110439278622?hash=item19b7a6a39e:g:abc&amdata=enc%3AAQ",
     "https://www.ebay.com/itm/110439278622"),
    ("http://www.ebay.com/itm/1?x=1#frag", "http://www.ebay.com/itm/1"),
    ("HTTPS://WWW.EBAY.COM/itm/1", "https://www.ebay.com/itm/1"),
    ("https://www.ebay.com/itm/Innova-Star-Destroyer/1?_trkparms=x",
     "https://www.ebay.com/itm/Innova-Star-Destroyer/1"),
    ("  https://www.ebay.com/itm/1?a=b  ", "https://www.ebay.com/itm/1"),
    ("https://www.ebay.com:8443/itm/1?a=b", "https://www.ebay.com:8443/itm/1"),
])
def test_url_is_stripped_to_scheme_host_and_path(raw, expected):
    assert one(itemWebUrl=raw).items[0].url == expected


@pytest.mark.parametrize("raw", [
    "javascript:alert(1)", "ftp://www.ebay.com/itm/1", "//www.ebay.com/itm/1", "file:///etc/passwd",
    "data:text/html,<script>", "https://user:pw@www.ebay.com/itm/1", "https://www.ebay.com",
    "https://www.ebay.com/", "not a url", "https://www.ebay.com/itm/ 1", "https://www.ebay.com/itm/1\n2",
    "https://www.ebay.com:abc/itm/1", "", None, 123, ["https://www.ebay.com/itm/1"], MISSING,
    # browsers read a backslash as a slash: this one navigates to evil.example, urllib sees www.ebay.com
    "https://evil.example\\www.ebay.com/itm/1", "https://www.ebay.com\\@evil.example/itm/1",
    "https://www.ebay.com/itm/1\\..\\x", "https://www.ebay.com%2fevil.example/itm/1",
])
def test_non_http_or_malformed_urls_fall_back_to_the_item_page(raw):
    assert one(itemWebUrl=raw).items[0].url == f"https://www.ebay.com/itm/{pid(1)}"


@pytest.mark.parametrize("condition_id,tag", [
    ("1000", "condition:new"), ("1500", "condition:new"), (1000, "condition:new"), (1500, "condition:new"),
    ("1750", "condition:used"), ("2000", "condition:used"), ("2500", "condition:used"),
    ("2750", "condition:used"), ("3000", "condition:used"), ("4000", "condition:used"),
    ("5000", "condition:used"), ("6000", "condition:used"), ("7000", "condition:used"), (3000, "condition:used"),
    (" 1000 ", "condition:new"),
    (MISSING, None), (None, None), ("", None), ("abc", None), ("new", None), (True, None), ([], None),
    ({}, None), (-1000, None),
])
def test_condition_id_maps_to_exactly_one_tag_or_none(condition_id, tag):
    p = one(conditionId=condition_id).items[0]
    assert p.tags == ([tag] if tag else [])


def test_condition_text_alone_gives_no_tag_but_is_kept_as_product_type():
    p = one(conditionId=MISSING, condition="Used").items[0]
    assert p.tags == [] and p.product_type == "Used"
    assert one(conditionId=MISSING, condition=MISSING).items[0].product_type == ""
    assert one(condition=5).items[0].product_type == ""


@pytest.mark.parametrize("options,kept", [
    (["FIXED_PRICE"], True), (["FIXED_PRICE", "BEST_OFFER"], True), (MISSING, True), ([], True),
    (["AUCTION"], False), (["AUCTION", "FIXED_PRICE"], False), (["AUCTION", "BEST_OFFER"], False),
    (["CLASSIFIED_AD"], False), (["BEST_OFFER"], False),
])
def test_auctions_and_non_fixed_price_items_never_become_listings(options, kept):
    result = one(buyingOptions=options)
    assert len(result.items) == (1 if kept else 0) and result.skipped_other == (0 if kept else 1)


@pytest.mark.parametrize("raw,expected", [
    ("2026-11-04T18:21:33.000Z", "2026-11-04T18:21:33.000Z"), ("2026-11-04", "2026-11-04"),
    ("2026-11-04T18:21:33+02:00", "2026-11-04T18:21:33+02:00"),
    ("soon", ""), ("", ""), (None, ""), (20261104, ""), ("11/04/2026", ""), ("2026-11-04 DROP TABLE", ""),
    (MISSING, ""),
])
def test_end_date_is_kept_only_when_it_looks_like_iso(raw, expected):
    assert one(itemEndDate=raw).items[0].ends_at == expected


def test_title_is_kept_as_is_and_lone_surrogates_are_scrubbed():
    title = "  NEW ★ Innova Star Destroyer 175g!!  "
    assert one(title=title).items[0].title == title
    body = '{"total": 1, "itemSummaries": [' + json.dumps(summary(1)).replace(
        "Innova Star Destroyer 1", "Star \\ud800 Destroyer") + "]}"
    e = Ebay(search_script=[httpx.Response(200, content=body.encode())])
    title = search(e).items[0].title
    assert title == "Star � Destroyer"
    title.encode("utf-8")  # storable


@pytest.mark.parametrize("title", ["", "   ", None, 5, MISSING, ["x"]])
def test_untitled_items_are_skipped(title):
    result = one(title=title)
    assert result.items == [] and result.skipped_other == 1


# --- build_queries ----------------------------------------------------------------------------------------------

MOLDS = [
    {"manufacturer": "Innova", "mold": "Destroyer"},
    {"manufacturer": "Innova", "mold": "Roc3"},
    {"manufacturer": "Discraft", "mold": "Buzzz"},
    {"manufacturer": "Obscure Discs", "mold": "Thing"},
    {"manufacturer": "Empty Maker", "mold": "Blank"},
]
PLASTICS = {
    "generic": [{"name": "Premium", "aliases": ["Premium Plastic"]}, {"name": "Recycled", "aliases": []}],
    "manufacturers": {
        "Innova": [{"name": "Star"}, {"name": "DX"}, {"name": "Z"}],
        "Discraft": [{"name": "Z"}, {"name": "400"}, {"name": "ESP"}],
        "Empty Maker": [],
    },
}


def test_build_queries_is_mold_times_plastic_of_its_manufacturer():
    qs = ebay.build_queries(MOLDS, PLASTICS)
    # Innova 2 molds x 3, Discraft 1 x 3, then the two makers without plastics get the 2 generic ones.
    assert len(qs) == 6 + 3 + 2 + 2
    assert qs[:3] == [("innova|destroyer|star", "Star Destroyer Innova"),
                      ("innova|destroyer|dx", "DX Destroyer Innova"),
                      ("innova|destroyer|z", "Z Destroyer Innova")]
    assert dict(qs)["discraft|buzzz|400"] == "400 Buzzz Discraft"


def test_build_queries_falls_back_to_generic_plastics_never_an_empty_plastic():
    qs = dict(ebay.build_queries(MOLDS, PLASTICS))
    assert qs["obscure discs|thing|premium"] == "Premium Thing Obscure Discs"
    assert qs["obscure discs|thing|recycled"] == "Recycled Thing Obscure Discs"
    assert qs["empty maker|blank|premium"] == "Premium Blank Empty Maker"  # listed but with no plastics
    assert not [k for k in qs if k.startswith("innova|") and k.endswith(("premium", "recycled"))]
    for key in qs:
        parts = key.split("|")
        assert len(parts) == 3 and all(parts)
    assert ebay.build_queries([{"manufacturer": "X", "mold": "Y"}], {"generic": [], "manufacturers": {}}) == []


def test_single_character_and_numeric_plastics_go_through():
    qs = dict(ebay.build_queries(MOLDS, PLASTICS))
    assert qs["discraft|buzzz|z"] == "Z Buzzz Discraft"
    assert qs["discraft|buzzz|400"] == "400 Buzzz Discraft"
    assert qs["innova|roc3|z"] == "Z Roc3 Innova"


def test_build_queries_is_deterministic_with_unique_keys():
    first, second = ebay.build_queries(MOLDS, PLASTICS), ebay.build_queries(MOLDS, PLASTICS)
    assert first == second
    assert first == ebay.build_queries({"_doc": "x", "molds": MOLDS}, PLASTICS)  # whole document accepted
    keys = [k for k, _ in first]
    assert len(set(keys)) == len(keys)
    assert all(k == k.lower() for k in keys)


def test_build_queries_dedupes_and_normalises():
    molds = [{"manufacturer": "Innova", "mold": "Destroyer"}, {"manufacturer": " innova ", "mold": "DESTROYER"},
             {"manufacturer": "Innova", "mold": "Wraith"}]
    plastics = {"manufacturers": {"INNOVA": [{"name": "Star"}, {"name": "star"}, "Champion", {"name": "  "},
                                             {"name": ""}, {}, "Metal  Flake"]}}
    qs = ebay.build_queries(molds, plastics)
    assert [k for k, _ in qs] == [f"innova|{m}|{p}" for m in ("destroyer", "wraith")
                                  for p in ("star", "champion", "metal flake")]
    assert dict(qs)["innova|destroyer|metal flake"] == "Metal Flake Destroyer Innova"


def test_build_queries_skips_malformed_mold_entries():
    molds = [{"manufacturer": "Innova"}, {"mold": "X"}, {"manufacturer": "", "mold": "X"},
             {"manufacturer": "Innova", "mold": "  "}, "junk", None, 5, {"manufacturer": "Innova", "mold": "Boss"}]
    qs = ebay.build_queries(molds, {"manufacturers": {"Innova": ["Star"]}})
    assert qs == [("innova|boss|star", "Star Boss Innova")]


def test_shipped_data_produces_a_sane_query_set():
    qs = ebay.build_queries()
    assert 1_500 <= len(qs) <= 5_000
    keys = [k for k, _ in qs]
    assert len(set(keys)) == len(keys), "duplicate query keys"
    for key, text in qs:
        parts = key.split("|")
        assert len(parts) == 3 and all(p.strip() for p in parts), f"empty part in {key!r}"
        assert key == key.lower() and text.strip() == text and "  " not in text
        assert all(p in text.lower() for p in parts), (key, text)
    assert ebay.build_queries() == qs  # stable between calls
    assert "innova|destroyer|star" in keys


def test_shipped_query_count_matches_the_data_files():
    molds = json.loads((DATA / "molds.json").read_text(encoding="utf-8"))["molds"]
    plastics = json.loads((DATA / "plastics.json").read_text(encoding="utf-8"))
    per_maker = {m.lower(): len(v) for m, v in plastics["manufacturers"].items()}
    expected = len({(e["manufacturer"].lower(), e["mold"].lower(), p["name"].lower())
                    for e in molds
                    for p in (plastics["manufacturers"].get(e["manufacturer"]) or plastics["generic"])})
    assert len(ebay.build_queries()) == expected
    assert per_maker  # sanity: the file is not empty


# --- collect() against a real in-memory database --------------------------------------------------------------------

def run_collect(conn, e, queries, day="2026-01-01", budget=3500, client=None, **kw):
    logs: list[str] = []
    client = client or make_client(e)
    kw.setdefault("log", logs.append)
    stats = ebay.collect(conn, client, day, call_budget=budget, queries=queries, **kw)
    return stats, logs


def table(conn, sql, *args):
    return [tuple(r) for r in conn.execute(sql, args)]


def count(conn, name):
    return conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]


def snapshot(conn):
    return list(conn.iterdump())


def qlist(n, prefix="q"):
    return [(f"{prefix}{i}", f"{prefix} {i}") for i in range(n)]


def test_collect_records_new_listings_incomplete_and_the_query_run(monkeypatch):
    spied = []
    real = db.record_products
    monkeypatch.setattr(db, "record_products", lambda *a, **kw: (spied.append(kw), real(*a, **kw))[1])
    conn = db.connect(":memory:")
    e = Ebay({"star destroyer innova": [
        summary(1, price={"value": "18.99", "currency": "USD"}),
        summary(2, price={"value": "12.50", "currency": "USD"}, condition="Pre-Owned", conditionId="3000",
                itemEndDate="2026-06-01T00:00:00.000Z")]})
    stats, logs = run_collect(conn, e, [("innova|destroyer|star", "star destroyer innova")])
    assert stats == {"queries_run": 1, "queries_complete": 1, "calls": 1, "items_seen": 2, "new_listings": 2,
                     "gone": 0, "inferred_sales": 0, "skipped_currency": 0, "stopped": "done"}
    assert [kw.get("complete") for kw in spied] == [False]

    assert table(conn, "SELECT id, name, base_url, currency, kind FROM stores") == [
        ("ebay", "eBay", "https://www.ebay.com", "USD", "marketplace")]
    assert table(conn, "SELECT store_id, product_id, title, product_type, tags, first_seen, last_seen, gone, "
                       "ends_at, last_query FROM listings ORDER BY product_id") == [
        ("ebay", pid(1), "Innova Star Destroyer 1", "New", '["condition:new"]', "2026-01-01", "2026-01-01", 0, "",
         "innova|destroyer|star"),
        ("ebay", pid(2), "Innova Star Destroyer 2", "Pre-Owned", '["condition:used"]', "2026-01-01", "2026-01-01",
         0, "2026-06-01T00:00:00.000Z", "innova|destroyer|star")]
    assert table(conn, "SELECT variant_id, price_cents, available, gone FROM variants ORDER BY variant_id") == [
        (pid(1), 1899, 1, 0), (pid(2), 1250, 1, 0)]
    assert table(conn, "SELECT observed_on, price_cents, available, gone FROM observations "
                       "ORDER BY price_cents") == [("2026-01-01", 1250, 1, 0), ("2026-01-01", 1899, 1, 0)]
    assert table(conn, "SELECT store_id, observed_on, status, n_products, error, finished_at IS NOT NULL "
                       "FROM runs") == [("ebay", "2026-01-01", "ok", 2, None, 1)]
    assert table(conn, "SELECT query_key, query, last_run, last_complete, last_total, runs "
                       "FROM ebay_queries") == [
        ("innova|destroyer|star", "star destroyer innova", "2026-01-01", 1, 2, 1)]
    assert count(conn, "sales") == 0
    assert any("stopped (done)" in line for line in logs)


def test_collect_passes_the_weight_parser_to_the_database():
    conn = db.connect(":memory:")
    e = Ebay({"q": [summary(1, title="Innova Star Destroyer 173g")]})
    run_collect(conn, e, [("k", "q")], weight_parser=lambda t: 173 if "173g" in t else None)
    assert table(conn, "SELECT weight_g FROM variants") == [(173,)]


def test_listing_that_disappears_from_a_complete_query_is_expired_with_a_low_confidence_sale():
    conn = db.connect(":memory:")
    q = [("k", "q")]
    e = Ebay({"q": [summary(1, price={"value": "18.99", "currency": "USD"}),
                    summary(2, price={"value": "30.00", "currency": "USD"})]})
    run_collect(conn, e, q, "2026-01-01")
    e.results["q"] = [summary(1, price={"value": "18.99", "currency": "USD"})]
    stats, _ = run_collect(conn, e, q, "2026-01-02")
    assert stats["gone"] == 1 and stats["inferred_sales"] == 1 and stats["queries_complete"] == 1
    assert table(conn, "SELECT product_id, gone FROM listings ORDER BY product_id") == [(pid(1), 0), (pid(2), 1)]
    assert table(conn, "SELECT sold_on, price_cents, quantity, source, confidence FROM sales") == [
        ("2026-01-01", 3000, 1, "inferred_disappeared", "low")]
    assert table(conn, "SELECT o.available, o.gone FROM observations o JOIN variants v ON v.id=o.variant_pk "
                       "WHERE v.variant_id=? ORDER BY o.observed_on DESC", pid(2))[0] == (0, 1)


def test_listing_missing_from_an_incomplete_query_is_not_expired():
    conn = db.connect(":memory:")
    q = [("k", "q")]
    e = Ebay({"q": Virtual(450)})
    first, _ = run_collect(conn, e, q, "2026-01-01")
    assert first["queries_complete"] == 1 and first["items_seen"] == 450
    # Day two the budget only allows two pages: 50 items are simply not looked at.
    second, logs = run_collect(conn, e, q, "2026-01-02", budget=2)
    assert second["queries_run"] == 1 and second["queries_complete"] == 0 and second["calls"] == 2
    assert second["gone"] == 0 and second["inferred_sales"] == 0
    assert count(conn, "sales") == 0
    assert conn.execute("SELECT COUNT(*) FROM listings WHERE gone=1").fetchone()[0] == 0
    assert table(conn, "SELECT last_run, last_complete, last_total, runs FROM ebay_queries") == [
        ("2026-01-02", 0, 450, 2)]
    assert any("incomplete" in line for line in logs)


def test_a_page_failure_makes_the_query_incomplete_so_nothing_expires():
    conn = db.connect(":memory:")
    q = [("k", "q")]
    e = Ebay({"q": Virtual(250)})
    run_collect(conn, e, q, "2026-01-01")
    e.search_script[:] = [None] + [status(500)] * 4  # page 2 dies on the second day
    e.requests.clear()
    stats, _ = run_collect(conn, e, q, "2026-01-02")
    assert stats["queries_complete"] == 0 and stats["gone"] == 0 and stats["items_seen"] == 200
    assert count(conn, "sales") == 0 and conn.execute("SELECT COUNT(*) FROM listings WHERE gone=1").fetchone()[0] == 0


def test_a_listing_that_reached_its_end_date_is_expired_without_an_inferred_sale():
    conn = db.connect(":memory:")
    q = [("k", "q")]
    e = Ebay({"q": [summary(1, itemEndDate="2026-01-02T10:00:00.000Z")]})
    run_collect(conn, e, q, "2026-01-01")
    e.results["q"] = [summary(2)]  # still returns something: an empty 200 alone is not believed
    stats, _ = run_collect(conn, e, q, "2026-01-03")
    assert stats["gone"] == 1 and stats["inferred_sales"] == 0 and count(conn, "sales") == 0


def test_relisting_cancels_the_inferred_sale():
    conn = db.connect(":memory:")
    q = [("k", "q")]
    e = Ebay({"q": [summary(1)]})
    run_collect(conn, e, q, "2026-01-01")
    e.results["q"] = [summary(2)]
    run_collect(conn, e, q, "2026-01-02")
    assert count(conn, "sales") == 1
    e.results["q"] = [summary(1), summary(2)]
    run_collect(conn, e, q, "2026-01-03")
    assert count(conn, "sales") == 0
    assert table(conn, "SELECT gone FROM listings") == [(0,), (0,)]


def test_an_item_found_by_another_query_today_is_not_expired_by_its_old_query():
    conn = db.connect(":memory:")
    e = Ebay({"one": [summary(1)], "two": []})
    run_collect(conn, e, [("k1", "one")], "2026-01-01")
    e.results["one"] = []  # k1 no longer returns it ...
    e.results["two"] = [summary(1)]  # ... but k2 does
    stats, _ = run_collect(conn, e, [("k1", "one"), ("k2", "two")], "2026-01-02")
    assert e.queries_searched[-2:] == ["two", "one"]  # never-run k2 goes first
    assert stats["gone"] == 0 and count(conn, "sales") == 0
    assert table(conn, "SELECT gone, last_query FROM listings") == [(0, "k2")]


def test_stale_queries_run_first_and_a_short_budget_resumes_the_rotation():
    conn = db.connect(":memory:")
    queries = qlist(4)
    e = Ebay({text: [summary(i)] for i, (_, text) in enumerate(queries)})
    client = make_client(e)

    day1, _ = run_collect(conn, e, queries, "2026-01-01", budget=2, client=client)
    assert e.queries_searched == ["q 0", "q 1"]
    assert (day1["queries_run"], day1["calls"], day1["stopped"]) == (2, 2, "budget")

    e.requests.clear()
    day2, _ = run_collect(conn, e, queries, "2026-01-02", budget=2, client=client)
    assert e.queries_searched == ["q 2", "q 3"]  # never-run ones come before the stale ones
    assert day2["stopped"] == "budget"

    e.requests.clear()
    day3, _ = run_collect(conn, e, queries, "2026-01-03", budget=3, client=client)
    assert e.queries_searched == ["q 0", "q 1", "q 2"]  # day-1 queries are the stalest
    assert table(conn, "SELECT query_key, last_run, runs FROM ebay_queries ORDER BY query_key") == [
        ("q0", "2026-01-03", 2), ("q1", "2026-01-03", 2), ("q2", "2026-01-03", 2), ("q3", "2026-01-02", 1)]
    assert day3["stopped"] == "budget"


def test_budget_exactly_matching_the_work_reports_done_not_budget():
    conn = db.connect(":memory:")
    queries = qlist(3)
    e = Ebay({text: [summary(i)] for i, (_, text) in enumerate(queries)})
    stats, _ = run_collect(conn, e, queries, budget=3)
    assert stats["stopped"] == "done" and stats["calls"] == 3 and stats["queries_run"] == 3


def test_call_budget_counts_pages_and_retries_and_is_never_exceeded():
    conn = db.connect(":memory:")
    queries = [("a", "big"), ("b", "flaky"), ("c", "never")]
    e = Ebay({"big": Virtual(450), "flaky": [summary(1)], "never": [summary(2)]},
             search_script=[None, None, None, status(500)])  # big: 3 pages; flaky: 1 failed attempt first
    stats, _ = run_collect(conn, e, queries, budget=5)
    assert len(e.search_requests) == 5 == stats["calls"]
    assert stats["stopped"] == "budget" and e.queries_searched.count("never") == 0
    assert stats["queries_run"] == 2 and stats["queries_complete"] == 2


def test_budget_cutting_a_query_short_records_it_incomplete_and_stops_on_the_next():
    conn = db.connect(":memory:")
    e = Ebay({"big": Virtual(1000), "next": [summary(1)]})
    stats, _ = run_collect(conn, e, [("a", "big"), ("b", "next")], budget=2)
    assert (stats["queries_run"], stats["queries_complete"], stats["calls"], stats["stopped"]) == (1, 0, 2, "budget")
    assert stats["items_seen"] == 400


def test_a_failing_query_is_logged_and_skipped_and_the_run_goes_on():
    conn = db.connect(":memory:")
    e = Ebay({"good": [summary(1)]}, search_script=[status(500)] * 4)
    stats, logs = run_collect(conn, e, [("bad", "bad"), ("good", "good")])
    assert stats["queries_run"] == 1 and stats["stopped"] == "done" and stats["calls"] == 5
    assert any("'bad'" in line and "failed" in line for line in logs)
    assert table(conn, "SELECT query_key, last_run, last_complete, last_total FROM ebay_queries ORDER BY query_key") == [
        ("bad", "2026-01-01", 0, 0), ("good", "2026-01-01", 1, 1)]  # the failed one is marked attempted, not complete
    assert table(conn, "SELECT status, error FROM runs") == [("ok", None)]


def test_a_run_where_every_query_failed_ends_with_status_error():
    conn = db.connect(":memory:")
    e = Ebay(search_script=[status(400)] * 2)
    stats, _ = run_collect(conn, e, qlist(2))
    assert stats["queries_run"] == 0 and stats["calls"] == 2 and stats["stopped"] == "done"
    [(run_status, error)] = table(conn, "SELECT status, error FROM runs")
    assert run_status == "error" and "no query succeeded" in error and "2 failed" in error
    assert count(conn, "listings") == 0
    assert table(conn, "SELECT query_key, last_complete FROM ebay_queries ORDER BY query_key") == [("q0", 0), ("q1", 0)]


def test_quota_stops_the_run_gracefully_and_keeps_what_was_recorded():
    conn = db.connect(":memory:")
    e = Ebay({"q 0": [summary(0)], "q 1": [summary(1)], "q 2": [summary(2)]},
             search_script=[None, status(429, 1), status(429, 1)])
    stats, logs = run_collect(conn, e, qlist(3))
    assert stats["stopped"] == "quota" and stats["queries_run"] == 1 and stats["calls"] == 3
    assert e.queries_searched == ["q 0", "q 1", "q 1"]  # the third query was never attempted
    assert table(conn, "SELECT product_id FROM listings") == [(pid(0),)]
    assert table(conn, "SELECT query_key FROM ebay_queries") == [("q0",)]
    assert table(conn, "SELECT status, error, n_products FROM runs") == [("ok", None, 1)]
    assert any("rate limit" in line for line in logs)


def test_quota_before_anything_was_recorded_is_an_error_run_not_an_exception():
    conn = db.connect(":memory:")
    e = Ebay(search_script=[status(429, 1), status(429, 1)])
    stats, _ = run_collect(conn, e, qlist(2))
    assert stats["stopped"] == "quota" and stats["queries_run"] == 0 and stats["calls"] == 2
    [(run_status, error)] = table(conn, "SELECT status, error FROM runs")
    assert run_status == "error" and "quota" in error
    assert count(conn, "listings") == 0


def test_a_streak_of_failing_queries_stops_the_run():
    conn = db.connect(":memory:")
    n = ebay.MAX_CONSECUTIVE_FAILURES
    e = Ebay(search_script=[status(400)] * (n + 5))
    stats, logs = run_collect(conn, e, qlist(n + 5))
    assert stats["stopped"] == "errors" and stats["calls"] == n and len(e.search_requests) == n
    assert table(conn, "SELECT status FROM runs") == [("error",)]
    assert any("in a row" in line for line in logs)


def test_a_success_resets_the_failure_streak():
    conn = db.connect(":memory:")
    n = ebay.MAX_CONSECUTIVE_FAILURES - 1
    queries = [(f"bad{i}", f"bad {i}") for i in range(n)] + [("good", "good")] + \
              [(f"worse{i}", f"worse {i}") for i in range(n)]
    e = Ebay({"good": [summary(1)]}, search_script=[status(400)] * n + [None] + [status(400)] * n)
    stats, _ = run_collect(conn, e, queries)
    assert stats["stopped"] == "done" and stats["queries_run"] == 1 and stats["calls"] == 2 * n + 1
    assert table(conn, "SELECT status FROM runs") == [("ok",)]


def test_non_usd_items_are_counted_in_the_stats():
    conn = db.connect(":memory:")
    e = Ebay({"q": [summary(1), summary(2, price={"value": "20.00", "currency": "EUR"}),
                    summary(3, price={"value": "20.00", "currency": "GBP"})],
              "q2": [summary(4, price={"value": "20.00", "currency": "CAD"})]})
    stats, _ = run_collect(conn, e, [("a", "q"), ("b", "q2")])
    assert stats["skipped_currency"] == 3 and stats["items_seen"] == 1 and stats["new_listings"] == 1
    assert count(conn, "listings") == 1


def test_auth_error_at_the_token_endpoint_leaves_the_database_untouched():
    conn = db.connect(":memory:")
    e = Ebay({"q": [summary(1)]}, token_script=[httpx.Response(401, json=fixture("ebay_token_error.json"))])
    before = snapshot(conn)
    with pytest.raises(EbayAuthError):
        run_collect(conn, e, [("k", "q")])
    assert snapshot(conn) == before and not conn.in_transaction
    assert all(count(conn, t) == 0 for t in ALL_TABLES)


def test_auth_error_does_not_touch_a_database_that_already_holds_data():
    conn = db.connect(":memory:")
    e = Ebay({"q": [summary(1)]})
    run_collect(conn, e, [("k", "q")], "2026-01-01")
    before = snapshot(conn)
    e2 = Ebay({"q": []}, token_script=[status(401)])
    with pytest.raises(EbayAuthError):
        run_collect(conn, e2, [("k", "q")], "2026-01-02")
    assert snapshot(conn) == before


def test_auth_error_on_the_first_search_also_leaves_the_database_untouched():
    conn = db.connect(":memory:")
    e = Ebay({"q": [summary(1)]}, search_script=[status(401), status(401)])
    with pytest.raises(EbayAuthError):
        run_collect(conn, e, [("k", "q")])
    assert all(count(conn, t) == 0 for t in ALL_TABLES)


def test_auth_error_mid_run_closes_the_run_as_error_and_keeps_earlier_queries():
    conn = db.connect(":memory:")
    e = Ebay({"q 0": [summary(0)], "q 1": [summary(1)], "q 2": [summary(2)]},
             search_script=[None, status(401), status(401)])
    with pytest.raises(EbayAuthError):
        run_collect(conn, e, qlist(3))
    assert table(conn, "SELECT product_id FROM listings") == [(pid(0),)]
    assert table(conn, "SELECT query_key FROM ebay_queries") == [("q0",)]  # the query in flight left no trace
    [(run_status, error, finished)] = table(conn, "SELECT status, error, finished_at IS NOT NULL FROM runs")
    assert run_status == "error" and error.startswith("EbayAuthError") and finished == 1
    assert "q 2" not in e.queries_searched


def test_an_unexpected_exception_closes_the_run_row_and_propagates():
    conn = db.connect(":memory:")

    class Exploding:
        def search(self, query, key, max_calls):
            if key == "ok":
                return SearchResult([], 0, True, 1)
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        ebay.collect(conn, Exploding(), "2026-01-01", queries=[("ok", "a"), ("x", "b")], log=lambda s: None)
    assert table(conn, "SELECT status, error FROM runs") == [("error", "RuntimeError: boom")]


def test_collect_works_with_any_object_that_has_search_and_an_empty_query_list_is_done():
    conn = db.connect(":memory:")
    calls = []

    class Fake:
        def search(self, query, key, max_calls):
            calls.append((query, key, max_calls))
            return SearchResult([], 0, True, 1)

    stats = ebay.collect(conn, Fake(), "2026-01-01", call_budget=10, queries=[("k", "q"), ("k", "dup"), ("k2", "q2")],
                         log=lambda s: None)
    assert calls == [("q", "k", 10), ("q2", "k2", 9)]  # duplicate keys collapse; remaining budget is passed down
    assert stats["queries_run"] == 2 and stats["calls"] == 2

    conn = db.connect(":memory:")
    empty = ebay.collect(conn, Fake(), "2026-01-01", queries=[], log=lambda s: None)
    assert empty == {"queries_run": 0, "queries_complete": 0, "calls": 0, "items_seen": 0, "new_listings": 0,
                     "gone": 0, "inferred_sales": 0, "skipped_currency": 0, "stopped": "done"}
    assert table(conn, "SELECT status FROM runs") == [("ok",)]


def test_collect_defaults_to_the_generated_queries_and_zero_budget_stops_immediately(monkeypatch):
    monkeypatch.setattr(ebay, "build_queries", lambda: [("a", "alpha"), ("b", "beta")])
    conn = db.connect(":memory:")
    e = Ebay()
    stats, _ = run_collect(conn, e, None)
    assert e.queries_searched == ["alpha", "beta"] and stats["queries_run"] == 2

    conn = db.connect(":memory:")
    e = Ebay()
    stats, _ = run_collect(conn, e, None, budget=0)
    assert stats["stopped"] == "budget" and e.search_requests == [] and stats["queries_run"] == 0
    assert table(conn, "SELECT status FROM runs") == [("ok",)]


def test_collect_returns_exactly_the_contract_keys():
    conn = db.connect(":memory:")
    stats, _ = run_collect(conn, Ebay({"q": [summary(1)]}), [("k", "q")])
    assert list(stats) == ["queries_run", "queries_complete", "calls", "items_seen", "new_listings", "gone",
                           "inferred_sales", "skipped_currency", "stopped"]


def test_default_log_is_print(capsys):
    conn = db.connect(":memory:")
    ebay.collect(conn, make_client(Ebay({"q": [summary(1)]})), "2026-01-01", queries=[("k", "q")])
    assert "ebay: stopped (done)" in capsys.readouterr().out


def test_ebay_store_constant_matches_the_contract():
    assert EBAY_STORE == {"id": "ebay", "name": "eBay", "base_url": "https://www.ebay.com",
                          "currency": "USD", "kind": "marketplace"}
    assert issubclass(EbayAuthError, EbayError) and issubclass(EbayQuotaError, EbayError)


# --- regressions found in adversarial review ------------------------------------------------------------------------

def total_on_page(e, offset, total):
    """Make eBay answer the page at `offset` with an (empty) body that reports `total`."""
    inner = e._search

    def respond(request):
        if int(request.url.params["offset"]) == offset:
            return httpx.Response(200, json={"total": total, "limit": 200, "offset": offset})
        return inner(request)

    e._search = respond


@pytest.mark.parametrize("shrunk_total", [0, 150, 299])
def test_a_total_that_collapses_between_pages_cannot_fake_completeness(shrunk_total):
    # Page 1 says 500 results, page 2 suddenly says fewer than the 200 already seen. Believing the
    # last page would call the query complete and expire the 300 listings that were never fetched.
    e = Ebay({"q": Virtual(500)})
    total_on_page(e, 200, shrunk_total)
    result = search(e)
    assert result.complete is False and result.total == 500 and len(result.items) == 200
    assert "200 of 500" in result.error


def test_a_total_that_shrinks_while_paging_stays_incomplete_when_items_were_missed():
    # 3 of the first 200 sell while we page, everything after them slides up and 3 items are never
    # requested: 497 distinct items seen against the 500 eBay first announced.
    page1 = [summary(i) for i in range(200)]
    page2 = [summary(i) for i in range(203, 403)]
    page3 = [summary(i) for i in range(403, 500)]

    def body(page, offset):
        return lambda request: httpx.Response(200, json={
            "total": 500 if offset == 0 else 497, "limit": 200, "offset": offset, "itemSummaries": page})

    e = Ebay(search_script=[body(page1, 0), body(page2, 200), body(page3, 400)])
    result = search(e)
    assert len(result.items) == 497 and result.total == 500 and not result.complete


def test_a_total_that_grows_while_paging_is_honoured():
    e = Ebay({"q": Virtual(300)})
    inner = e._search

    def grow(request):
        resp = inner(request)
        body = json.loads(resp.content)
        body["total"] = 300 if int(request.url.params["offset"]) == 0 else 450
        return httpx.Response(200, json=body)

    e._search = grow
    result = search(e)
    assert result.total == 450 and not result.complete and len(result.items) == 300


def test_queries_that_always_fail_cannot_starve_the_rest_of_the_rotation():
    # 15 queries eBay always refuses, every 7th of 100. A failed query used to stay "never run", so
    # on day 2 they all sorted first, 10 failures in a row tripped the streak guard and the
    # collector made no progress on day 2 or ever after.
    conn = db.connect(":memory:")
    names = [f"{'bad' if i % 7 == 0 else 'good'} {i}" for i in range(100)]
    good = [n for n in names if n.startswith("good")]
    e = Ebay({n: [summary(i)] for i, n in enumerate(names) if n in good},
             rejected={n: 400 for n in names if n.startswith("bad")})
    client = make_client(e)
    queries = [(n, n) for n in names]
    for day in ("2026-01-01", "2026-01-02", "2026-01-03"):
        stats, _ = run_collect(conn, e, queries, day, client=client)
        assert (stats["queries_run"], stats["stopped"]) == (len(good), "done"), day
    assert table(conn, "SELECT COUNT(DISTINCT last_run) FROM ebay_queries") == [(1,)]


def test_a_block_of_failing_queries_at_the_head_delays_the_rotation_but_does_not_kill_it():
    # Worst case: 12 refused queries first in the input order. Run 1 is stopped by the streak guard
    # but moves the 10 it tried to the back; run 2 reaches the good queries, and so on.
    conn = db.connect(":memory:")
    bad = [(f"bad{i}", f"bad {i}") for i in range(12)]
    good = [(f"good{i}", f"good {i}") for i in range(3)]
    e = Ebay({text: [summary(i)] for i, (_, text) in enumerate(good)}, rejected={text: 400 for _, text in bad})
    client = make_client(e)
    ran = []
    for day in ("2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"):
        stats, _ = run_collect(conn, e, bad + good, day, client=client)
        ran.append(stats["queries_run"])
    assert ran[0] == 0 and ran[1] == 3 and ran[3] == 3  # (day 3 ties again and loses to the bad block)
    assert table(conn, "SELECT COUNT(*) FROM listings") == [(3,)]


def test_failed_queries_are_recorded_as_attempted_and_keep_their_last_known_total():
    conn = db.connect(":memory:")
    e = Ebay({"flaky": Virtual(40), "fine": [summary(900)]})
    run_collect(conn, e, [("flaky", "flaky"), ("fine", "fine")], "2026-01-01")
    e.rejected["flaky"] = 503
    stats, _ = run_collect(conn, e, [("flaky", "flaky"), ("fine", "fine")], "2026-01-02")
    assert stats["queries_run"] == 1 and stats["calls"] == 5
    assert table(conn, "SELECT query_key, last_run, last_complete, last_total FROM ebay_queries ORDER BY query_key") == [
        ("fine", "2026-01-02", 1, 1), ("flaky", "2026-01-02", 0, 40)]  # attempted today, baseline kept
    assert table(conn, "SELECT COUNT(*) FROM listings WHERE gone=1") == [(0,)]  # nothing expired
    assert count(conn, "sales") == 0


def test_running_out_of_budget_is_not_held_against_the_query():
    conn = db.connect(":memory:")
    e = Ebay({"a": [summary(1)], "b": [summary(2)], "c": [summary(3)]}, search_script=[None, status(500)])
    stats, _ = run_collect(conn, e, [("a", "a"), ("b", "b"), ("c", "c")], budget=2)  # b fails, no call left to retry
    assert stats["stopped"] == "budget" and stats["queries_run"] == 1
    assert table(conn, "SELECT query_key FROM ebay_queries") == [("a",)]  # b and c stay never-run queries


def test_failed_attempts_are_not_written_when_the_run_dies_of_an_auth_error():
    conn = db.connect(":memory:")
    e = Ebay({"q 2": [summary(2)]}, rejected={"q 0": 400, "q 1": 400}, search_script=[None, None, status(401), status(401)])
    before = snapshot(conn)
    with pytest.raises(EbayAuthError):
        run_collect(conn, e, qlist(3))
    assert snapshot(conn) == before


@pytest.mark.parametrize("pad", [150, 190, 194, 197, 199, 200, 230])
@pytest.mark.parametrize("secret", [SECRET, ID, BASIC])
def test_a_secret_cut_by_the_message_length_limit_is_still_redacted(pad, secret):
    # The error text is capped at 200 characters; redacting AFTER the cut would leave a fragment
    # of a secret that straddles it.
    body = {"error_description": "x" * pad + secret + " trailing", "errors": [{"message": "x" * pad + secret}]}
    for kind, e in (("token", Ebay(token_script=[status(400, body=body)])),
                    ("search", Ebay(search_script=[status(400, body=body)]))):
        with pytest.raises(EbayError) as info:
            search(e)
        for text in (str(info.value), repr(info.value)):
            assert secret[:6] not in text, (kind, text)
            assert_clean(text)


def test_a_token_cut_by_the_message_length_limit_is_still_redacted():
    def echo_token(request):
        token = request.headers["authorization"].removeprefix("Bearer ")
        return httpx.Response(400, json={"errors": [{"message": "y" * 198 + token}]})

    e = Ebay(search_script=[echo_token])
    with pytest.raises(EbayError) as info:
        search(e)
    assert "tok" not in str(info.value)


@pytest.mark.parametrize("value", ["0.001", "0.004", "0.0001", "0.000001", " 0.002 "])
def test_a_price_that_rounds_to_zero_cents_is_not_a_price(value):
    result = one(price={"value": value, "currency": "USD"})
    assert result.items == [] and result.skipped_other == 1


@pytest.mark.parametrize("number", ["1e-30", "1E-999999999", "0.0001"])
def test_a_json_number_price_that_rounds_to_zero_cents_is_not_a_price(number):
    body = '{"total": 1, "itemSummaries": [' + json.dumps(summary(1)).replace(
        '"value": "24.99"', f'"value": {number}') + "]}"
    e = Ebay(search_script=[httpx.Response(200, content=body.encode())])
    result = search(e)
    assert result.items == [] and result.skipped_other == 1 and result.complete


def drifted(i, **over):
    """A summary in a shape this client does not understand: the price moved to another field."""
    s = summary(i, **over)
    s["price"] = {"amount": "24.99", "currencyCode": "USD"}
    return s


def test_a_response_that_is_mostly_unusable_is_not_a_complete_result():
    # eBay changes the price object; every item is "fetched" but none can be read. Calling that
    # complete would expire every listing the query ever returned.
    e = Ebay({"q": Virtual(10, make=drifted)})
    result = search(e)
    assert result.items == [] and result.skipped_other == 10
    assert result.complete is False and "unusable" in result.error


def test_a_majority_of_foreign_currency_items_is_not_a_complete_result_either():
    foreign = lambda i: summary(i, price={"value": "20.00", "currency": "EUR"})  # noqa: E731
    result = search(Ebay({"q": Virtual(8, make=foreign)}))
    assert result.skipped_currency == 8 and not result.complete


@pytest.mark.parametrize("good,junk,complete", [
    (0, 4, True),   # too few to tell
    (2, 4, True),
    (6, 5, True),   # a minority
    (5, 5, True),   # not a majority
    (4, 5, False),  # a majority of at least five
    (0, 5, False),
])
def test_a_few_unusable_items_are_noise_not_drift(good, junk, complete):
    items = [summary(i) for i in range(good)] + [drifted(100 + i) for i in range(junk)]
    result = search(Ebay({"q": items}))
    assert len(result.items) == good and result.skipped_other == junk and result.complete is complete


def test_auctions_and_duplicates_are_not_counted_as_unusable():
    items = [summary(i, buyingOptions=["AUCTION"]) for i in range(20)] + [summary(100)]
    result = search(Ebay({"q": items}))
    assert result.skipped_other == 20 and result.complete and len(result.items) == 1
    variations = [summary(1, itemId=f"v1|{pid(1)}|{v}") for v in range(1, 12)]
    result = search(Ebay({"q": variations}))
    assert result.skipped_variations == 10 and result.complete


def test_a_mostly_unusable_response_expires_nothing():
    conn = db.connect(":memory:")
    q = [("k", "q")]
    e = Ebay({"q": Virtual(10)})
    run_collect(conn, e, q, "2026-01-01")
    e.results["q"] = Virtual(10, make=drifted)
    stats, logs = run_collect(conn, e, q, "2026-01-02")
    assert stats["queries_complete"] == 0 and stats["gone"] == 0 and stats["inferred_sales"] == 0
    assert count(conn, "sales") == 0 and conn.execute("SELECT COUNT(*) FROM listings WHERE gone=1").fetchone()[0] == 0
    assert any("incomplete" in line and "unusable" in line for line in logs)


def test_a_query_whose_results_suddenly_vanish_is_not_expired_on_the_first_sighting():
    # A transient "200 OK, total 0" for a query that had 30 results yesterday must not mark 30
    # listings gone and invent 30 sales; if it is still empty on the next run it is believed.
    conn = db.connect(":memory:")
    q = [("k", "q")]
    e = Ebay({"q": Virtual(30)})
    run_collect(conn, e, q, "2026-01-01")
    e.results["q"] = []
    stats, logs = run_collect(conn, e, q, "2026-01-02")
    assert stats["queries_complete"] == 1 and stats["gone"] == 0 and stats["inferred_sales"] == 0
    assert count(conn, "sales") == 0
    assert any("not expiring" in line for line in logs)
    assert table(conn, "SELECT last_run, last_complete, last_total FROM ebay_queries") == [("2026-01-02", 1, 0)]
    stats, _ = run_collect(conn, e, q, "2026-01-03")  # still empty: now it is real
    assert stats["gone"] == 30 and stats["inferred_sales"] == 30


@pytest.mark.parametrize("before,after,expires", [
    (30, 0, False), (30, 14, False), (30, 15, True), (30, 29, True), (20, 9, False),
    (19, 0, False), (2, 0, False), (2, 1, True),  # an empty result after a non-empty one needs confirming
])
def test_the_vanishing_results_guard_only_applies_to_big_drops_of_established_queries(before, after, expires):
    conn = db.connect(":memory:")
    q = [("k", "q")]
    e = Ebay({"q": Virtual(before)})
    run_collect(conn, e, q, "2026-01-01")
    e.results["q"] = Virtual(after)
    stats, _ = run_collect(conn, e, q, "2026-01-02")
    assert stats["gone"] == (before - after if expires else 0)


def _chain(exc):
    """Every exception reachable from exc through __cause__ / __context__ (hidden or not)."""
    found, stack = [], [exc]
    while stack:
        item = stack.pop()
        if item is not None and all(item is not f for f in found):
            found.append(item)
            stack += [item.__cause__, item.__context__]
    return found


@pytest.mark.parametrize("script", [
    [httpx.ProxyError(f"proxy said {SECRET}")], [httpx.DecodingError(f"bad {SECRET}")],
    [httpx.UnsupportedProtocol(f"nope {SECRET}")], [httpx.TooManyRedirects(f"loop {SECRET}")],
    [httpx.ConnectError(f"c {SECRET}")] * 4, [httpx.Response(200, content=f"{SECRET} tok-1".encode())],
    [httpx.Response(200, content=b"{" + SECRET.encode())],
], ids=["proxy", "decoding", "protocol", "redirects", "connect", "not-json", "truncated-json"])
def test_exceptions_do_not_chain_to_the_http_errors_that_hold_the_request(script):
    # The httpx exception carries the request, i.e. the Authorization header; `from None` only
    # hides it from tracebacks, anything walking __context__ would still find it.
    e = Ebay(search_script=script)
    with pytest.raises(EbayError) as info:
        search(e)
    assert _chain(info.value) == [info.value]


# --- cross-module: how the rest of the tracker treats the marketplace store -----------------------------------------

def test_a_full_retail_scrape_leaves_the_marketplace_store_alone(tmp_path, monkeypatch):
    from disctracker import cli, shopify
    from disctracker.models import RawProduct, RawVariant

    dbfile = tmp_path / "discs.db"
    conn = db.connect(dbfile)
    run_collect(conn, Ebay({"q": [summary(1), summary(2)]}), [("k", "q")], "2026-01-01")
    conn.close()
    stores = tmp_path / "stores.json"
    stores.write_text(json.dumps({"stores": [{"id": "shop", "name": "Shop", "base_url": "https://shop.example",
                                              "currency": "USD", "enabled": True}]}), encoding="utf-8")
    monkeypatch.setattr(shopify, "fetch_store", lambda s, delay=1.0, **kw: [
        RawProduct(1, "h", "Innova Star Destroyer", variants=[RawVariant(1, price_cents=1999, available=True)])])
    assert cli.main(["--db", str(dbfile), "--stores", str(stores), "scrape"]) == 0
    conn = db.connect(dbfile)
    assert table(conn, "SELECT gone FROM listings WHERE store_id='ebay'") == [(0,), (0,)]
