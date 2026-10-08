"""Offline tests for disctracker.shopify. A fake store sits behind
httpx.MockTransport; nothing here touches the network or really sleeps."""
from __future__ import annotations

import email.utils
import inspect
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from disctracker import shopify
from disctracker.models import RawProduct, RawVariant

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://shop.example"
STORE = {"id": "shop", "name": "Shop", "base_url": BASE, "currency": "USD", "enabled": True}
REAL_SLEEP = shopify._sleep


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def product(i: int, *, tags=("Innova",), price="19.99", compare_at=None, **extra) -> dict:
    """A minimal Shopify-shaped product with one variant."""
    p = {
        "id": 1000 + i, "title": f"Disc {i}", "handle": f"disc-{i}", "vendor": "Innova",
        "product_type": "Distance Driver", "tags": list(tags) if isinstance(tags, tuple) else tags,
        "variants": [{"id": 5000 + i, "title": "Default Title", "sku": f"SKU-{i}",
                      "available": True, "price": price, "compare_at_price": compare_at,
                      "grams": 0}],
    }
    p.update(extra)
    return p


class Shop:
    """Fake store behind httpx.MockTransport that records every request.

    robots: None -> 404; str -> 200 body; int -> that status; Exception -> raised.
    respond(request, n): optionally answer the n-th (1-based) products request;
    return None to fall through to normal paging over `catalog`.
    """

    def __init__(self, catalog=(), robots=None, respond=None):
        self.catalog = list(catalog)
        self.robots = robots
        self.respond = respond
        self.requests: list[httpx.Request] = []
        self.client = httpx.Client(transport=httpx.MockTransport(self._handle))

    @property
    def robots_requests(self):
        return [r for r in self.requests if r.url.path == "/robots.txt"]

    @property
    def product_requests(self):
        return [r for r in self.requests if r.url.path.endswith("products.json")]

    @property
    def pages(self) -> list[int]:
        return [int(r.url.params["page"]) for r in self.product_requests]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/robots.txt":
            if self.robots is None:
                return httpx.Response(404)
            if isinstance(self.robots, Exception):
                raise self.robots
            if isinstance(self.robots, int):
                return httpx.Response(self.robots)
            return httpx.Response(200, text=self.robots)
        if self.respond is not None:
            answer = self.respond(request, len(self.product_requests))
            if answer is not None:
                return answer
        page, limit = int(request.url.params["page"]), int(request.url.params["limit"])
        return httpx.Response(200, json={"products": self.catalog[(page - 1) * limit:page * limit]})


def fetch(shop: Shop, store: dict = STORE, **kw) -> list[RawProduct]:
    kw.setdefault("delay", 0)
    return shopify.fetch_store(store, client=shop.client, **kw)


@pytest.fixture(autouse=True)
def sleeps(monkeypatch) -> list[float]:
    """Replace the sleeper for every test and expose what it was asked to wait."""
    calls: list[float] = []
    monkeypatch.setattr(shopify, "_sleep", calls.append)
    return calls


# --- contract ------------------------------------------------------------------

def test_signature_and_constants_match_design():
    params = inspect.signature(shopify.fetch_store).parameters
    assert list(params) == ["store", "client", "delay", "max_pages", "respect_robots"]
    assert params["client"].default is None
    assert params["delay"].default == 1.0
    assert params["max_pages"].default == 100
    assert params["respect_robots"].default is True
    assert list(inspect.signature(shopify.check_store).parameters) == ["store", "client"]
    assert issubclass(shopify.StoreError, Exception)
    assert shopify.USER_AGENT == (
        "disc-tracker/1.0 (+https://github.com/mrcactusjones/mrcactusjones.github.io)")
    assert shopify.PAGE_SIZE == 250 and shopify.TIMEOUT == 30.0 and shopify.MAX_RETRIES == 3


# --- parsing -------------------------------------------------------------------

def test_parses_fixture_products():
    shop = Shop(load_fixture("shopify_products.json")["products"])
    destroyer, buzzz, raven = fetch(shop)

    assert destroyer.product_id == 6789012345671
    assert destroyer.handle == "innova-star-destroyer-175g-ricky-wysocki-tour-series-2015-oop-9-10"
    assert destroyer.title == "Innova Star Destroyer 175g Ricky Wysocki Tour Series 2015 OOP 9/10"
    assert destroyer.url == f"{BASE}/products/{destroyer.handle}"
    assert (destroyer.vendor, destroyer.product_type) == ("Innova", "Distance Driver")
    assert destroyer.tags == ["Distance Driver", "Innova", "OOP", "Tour Series", "Used"]
    assert destroyer.variants == [
        RawVariant(variant_id=39000000000001, title="175g", sku="INN-DST-175-A", price_cents=2499,
                   compare_at_cents=2999, available=True, grams=175),
        RawVariant(variant_id=39000000000002, title="173g", sku="", price_cents=2499,
                   compare_at_cents=None, available=False, grams=None),
    ]

    assert buzzz.tags == ["Discraft", "ESP", "Midrange", "Buzzz"]
    assert buzzz.variants == [
        RawVariant(variant_id=39000000000003, title="Default Title", sku="DIS-BZZ-ESP",
                   price_cents=1799, compare_at_cents=None, available=True, grams=177)]

    assert raven.title == "Latitude 64 Opto Ráven – 173g"
    assert raven.product_type == "" and raven.tags == []
    assert raven.variants[0].sku == "" and raven.variants[0].price_cents == 1950


def test_request_shape_and_politeness_headers():
    shop = Shop([product(1)])
    fetch(shop)

    robots, page = shop.requests
    assert str(robots.url) == f"{BASE}/robots.txt"
    assert page.url.path == "/products.json"
    assert page.url.query == b"limit=250&page=1"
    for req in shop.requests:
        assert req.headers["user-agent"] == shopify.USER_AGENT
        assert req.extensions["timeout"] == {"connect": 30.0, "read": 30.0, "write": 30.0,
                                             "pool": 30.0}


def test_trailing_slash_in_base_url_is_normalised():
    shop = Shop([product(1)])
    (p,) = fetch(shop, {**STORE, "base_url": BASE + "/"})
    assert p.url == f"{BASE}/products/disc-1"
    assert str(shop.requests[0].url) == f"{BASE}/robots.txt"


@pytest.mark.parametrize("tags, expected", [
    (["Putter", " Innova ", "", "  ", "OOP"], ["Putter", "Innova", "OOP"]),
    ("Putter, Innova ,,OOP,", ["Putter", "Innova", "OOP"]),
    ("single", ["single"]),
    ("", []),
    ("  ,  ", []),
    ([], []),
    (None, []),
    ([None, 7, "x"], ["7", "x"]),
])
def test_tags_normalised_to_stripped_list(tags, expected):
    shop = Shop([product(1, tags=tags)])
    assert fetch(shop)[0].tags == expected


@pytest.mark.parametrize("value, expected", [
    ("19.99", 1999), ("0.29", 29), ("1.15", 115), ("0.10", 10), ("0.00", 0), ("0", 0),
    ("1234", 123400), ("1234.5", 123450), (" 7.50 ", 750), ("1.005", 101), ("1.004", 100),
    ("99999.99", 9999999), (20, 2000), (Decimal("19.99"), 1999), (19.99, 1999), (1.15, 115),
    (None, None), ("", None), ("   ", None),
])
def test_to_cents_is_exact(value, expected):
    assert shopify._to_cents(value) == expected


@pytest.mark.parametrize("value", ["abc", "-1.00", "NaN", "Infinity", "1e400", "$5", "1,50", True])
def test_to_cents_rejects_garbage(value):
    with pytest.raises(ValueError):
        shopify._to_cents(value)


def test_json_number_prices_do_not_go_through_float():
    body = (b'{"products":[{"id":1,"handle":"h","title":"t","variants":'
            b'[{"id":2,"price":1.15,"compare_at_price":0.29,"available":true}]}]}')
    shop = Shop(respond=lambda req, n: httpx.Response(200, content=body))
    (v,) = fetch(shop)[0].variants
    assert (v.price_cents, v.compare_at_cents) == (115, 29)


@pytest.mark.parametrize("compare_at, expected", [
    (None, None), ("0.00", None), ("0", None), ("", None), ("15.00", 1500), ("24.99", 2499)])
def test_compare_at_price(compare_at, expected):
    shop = Shop([product(1, price="12.00", compare_at=compare_at)])
    assert fetch(shop)[0].variants[0].compare_at_cents == expected


def test_compare_at_missing_key_is_none():
    p = product(1)
    del p["variants"][0]["compare_at_price"]
    assert fetch(Shop([p]))[0].variants[0].compare_at_cents is None


def test_variant_available_and_grams():
    p = product(1)
    p["variants"] = [
        {"id": 1, "price": "1.00", "available": True, "grams": 175},
        {"id": 2, "price": "1.00", "available": False, "grams": 0},
        {"id": 3, "price": "1.00", "grams": None},  # no `available` key
        {"id": 4, "price": "1.00", "available": True, "grams": "oops"},
    ]
    v = fetch(Shop([p]))[0].variants
    assert [(x.available, x.grams) for x in v] == [(True, 175), (False, None), (False, None),
                                                    (True, None)]


def test_product_without_variants_is_kept():
    (p,) = fetch(Shop([product(1, variants=[])]))
    assert p.variants == []


@pytest.mark.parametrize("mutate", [
    lambda p: p.pop("id"),
    lambda p: p.pop("handle"),
    lambda p: p.update(handle=""),
    lambda p: p.update(id="not-a-number"),
    lambda p: p.update(id=True),
    lambda p: p.update(variants="nope"),
    lambda p: p["variants"][0].pop("price"),
    lambda p: p["variants"][0].update(price=None),
    lambda p: p["variants"][0].update(price="free"),
    lambda p: p["variants"][0].update(price="-4.00"),
    lambda p: p["variants"][0].update(compare_at_price="abc"),
    lambda p: p["variants"][0].pop("id"),
    lambda p: p["variants"].append("not an object"),
])
def test_malformed_product_raises_instead_of_being_skipped(mutate):
    bad = product(2)
    mutate(bad)
    shop = Shop([product(1), bad, product(3)])
    with pytest.raises(shopify.StoreError, match="malformed product"):
        fetch(shop)


def test_non_object_product_raises():
    with pytest.raises(shopify.StoreError, match="malformed product"):
        fetch(Shop([product(1), "oops"]))


# --- pagination ----------------------------------------------------------------

@pytest.mark.parametrize("count, pages", [
    (0, [1]), (1, [1]), (249, [1]), (250, [1, 2]), (251, [1, 2]), (499, [1, 2]),
    (500, [1, 2, 3]), (517, [1, 2, 3]),
])
def test_stop_condition_is_a_short_or_empty_page(count, pages):
    shop = Shop([product(i) for i in range(count)])
    products = fetch(shop)
    assert shop.pages == pages
    assert [p.product_id for p in products] == [1000 + i for i in range(count)]


def test_requests_use_limit_250_and_incrementing_page():
    shop = Shop([product(i) for i in range(600)])
    fetch(shop)
    assert [r.url.query for r in shop.product_requests] == [
        b"limit=250&page=1", b"limit=250&page=2", b"limit=250&page=3"]


def test_failure_on_a_later_page_never_returns_a_partial_list():
    shop = Shop([product(i) for i in range(600)],
                respond=lambda req, n: httpx.Response(500) if n >= 2 else None)
    with pytest.raises(shopify.StoreError, match="HTTP 500"):
        fetch(shop)
    assert shop.pages == [1, 2, 2, 2, 2]


@pytest.mark.parametrize("answer", [
    lambda: httpx.Response(404),
    lambda: httpx.Response(200, text="<html>oops</html>"),
    lambda: httpx.Response(200, json={"errors": "x"}),
])
def test_bad_page_two_aborts_the_whole_fetch(answer):
    shop = Shop([product(i) for i in range(300)],
                respond=lambda req, n: answer() if n == 2 else None)
    with pytest.raises(shopify.StoreError):
        fetch(shop)


def test_repeating_page_is_detected_as_a_loop():
    first_page = [product(i) for i in range(250)]
    shop = Shop(respond=lambda req, n: httpx.Response(200, json={"products": first_page}))
    with pytest.raises(shopify.StoreError, match="ignore"):
        fetch(shop)
    assert shop.pages == [1, 2]  # gave up as soon as page 2 had nothing new


def test_overlap_from_a_shifting_catalogue_is_deduplicated_not_an_error():
    pages = {
        1: [product(i) for i in range(250)],
        2: [product(i) for i in range(1, 251)],  # 249 repeats + one new product
        3: [],
    }
    shop = Shop(respond=lambda req, n: httpx.Response(200, json={"products": pages[n]}))
    products = fetch(shop)
    ids = [p.product_id for p in products]
    assert ids == [1000 + i for i in range(251)]
    assert len(set(ids)) == len(ids)


def test_max_pages_reached_while_still_full_raises():
    shop = Shop([product(i) for i in range(1000)])
    with pytest.raises(shopify.StoreError, match="max_pages"):
        fetch(shop, max_pages=3)
    assert shop.pages == [1, 2, 3]  # no 4th request


def test_max_pages_is_fine_when_the_last_allowed_page_is_short():
    shop = Shop([product(i) for i in range(700)])
    assert len(fetch(shop, max_pages=3)) == 700
    assert shop.pages == [1, 2, 3]


def test_max_pages_exactly_full_cannot_be_confirmed_complete():
    shop = Shop([product(i) for i in range(500)])
    with pytest.raises(shopify.StoreError, match="max_pages"):
        fetch(shop, max_pages=2)


def test_max_pages_must_be_positive():
    with pytest.raises(ValueError):
        fetch(Shop([product(1)]), max_pages=0)


# --- collections ---------------------------------------------------------------

def test_collection_url():
    shop = Shop([product(i) for i in range(300)])
    products = fetch(shop, {**STORE, "collection": "all-discs"})
    assert len(products) == 300
    assert [r.url.path for r in shop.product_requests] == [
        "/collections/all-discs/products.json"] * 2
    assert [r.url.query for r in shop.product_requests] == [
        b"limit=250&page=1", b"limit=250&page=2"]
    assert products[0].url == f"{BASE}/products/disc-0"  # product URLs ignore the collection


@pytest.mark.parametrize("collection", [None, "", "  "])
def test_blank_collection_means_whole_store(collection):
    shop = Shop([product(1)])
    fetch(shop, {**STORE, "collection": collection})
    assert shop.product_requests[0].url.path == "/products.json"


def test_collection_slashes_are_stripped():
    shop = Shop([product(1)])
    fetch(shop, {**STORE, "collection": "/discs/"})
    assert shop.product_requests[0].url.path == "/collections/discs/products.json"


# --- retries and backoff -------------------------------------------------------

def test_429_honours_retry_after_seconds(sleeps):
    shop = Shop([product(1)], respond=lambda req, n: (
        httpx.Response(429, headers={"Retry-After": "7"}) if n == 1 else None))
    assert len(fetch(shop)) == 1
    assert sleeps == [7.0]
    assert shop.pages == [1, 1]


def test_429_without_retry_after_backs_off(sleeps):
    shop = Shop([product(1)], respond=lambda req, n: httpx.Response(429) if n == 1 else None)
    assert len(fetch(shop)) == 1
    assert sleeps == [shopify.BACKOFF_BASE]


def test_429_with_unusable_retry_after_backs_off(sleeps):
    shop = Shop([product(1)], respond=lambda req, n: (
        httpx.Response(429, headers={"Retry-After": "soon"}) if n == 1 else None))
    fetch(shop)
    assert sleeps == [shopify.BACKOFF_BASE]


def test_retry_after_zero_retries_immediately(sleeps):
    shop = Shop([product(1)], respond=lambda req, n: (
        httpx.Response(429, headers={"Retry-After": "0"}) if n == 1 else None))
    fetch(shop)
    assert sleeps == []
    assert shop.pages == [1, 1]


def test_retry_after_http_date(sleeps):
    when = email.utils.format_datetime(
        datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True)
    shop = Shop([product(1)], respond=lambda req, n: (
        httpx.Response(429, headers={"Retry-After": when}) if n == 1 else None))
    fetch(shop)
    (waited,) = sleeps
    assert 25 < waited <= 30


def test_503_retry_after_is_honoured_too(sleeps):
    shop = Shop([product(1)], respond=lambda req, n: (
        httpx.Response(503, headers={"Retry-After": "4"}) if n == 1 else None))
    fetch(shop)
    assert sleeps == [4.0]


def test_retry_after_longer_than_the_cap_is_a_failure(sleeps):
    shop = Shop([product(1)], respond=lambda req, n: httpx.Response(
        429, headers={"Retry-After": str(int(shopify.MAX_RETRY_AFTER) + 1)}))
    with pytest.raises(shopify.StoreError, match="Retry-After"):
        fetch(shop)
    assert sleeps == [] and len(shop.product_requests) == 1


def test_5xx_is_retried_with_exponential_backoff_then_succeeds(sleeps):
    statuses = {1: 502, 2: 503}
    shop = Shop([product(1)], respond=lambda req, n: (
        httpx.Response(statuses[n]) if n in statuses else None))
    assert len(fetch(shop)) == 1
    assert sleeps == [shopify.BACKOFF_BASE, shopify.BACKOFF_BASE * 2]
    assert shop.pages == [1, 1, 1]


def test_retry_exhaustion_raises_after_three_retries(sleeps):
    shop = Shop(respond=lambda req, n: httpx.Response(500))
    with pytest.raises(shopify.StoreError, match="failed after 3 retries: HTTP 500"):
        fetch(shop)
    assert len(shop.product_requests) == 1 + shopify.MAX_RETRIES
    b = shopify.BACKOFF_BASE
    assert sleeps == [b, b * 2, b * 4]  # nothing slept after the final failure


def test_429_exhaustion_raises(sleeps):
    shop = Shop(respond=lambda req, n: httpx.Response(429, headers={"Retry-After": "1"}))
    with pytest.raises(shopify.StoreError, match="HTTP 429"):
        fetch(shop)
    assert len(shop.product_requests) == 4 and sleeps == [1.0, 1.0, 1.0]


@pytest.mark.parametrize("exc", [
    httpx.ReadTimeout("slow"), httpx.ConnectTimeout("slow"), httpx.ConnectError("refused"),
    httpx.RemoteProtocolError("dropped"),
])
def test_transport_errors_are_retried(exc, sleeps):
    shop = Shop([product(1)], respond=lambda req, n: (_ for _ in ()).throw(exc) if n == 1 else None)
    assert len(fetch(shop)) == 1
    assert sleeps == [shopify.BACKOFF_BASE]


def test_timeout_exhaustion_raises(sleeps):
    def always_time_out(req, n):
        raise httpx.ReadTimeout("slow")

    shop = Shop(respond=always_time_out)
    with pytest.raises(shopify.StoreError, match="ReadTimeout"):
        fetch(shop)
    assert len(shop.product_requests) == 4 and len(sleeps) == 3


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410])
def test_client_errors_fail_fast_without_retry(status, sleeps):
    shop = Shop(respond=lambda req, n: httpx.Response(status))
    with pytest.raises(shopify.StoreError, match=f"HTTP {status}"):
        fetch(shop)
    assert len(shop.product_requests) == 1 and sleeps == []


def test_redirects_are_followed_even_if_the_injected_client_does_not():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "shop.example":
            return httpx.Response(301, headers={
                "Location": str(request.url.copy_with(host="www.shop.example"))})
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, json={"products": [product(1)]})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    assert len(shopify.fetch_store(STORE, client=client, delay=0)) == 1


@pytest.mark.parametrize("base_url", ["shop.example", "ftp://shop.example", "//shop.example"])
def test_base_url_must_be_http(base_url):
    shop = Shop([product(1)])
    with pytest.raises(shopify.StoreError, match="http"):
        fetch(shop, {**STORE, "base_url": base_url})
    assert shop.requests == []


@pytest.mark.parametrize("header, expected", [
    ("5", 5.0), ("1.5", 1.5), (" 7 ", 7.0), ("0", 0.0),
    ("-3", None), ("nan", None), ("inf", None), ("soon", None), ("", None),
    ("Wed, 21 Oct 2015 07:28:00 GMT", 0.0),  # a date in the past: retry now
])
def test_retry_after_parsing(header, expected):
    assert shopify._retry_after(httpx.Response(429, headers={"Retry-After": header})) == expected


def test_retry_after_absent():
    assert shopify._retry_after(httpx.Response(429)) is None


# --- bad payloads --------------------------------------------------------------

@pytest.mark.parametrize("response", [
    httpx.Response(200, text="<html><body>Opening soon</body></html>",
                   headers={"content-type": "text/html"}),
    httpx.Response(200, text='{"products": [{"id": 1,'),
    httpx.Response(200, content=b""),
    httpx.Response(200, content=b"\xff\xfe\x00 not json"),
    httpx.Response(204),
])
def test_invalid_json_raises_without_retry(response, sleeps):
    shop = Shop(respond=lambda req, n: response)
    with pytest.raises(shopify.StoreError, match="not valid JSON"):
        fetch(shop)
    assert len(shop.product_requests) == 1 and sleeps == []


def test_invalid_json_error_names_the_content_type():
    shop = Shop(respond=lambda req, n: httpx.Response(
        200, text="<html></html>", headers={"content-type": "text/html; charset=utf-8"}))
    with pytest.raises(shopify.StoreError, match="text/html"):
        fetch(shop)


@pytest.mark.parametrize("payload", [
    load_fixture("shopify_not_products.json"), [], [{"id": 1}], {"products": None},
    {"products": {"id": 1}}, "products", 3,
])
def test_valid_json_of_the_wrong_shape_raises(payload):
    shop = Shop(respond=lambda req, n: httpx.Response(200, json=payload))
    with pytest.raises(shopify.StoreError, match="no 'products' list"):
        fetch(shop)


def test_empty_catalogue_is_an_empty_list_not_an_error():
    shop = Shop(load_fixture("shopify_empty.json")["products"])
    assert fetch(shop) == []


def test_missing_base_url_raises():
    for store in ({"id": "x"}, {"id": "x", "base_url": ""}, {"id": "x", "base_url": "/"}):
        with pytest.raises(shopify.StoreError, match="base_url"):
            shopify.fetch_store(store, client=Shop().client, delay=0)


# --- robots.txt ----------------------------------------------------------------

@pytest.mark.parametrize("robots", [
    "User-agent: *\nDisallow: /products.json\n",
    "User-agent: *\nDisallow: /\n",
    "User-agent: disc-tracker\nDisallow: /\n",
    "User-agent: *\nDisallow: /products\n",
    "User-agent: *\nAllow: /\n\nUser-agent: disc-tracker\nDisallow: /products.json\n",
])
def test_robots_disallow_raises_before_any_product_request(robots):
    shop = Shop([product(1)], robots=robots)
    with pytest.raises(shopify.StoreError, match="robots.txt"):
        fetch(shop)
    assert shop.product_requests == []


@pytest.mark.parametrize("robots", [
    None,  # 404: no robots.txt
    500, 403, 429,  # unreadable
    httpx.ConnectError("refused"), httpx.ReadTimeout("slow"),
    "", "<html><body>Not a robots file</body></html>",
    "User-agent: *\nDisallow:\n",
    "User-agent: *\nDisallow: /admin\nDisallow: /cart\nDisallow: /search\n\n"
    "User-agent: Googlebot\nDisallow: /\n",
    "User-agent: *\nDisallow: /\n\nUser-agent: disc-tracker\nAllow: /\n",
    "User-agent: SomeOtherBot\nDisallow: /products.json\n",
])
def test_robots_missing_unreadable_or_not_applicable_allows(robots):
    shop = Shop([product(1)], robots=robots)
    assert len(fetch(shop)) == 1


def test_robots_is_fetched_once_per_store_not_per_page():
    shop = Shop([product(i) for i in range(600)], robots="User-agent: *\nDisallow: /search\n")
    fetch(shop)
    assert len(shop.robots_requests) == 1 and len(shop.product_requests) == 3


def test_respect_robots_false_skips_the_check_entirely():
    shop = Shop([product(1)], robots="User-agent: *\nDisallow: /\n")
    assert len(fetch(shop, respect_robots=False)) == 1
    assert shop.robots_requests == []


def test_robots_is_checked_against_the_collection_path():
    robots = "User-agent: *\nDisallow: /collections/\n"
    with pytest.raises(shopify.StoreError, match="robots.txt"):
        fetch(Shop([product(1)], robots=robots), {**STORE, "collection": "all-discs"})
    assert len(fetch(Shop([product(1)], robots=robots))) == 1  # whole-store path is fine


def test_robots_crawl_delay_raises_the_delay(sleeps):
    shop = Shop([product(i) for i in range(300)], robots="User-agent: *\nCrawl-delay: 5\n")
    fetch(shop, delay=1)
    assert sleeps == [5.0]


def test_robots_crawl_delay_never_lowers_the_delay_and_is_capped(sleeps):
    shop = Shop([product(i) for i in range(300)], robots="User-agent: *\nCrawl-delay: 2\n")
    fetch(shop, delay=10)
    assert sleeps == [10.0]
    sleeps.clear()
    shop = Shop([product(i) for i in range(300)], robots="User-agent: *\nCrawl-delay: 9999\n")
    fetch(shop, delay=1)
    assert sleeps == [shopify.MAX_CRAWL_DELAY]


# --- delay ---------------------------------------------------------------------

def test_delay_is_slept_between_pages_only(monkeypatch):
    log: list[tuple] = []
    monkeypatch.setattr(shopify, "_sleep", lambda s: log.append(("sleep", s)))
    shop = Shop([product(i) for i in range(517)],
                respond=lambda req, n: log.append(("get", n)))
    fetch(shop, delay=2.5)
    assert log == [("get", 1), ("sleep", 2.5), ("get", 2), ("sleep", 2.5), ("get", 3)]


def test_default_delay_is_one_second(sleeps):
    shop = Shop([product(i) for i in range(300)])
    shopify.fetch_store(STORE, client=shop.client)
    assert sleeps == [1.0]


@pytest.mark.parametrize("count", [0, 10])
def test_single_page_never_sleeps(count, sleeps):
    fetch(Shop([product(i) for i in range(count)]), delay=3)
    assert sleeps == []


def test_zero_delay_never_sleeps(sleeps):
    fetch(Shop([product(i) for i in range(517)]), delay=0)
    assert sleeps == []


def test_sleeping_can_also_be_patched_through_time_sleep(monkeypatch):
    monkeypatch.setattr(shopify, "_sleep", REAL_SLEEP)  # undo the autouse patch
    waited: list[float] = []
    monkeypatch.setattr(shopify.time, "sleep", waited.append)
    fetch(Shop([product(i) for i in range(517)]), delay=4)
    assert waited == [4, 4]


# --- client ownership ----------------------------------------------------------

def test_injected_client_is_left_open():
    shop = Shop([product(1)])
    fetch(shop)
    assert not shop.client.is_closed


def test_own_client_is_configured_and_closed(monkeypatch):
    made: list[tuple[httpx.Client, dict]] = []
    real_client = httpx.Client
    shop = Shop([product(1)])

    def factory(**kwargs):
        client = real_client(transport=httpx.MockTransport(shop._handle), **kwargs)
        made.append((client, kwargs))
        return client

    monkeypatch.setattr(httpx, "Client", factory)
    assert len(shopify.fetch_store(STORE, delay=0)) == 1
    ((client, kwargs),) = made
    assert client.is_closed
    assert kwargs["timeout"] == 30.0 and kwargs["follow_redirects"] is True


# --- check_store ---------------------------------------------------------------

def test_check_store_ok():
    shop = Shop(load_fixture("shopify_products.json")["products"])
    res = shopify.check_store(STORE, client=shop.client)
    assert res["ok"] is True and res["sample_count"] == 3
    assert f"{BASE}/products.json" in res["detail"] and "Destroyer" in res["detail"]
    assert set(res) == {"ok", "detail", "sample_count"}
    assert len(shop.robots_requests) == 1
    (req,) = shop.product_requests
    assert req.url.params["page"] == "1" and req.url.params["limit"] == str(shopify.CHECK_SAMPLE)


def test_check_store_samples_only_one_small_page():
    shop = Shop([product(i) for i in range(400)])
    res = shopify.check_store(STORE, client=shop.client)
    assert res["ok"] and res["sample_count"] == shopify.CHECK_SAMPLE
    assert len(shop.product_requests) == 1


def test_check_store_uses_the_collection():
    shop = Shop([product(1)])
    res = shopify.check_store({**STORE, "collection": "all-discs"}, client=shop.client)
    assert res["ok"]
    assert shop.product_requests[0].url.path == "/collections/all-discs/products.json"
    assert "/collections/all-discs/products.json" in res["detail"]


def test_check_store_http_error_is_reported_not_raised():
    shop = Shop(respond=lambda req, n: httpx.Response(404))
    res = shopify.check_store(STORE, client=shop.client)
    assert res["ok"] is False and res["sample_count"] == 0 and "HTTP 404" in res["detail"]


def test_check_store_retries_then_reports_failure(sleeps):
    shop = Shop(respond=lambda req, n: httpx.Response(503))
    res = shopify.check_store(STORE, client=shop.client)
    assert res["ok"] is False and "HTTP 503" in res["detail"]
    assert len(sleeps) == shopify.MAX_RETRIES


def test_check_store_not_json_is_reported():
    shop = Shop(respond=lambda req, n: httpx.Response(200, text="<html>password</html>"))
    res = shopify.check_store(STORE, client=shop.client)
    assert res["ok"] is False and "not valid JSON" in res["detail"]


def test_check_store_zero_products_is_not_ok():
    res = shopify.check_store(STORE, client=Shop([]).client)
    assert res["ok"] is False and res["sample_count"] == 0 and "no products" in res["detail"]


def test_check_store_malformed_product_is_not_ok():
    bad = product(1)
    del bad["handle"]
    res = shopify.check_store(STORE, client=Shop([bad]).client)
    assert res["ok"] is False and "malformed product" in res["detail"]


def test_check_store_respects_robots():
    shop = Shop([product(1)], robots="User-agent: *\nDisallow: /products.json\n")
    res = shopify.check_store(STORE, client=shop.client)
    assert res["ok"] is False and "robots.txt" in res["detail"]
    assert shop.product_requests == []


def test_check_store_without_base_url_is_not_ok():
    res = shopify.check_store({"id": "x"}, client=Shop().client)
    assert res["ok"] is False and "base_url" in res["detail"]


# === adversarial review: regression tests ========================================
# Each test below failed against the first implementation (see the comment above it).

# --- robots.txt: RFC 9309 semantics, identical on every Python version -------------
# urllib.robotparser differs by version (3.12 has no wildcards, "first rule wins"
# instead of "longest rule wins", only the first `*` group, a bare ValueError on
# `Crawl-delay: ²`, no BOM handling, and it was only asked about the bare path).

def robots_allows(robots: str, store: dict = STORE, **kw) -> bool:
    shop = Shop([product(i) for i in range(kw.pop("count", 1))], robots=robots)
    try:
        fetch(shop, store, **kw)
    except shopify.StoreError as exc:
        assert "robots.txt" in str(exc)
        return False
    return True


@pytest.mark.parametrize("robots, allowed", [
    ("User-agent: *\nAllow: /\nDisallow: /products.json\n", False),  # longer rule wins
    ("User-agent: *\nDisallow: /products.json\nAllow: /\n", False),
    ("User-agent: *\nDisallow: /\nAllow: /products.json\n", True),
    ("User-agent: *\nAllow: /products.json\nDisallow: /\n", True),
    ("User-agent: *\nDisallow: /products.json\nAllow: /products.json\n", True),  # tie -> allow
    ("User-agent: *\nAllow: /products.json\nDisallow: /products\n", True),
    ("User-agent: *\nAllow: /prod\nDisallow: /products.json\n", False),
])
def test_robots_longest_matching_rule_wins(robots, allowed):
    assert robots_allows(robots) is allowed


@pytest.mark.parametrize("robots, allowed", [
    ("User-agent: *\nDisallow: /*.json\n", False),
    ("User-agent: *\nDisallow: /*.json$\n", False),
    ("User-agent: *\nDisallow: /products.json$\n", False),  # the bare path is what DESIGN names
    ("User-agent: *\nDisallow: /pro*cts.js*\n", False),
    ("User-agent: *\nDisallow: /*\n", False),
    ("User-agent: *\nDisallow: /*.xml$\n", True),
    ("User-agent: *\nDisallow: /*products.jsonx\n", True),
    ("User-agent: *\nDisallow: /a*b*c\n", True),
    ("User-agent: *\nDisallow: /*.json\nAllow: /products.json\n", True),
])
def test_robots_wildcards(robots, allowed):
    assert robots_allows(robots) is allowed


@pytest.mark.parametrize("robots, allowed", [
    ("User-agent: *\nDisallow: /products.json?limit=\n", False),
    ("User-agent: *\nDisallow: /*?*limit=250\n", False),
    ("User-agent: *\nDisallow: /*?limit=250&page=1$\n", False),
    ("User-agent: *\nDisallow: /products.json?page=\n", True),
    ("User-agent: *\nDisallow: /*?*oseid=\n", True),
])
def test_robots_rules_that_mention_the_query_string_apply(robots, allowed):
    assert robots_allows(robots) is allowed


def test_robots_rule_on_a_later_page_aborts_the_fetch_without_a_partial_list():
    shop = Shop([product(i) for i in range(300)],
                robots="User-agent: *\nDisallow: /products.json?limit=250&page=2\n")
    with pytest.raises(shopify.StoreError, match="robots.txt"):
        fetch(shop)
    assert shop.pages == [1]


@pytest.mark.parametrize("robots", [
    "﻿User-agent: *\nDisallow: /products.json\n",  # UTF-8 byte order mark
    "User-agent: *\r\nDisallow: /products.json\r\n",
    "User-agent: *\rDisallow: /products.json\r",
    "USER-AGENT: *\nDISALLOW: /products.json\n",
    "User-agent : *\nDisallow : /products.json  # nope\n",
    "User-agent: *\nSitemap: https://shop.example/sitemap.xml\nDisallow: /products.json\n",
    "User-agent: Googlebot\nUser-agent: disc-tracker\nDisallow: /products.json\n",
    "User-agent: Disc-Tracker\nDisallow: /products.json\n",
    "User-agent: disc-tracker/1.0\nDisallow: /products.json\n",
    "User-agent: Googlebot\nDisallow: /x\nUser-agent: disc-tracker\nDisallow: /products.json\n",
    "User-agent: *\nDisallow: /admin\n\nUser-agent: *\nDisallow: /products.json\n",  # groups merge
    "User-agent: disc-tracker\nDisallow: /admin\n\nUser-agent: disc-tracker\n"
    "Disallow: /products.json\n",
])
def test_robots_syntax_variants_are_honoured(robots):
    assert robots_allows(robots) is False


@pytest.mark.parametrize("robots", [
    "User-agent: *\nDisallow: /products.json\n\nUser-agent: disc-tracker\nDisallow: /admin\n",
    "Disallow: /products.json\n",  # a rule before any User-agent line belongs to nobody
    "User-agent: *\nDisallow: /products.json".replace("Disallow", "Disalow"),
    "# User-agent: *\n# Disallow: /products.json\n",
])
def test_robots_rules_that_do_not_apply_to_us_are_ignored(robots):
    assert robots_allows(robots) is True


@pytest.mark.parametrize("value", ["²", "٣", "5s", "-5", "1e3", "nan", "inf", "", "1/2"])
def test_robots_garbage_crawl_delay_is_ignored_not_a_crash(value, sleeps):
    robots = f"User-agent: *\nCrawl-delay: {value}\nDisallow: /search\n"
    shop = Shop([product(i) for i in range(300)], robots=robots)
    assert len(fetch(shop, delay=1)) == 300
    assert sleeps == [1.0]
    assert shopify.check_store(STORE, client=Shop([product(1)], robots=robots).client)["ok"]


def test_robots_garbage_crawl_delay_does_not_hide_a_disallow():
    robots = "User-agent: *\nCrawl-delay: ²\nRequest-rate: ²/5\nDisallow: /products.json\n"
    assert robots_allows(robots) is False
    res = shopify.check_store(STORE, client=Shop([product(1)], robots=robots).client)
    assert res["ok"] is False and "robots.txt" in res["detail"]


def test_robots_fractional_crawl_delay_and_group_selection(sleeps):
    robots = ("User-agent: *\nCrawl-delay: 9\n\nUser-agent: disc-tracker\nCrawl-delay: 2.5\n")
    fetch(Shop([product(i) for i in range(300)], robots=robots), delay=1)
    assert sleeps == [2.5]  # the specific group replaces the `*` group


def test_robots_is_matched_case_sensitively_on_the_path():
    assert robots_allows("User-agent: *\nDisallow: /PRODUCTS.JSON\n") is True


def test_robots_collection_path_with_percent_encoding_and_unicode_rules():
    store = {**STORE, "collection": "café discs"}
    assert robots_allows("User-agent: *\nDisallow: /collections/café%20discs/\n", store) is False
    assert robots_allows("User-agent: *\nDisallow: /collections/caf%C3%A9%20discs/\n", store) is False
    assert robots_allows("User-agent: *\nDisallow: /collections/other/\n", store) is True


def test_robots_huge_pathological_file_is_handled_quickly():
    rules = "".join(f"Disallow: /{'*p' * 40}x{i}\n" for i in range(2000))
    assert robots_allows("User-agent: *\n" + rules) is True
    assert robots_allows("User-agent: *\n" + rules + "Disallow: /*s*o*n*\n") is False


def test_robots_rule_without_a_leading_slash_is_read_as_rooted():
    assert robots_allows("User-agent: *\nDisallow: products.json\n") is False


# --- text fields: whatever the server sends must be safe to store ------------------
# A lone surrogate made sqlite3 raise UnicodeEncodeError inside db.record_products,
# which then failed every future scrape of that store (the product never goes away);
# dict/list/Decimal values were passed through and failed the same way.

def wire_shop(*products) -> Shop:
    """A shop that sends ASCII-escaped JSON bytes, as real servers do ("\\ud83d" stays an escape;
    httpx's `json=` helper cannot even encode a lone surrogate)."""
    body = json.dumps({"products": list(products)}).encode("ascii")
    return Shop(respond=lambda req, n: httpx.Response(200, content=body))


def test_lone_surrogates_are_replaced_in_every_text_field():
    p = product(1, title="Disc \ud83d", vendor="In\ude00nova", product_type="\ud800",
                tags=["ok", "bad\udfff"])
    p["variants"][0].update(title="175\ud83dg", sku="S\udc00")
    (got,) = fetch(wire_shop(p))
    texts = [got.title, got.vendor, got.product_type, *got.tags, got.variants[0].title,
             got.variants[0].sku]
    for text in texts:
        text.encode("utf-8")  # must not raise
    assert got.title == "Disc �" and got.variants[0].sku == "S�"


def test_lone_surrogate_in_comma_separated_tags_and_handle():
    (got,) = fetch(wire_shop(product(1, tags="a, b\ud83d", handle="h\ud83d")))
    assert got.tags == ["a", "b�"]
    got.handle.encode("utf-8")
    got.url.encode("ascii")


def test_scraped_products_can_be_recorded_in_the_database():
    from disctracker import db
    p = product(1, title="Disc \ud83d é")
    p["variants"][0]["sku"] = "S\ude00"
    products = fetch(wire_shop(p))
    conn = db.connect(":memory:")
    db.upsert_store(conn, STORE)
    assert db.record_products(conn, "shop", "2026-10-08", products)["new_listings"] == 1


@pytest.mark.parametrize("field, value, expected", [
    ("title", 12345, "12345"), ("title", 1.5, "1.5"), ("vendor", 7, "7"),
    ("product_type", None, ""), ("title", None, ""),
])
def test_numeric_text_fields_become_strings(field, value, expected):
    (got,) = fetch(Shop([product(1, **{field: value})]))
    assert getattr(got, field) == expected


@pytest.mark.parametrize("field, value", [
    ("title", {"a": 1}), ("title", ["x"]), ("title", True), ("vendor", ["V"]),
    ("product_type", {"a": 1}), ("vendor", False),
])
def test_structured_text_fields_are_malformed(field, value):
    with pytest.raises(shopify.StoreError, match="malformed product"):
        fetch(Shop([product(1, **{field: value})]))


@pytest.mark.parametrize("field, value", [
    ("title", {"a": 1}), ("sku", ["x"]), ("title", True),
])
def test_structured_variant_text_fields_are_malformed(field, value):
    p = product(1)
    p["variants"][0][field] = value
    with pytest.raises(shopify.StoreError, match="malformed product"):
        fetch(Shop([p]))


def test_variant_numeric_text_fields_become_strings():
    p = product(1)
    p["variants"][0].update(title=175, sku=99)
    (got,) = fetch(Shop([p]))
    assert (got.variants[0].title, got.variants[0].sku) == ("175", "99")


# --- ids must fit SQLite integers ---------------------------------------------------
# 2**63 and above raised OverflowError inside db.record_products (after the fetch had
# been declared a success).

@pytest.mark.parametrize("bad_id", [2 ** 63, 10 ** 30, -(2 ** 63) - 1, "9" * 25])
def test_ids_outside_sqlite_integer_range_are_malformed(bad_id):
    with pytest.raises(shopify.StoreError, match="malformed product"):
        fetch(Shop([product(1, id=bad_id)]))
    p = product(1)
    p["variants"][0]["id"] = bad_id
    with pytest.raises(shopify.StoreError, match="malformed product"):
        fetch(Shop([p]))


def test_largest_valid_ids_are_accepted():
    p = product(1, id=2 ** 63 - 1)
    p["variants"][0]["id"] = str(2 ** 63 - 1)
    (got,) = fetch(Shop([p]))
    assert got.product_id == got.variants[0].variant_id == 2 ** 63 - 1


# --- availability ----------------------------------------------------------------
# bool("false") is True: a sold-out disc was recorded as in stock.

@pytest.mark.parametrize("raw, expected", [
    (True, True), (False, False), (None, False), (1, True), (0, False),
    ("true", True), ("True", True), (" TRUE ", True), ("1", True),
    ("false", False), ("False", False), ("0", False), ("no", False), ("", False),
    ("sold out", False), ([], False), ({}, False),
])
def test_variant_available_representations(raw, expected):
    p = product(1)
    p["variants"][0]["available"] = raw
    assert fetch(Shop([p]))[0].variants[0].available is expected


# --- product URLs ------------------------------------------------------------------
# The handle went into the URL verbatim, so "x?y#z", "../../cart", spaces and newlines
# produced links that do not point at the product (or are not URLs at all).

@pytest.mark.parametrize("handle, suffix", [
    ("ok-handle_1.2~x", "ok-handle_1.2~x"),
    ("a b", "a%20b"), ("x?y=1#frag", "x%3Fy%3D1%23frag"), ("../../cart", "..%2F..%2Fcart"),
    ("a/b", "a%2Fb"), ("a\nb", "a%0Ab"), ('x"y<z>', "x%22y%3Cz%3E"),
    ("javascript:alert(1)", "javascript%3Aalert%281%29"), ("ráven", "r%C3%A1ven"),
    ("%E3%82%B3", "%E3%82%B3"),  # already percent-encoded handles are kept as they are
])
def test_product_url_encodes_the_handle(handle, suffix):
    (got,) = fetch(Shop([product(1, handle=handle)]))
    assert got.url == f"{BASE}/products/{suffix}"
    assert got.handle == handle  # the stored handle stays what the store sent
    assert got.url.isascii() and not any(c.isspace() for c in got.url)


# --- base_url validation ------------------------------------------------------------
# "http://[::1" raised a bare ValueError; a query/fragment/credentials ended up inside
# every product URL (which is exported to the public site).

@pytest.mark.parametrize("base_url", [
    "https://", "http://", "https:///x", "http://[::1", "https://shop.example:99999",
    "https://shop.example:abc", "https://exa mple.com", "https://shop.example/\nx",
    "https://shop.example?x=1", "https://shop.example/?x=1", "https://shop.example#f",
    "https://user:pw@shop.example", "https://user@shop.example",
])
def test_unusable_base_urls_are_rejected_without_any_request(base_url):
    shop = Shop([product(1)])
    store = {**STORE, "base_url": base_url}
    with pytest.raises(shopify.StoreError, match="base_url"):
        fetch(shop, store)
    res = shopify.check_store(store, client=shop.client)
    assert res["ok"] is False and "base_url" in res["detail"]
    assert shop.requests == []


@pytest.mark.parametrize("base_url", [
    "https://shop.example", "https://shop.example/", "https://shop.example//", "  https://shop.example  ",
    "http://shop.example:8080", "https://shop.example/store", "http://[::1]:3000",
])
def test_ordinary_base_urls_are_accepted(base_url):
    assert shopify._base_url({**STORE, "base_url": base_url}).startswith("http")


# --- untrusted text in error messages ------------------------------------------------

def test_error_messages_do_not_echo_unbounded_server_text():
    huge = "x" * 20000
    shop = Shop(respond=lambda req, n: httpx.Response(
        200, text="<html></html>", headers={"content-type": "text/html; boundary=" + huge}))
    with pytest.raises(shopify.StoreError, match="not valid JSON") as info:
        fetch(shop)
    assert len(str(info.value)) < 400

    for bad in (product(1, title={"k": huge}), product(1, id={"k": huge}),
                product(1, variants=[{"id": 1, "price": huge}])):
        with pytest.raises(shopify.StoreError, match="malformed product") as info:
            fetch(Shop([bad]))
        assert len(str(info.value)) < 400
        assert huge not in str(info.value)


# --- numbers --------------------------------------------------------------------------
# int(Decimal("1e1000000")) took 13s (and larger exponents effectively hang the scrape);
# a JSON number like 1e-99999999999999999999 made json.loads raise decimal.InvalidOperation,
# which escaped fetch_store AND check_store as a non-StoreError.

@pytest.mark.parametrize("literal", ["1e1000000", "1e99999999", "-1e99999999", "1E+400"])
def test_absurd_grams_are_ignored_quickly(literal):
    import time
    body = ('{"products":[{"id":1,"handle":"h","title":"t","variants":'
            '[{"id":2,"price":"1.00","grams":%s}]}]}' % literal).encode()
    shop = Shop(respond=lambda req, n: httpx.Response(200, content=body))
    started = time.monotonic()
    (p,) = fetch(shop)
    assert p.variants[0].grams is None
    assert time.monotonic() - started < 2


@pytest.mark.parametrize("literal, grams", [("175.0", 175), ("175.9", 175), ("0.5", None),
                                            ("1e2", 100), ("-3.5", None), ('"175.5"', 175)])
def test_fractional_grams_are_truncated(literal, grams):
    body = ('{"products":[{"id":1,"handle":"h","title":"t","variants":'
            '[{"id":2,"price":"1.00","grams":%s}]}]}' % literal).encode()
    (p,) = fetch(Shop(respond=lambda req, n: httpx.Response(200, content=body)))
    assert p.variants[0].grams == grams


@pytest.mark.parametrize("literal", ["1e-99999999999999999999", "1e99999999999999999999", "-1.5e-99999999999999999999"])
def test_absurd_exponent_in_an_unrelated_field_does_not_abort_the_scrape(literal):
    body = ('{"products":[{"id":1,"handle":"h","title":"t","weight":%s,"variants":'
            '[{"id":2,"price":"1.00","grams":%s}]}]}' % (literal, literal)).encode()
    shop = Shop(respond=lambda req, n: httpx.Response(200, content=body))
    (p,) = fetch(shop)
    assert p.variants[0].price_cents == 100 and p.variants[0].grams is None
    assert shopify.check_store(STORE, client=shop.client)["ok"] is True


@pytest.mark.parametrize("field", ["price", "compare_at_price"])
@pytest.mark.parametrize("literal", ["1e-99999999999999999999", "1e99999999999999999999"])
def test_absurd_exponent_as_a_price_is_malformed_not_a_crash(field, literal):
    numbers = {"price": '"1.00"', "compare_at_price": "null", field: literal}
    body = ('{"products":[{"id":1,"handle":"h","title":"t","variants":[{"id":2,"price":%s,'
            '"compare_at_price":%s}]}]}' % (numbers["price"], numbers["compare_at_price"])).encode()
    shop = Shop(respond=lambda req, n: httpx.Response(200, content=body))
    with pytest.raises(shopify.StoreError, match="malformed product"):
        fetch(shop)
    res = shopify.check_store(STORE, client=shop.client)
    assert res["ok"] is False and "malformed product" in res["detail"]


# --- the response must be for the page we asked for ----------------------------------
# A canonical-host redirect that drops the query string made the store answer with its
# default 30-item first page; fetch_store returned those 30 of 600 products as complete,
# and record_products would have marked the other 570 gone.

def redirecting_shop(catalog, location):
    """Redirects the first host to `location`; the second host pages like Shopify
    (default limit 30 when no limit is given)."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.host == "shop.example":
            return httpx.Response(301, headers={"Location": location(request)})
        limit = int(request.url.params.get("limit", 30))
        page = int(request.url.params.get("page", 1))
        return httpx.Response(200, json={"products": catalog[(page - 1) * limit:page * limit]})

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("location", [
    lambda req: "https://www.shop.example/products.json",  # query dropped
    lambda req: "https://www.shop.example/products.json?limit=30&page=1",  # limit rewritten
    lambda req: "https://www.shop.example/products.json?limit=250",  # page dropped
    lambda req: "https://www.shop.example/products.json?limit=250&page=2",  # wrong page
])
def test_redirect_that_changes_the_query_is_an_error_not_a_short_catalogue(location):
    client = redirecting_shop([product(i) for i in range(600)], location)
    with pytest.raises(shopify.StoreError, match="query"):
        shopify.fetch_store(STORE, client=client, delay=0)
    assert shopify.check_store(STORE, client=client)["ok"] is False


def test_redirect_that_keeps_the_query_is_fine_even_with_extra_parameters():
    client = redirecting_shop(
        [product(i) for i in range(600)],
        lambda req: str(req.url.copy_with(host="www.shop.example").copy_add_param("currency", "USD")))
    assert len(shopify.fetch_store(STORE, client=client, delay=0)) == 600


def test_credentials_in_base_url_are_not_echoed_in_errors():
    store = {**STORE, "base_url": "https://admin:s3cret@shop.example"}
    with pytest.raises(shopify.StoreError, match="credentials") as info:
        fetch(Shop([product(1)]), store)
    assert "s3cret" not in str(info.value) and "admin" not in str(info.value)
    res = shopify.check_store(store, client=Shop([product(1)]).client)
    assert res["ok"] is False and "s3cret" not in res["detail"] and "admin" not in res["detail"]
