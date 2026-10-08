"""Fetch a store's catalogue from Shopify's public /products.json endpoint.

The contract (DESIGN.md section 3) is all-or-nothing: `db.record_products`
treats the returned list as the complete catalogue and marks everything else
gone, so every failure that could leave the list incomplete raises `StoreError`
instead of returning what we have so far.
"""
from __future__ import annotations

import email.utils
import json
import math
import re
import time
import urllib.parse
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterator

import httpx

from .models import RawProduct, RawVariant

USER_AGENT = "disc-tracker/1.0 (+https://github.com/mrcactusjones/mrcactusjones.github.io)"
ROBOTS_TOKEN = "disc-tracker"  # the product token robots.txt groups are matched against
PAGE_SIZE = 250  # Shopify's maximum `limit`; a shorter page means the last page
TIMEOUT = 30.0
MAX_RETRIES = 3
BACKOFF_BASE = 1.0  # seconds; retry n waits BACKOFF_BASE * 2**n
MAX_RETRY_AFTER = 120.0  # a server asking for longer than this is treated as a failure
MAX_CRAWL_DELAY = 30.0
CHECK_SAMPLE = 10  # products requested by check_store
ROBOTS_MAX_CHARS = 512 * 1024
MAX_PRICE = Decimal("10000000")  # dollars; anything above is garbage, not a disc price
MAX_GRAMS = Decimal("10000000")  # int() of a Decimal like 1e999999999 would run for minutes
RETRY_STATUSES = frozenset({408, 429})  # plus every 5xx
INT64_LIMIT = 2 ** 63  # ids are stored in SQLite INTEGER columns
_SURROGATES = re.compile("[\ud800-\udfff]")  # lone UTF-16 halves: unencodable, SQLite rejects them
_TRUE_TEXT = frozenset({"true", "t", "yes", "y", "1"})
_CRAWL_DELAY = re.compile(r"[0-9]{1,6}(\.[0-9]{1,3})?")


class StoreError(Exception):
    """The store could not be fetched completely (or is off limits)."""


def _sleep(seconds: float) -> None:
    """All waiting goes through here so tests can patch it (or time.sleep)."""
    time.sleep(seconds)


# --- money / field normalisation -------------------------------------------------

def _short(value, limit: int = 60) -> str:
    """repr() of untrusted data, bounded so error messages stay small."""
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "..."


def _scrub(text: str) -> str:
    """Replace lone surrogates (they cannot be encoded as UTF-8 or stored by SQLite)."""
    return _SURROGATES.sub("\ufffd", text)


def _text(value) -> str:
    """A text field from the store: None -> "", numbers -> str, anything structured is malformed."""
    if value is None:
        return ""
    if isinstance(value, str):
        return _scrub(value)
    if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
        return str(value)
    raise ValueError(f"not text: {_short(value)}")


def _available(value) -> bool:
    """`available` is a JSON bool; tolerate 0/1 and "true"/"false" (bool("false") is True)."""
    if isinstance(value, str):
        return value.strip().lower() in _TRUE_TEXT
    if isinstance(value, (int, Decimal)):
        return value != 0
    return False


def _to_cents(value) -> int | None:
    """Decimal price ("19.99", 20, Decimal) -> integer cents. None/"" -> None.

    Never goes through float. Raises ValueError for anything that is not a
    sane non-negative amount.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"not a price: {value!r}")
    text = str(value).strip()
    if not text:
        return None
    try:
        amount = Decimal(text)
    except ArithmeticError:
        raise ValueError(f"not a price: {_short(value)}") from None
    if not amount.is_finite() or amount < 0 or amount > MAX_PRICE:
        raise ValueError(f"price out of range: {_short(value)}")
    return int((amount * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _as_int(value) -> int:
    """An id: an int (or digit string) that fits SQLite's 64-bit INTEGER."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"not an integer: {_short(value)}")
    number = int(value)
    if not -INT64_LIMIT <= number < INT64_LIMIT:
        raise ValueError(f"integer out of range: {_short(value)}")
    return number


def _parse_float(text: str) -> Decimal:
    """json `parse_float`: keep decimals exact. An absurd exponent (1e-99999999999999999999)
    becomes NaN, which every consumer here already rejects, instead of aborting the parse."""
    try:
        return Decimal(text)
    except ArithmeticError:
        return Decimal("NaN")


def _grams(value) -> int | None:
    """Variant weight in grams; Shopify uses 0 for "not set"."""
    if isinstance(value, bool):
        return None
    try:
        number = value if isinstance(value, Decimal) else Decimal(value)
        if not number.is_finite() or not 0 < number <= MAX_GRAMS:  # bound both sides before int()
            return None
        grams = int(number)
    except (TypeError, ValueError, ArithmeticError):
        return None
    return grams if grams > 0 else None


def _tags(value) -> list[str]:
    """Tags arrive as a list or a comma-separated string; return stripped strings."""
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, list):
        items = value
    else:
        return []
    out = []
    for item in items:
        text = "" if item is None else _scrub(str(item)).strip()
        if text:
            out.append(text)
    return out


def _parse_variant(raw) -> RawVariant:
    if not isinstance(raw, dict):
        raise ValueError("variant is not an object")
    price = _to_cents(raw.get("price"))
    if price is None:
        raise ValueError(f"variant {_short(raw.get('id'))} has no price")
    compare_at = _to_cents(raw.get("compare_at_price"))
    return RawVariant(
        variant_id=_as_int(raw["id"]),
        title=_text(raw.get("title")),
        sku=_text(raw.get("sku")),
        price_cents=price,
        compare_at_cents=compare_at or None,  # null, "" and "0.00" all mean "no compare-at"
        available=_available(raw.get("available")),
        grams=_grams(raw.get("grams")),
    )


def _parse_product(raw, base_url: str) -> RawProduct:
    if not isinstance(raw, dict):
        raise ValueError("product is not an object")
    handle = raw.get("handle")
    if not isinstance(handle, str) or not handle:
        raise ValueError("product has no handle")
    handle = _scrub(handle)
    variants = raw.get("variants") or []
    if not isinstance(variants, list):
        raise ValueError("product variants is not a list")
    return RawProduct(
        product_id=_as_int(raw["id"]),
        handle=handle,
        title=_text(raw.get("title")),
        vendor=_text(raw.get("vendor")),
        product_type=_text(raw.get("product_type")),
        tags=_tags(raw.get("tags")),
        # The handle is untrusted: encode it so "?", "#", "/", "..", spaces and newlines
        # cannot turn the link into something other than this product's page. Plain
        # handles (a-z, 0-9, "-") and already percent-encoded ones are unchanged.
        url=f"{base_url}/products/{urllib.parse.quote(handle, safe='-._~%')}",
        variants=[_parse_variant(v) for v in variants],
    )


# --- HTTP ------------------------------------------------------------------------

@contextmanager
def _use_client(client: httpx.Client | None) -> Iterator[httpx.Client]:
    """Yield the injected client untouched, or a short-lived one we close ourselves."""
    if client is not None:
        yield client
        return
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as own:
        yield own


def _retry_after(response: httpx.Response) -> float | None:
    """Seconds from a Retry-After header (delta-seconds or HTTP-date), else None."""
    value = response.headers.get("retry-after")
    if value is None:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    if not math.isfinite(seconds) or seconds < 0:  # nan, inf or a negative delta: ignore it
        return None
    return seconds


def _get(client: httpx.Client, url: str, params: dict | None = None) -> httpx.Response:
    """GET with retries on timeouts, 408, 429 and 5xx; any other failure is final.

    Waits `Retry-After` when the server sends one, otherwise backs off
    exponentially. Raises StoreError once the retries are used up.
    """
    reason = ""
    for attempt in range(MAX_RETRIES + 1):
        wait = BACKOFF_BASE * 2 ** attempt
        try:
            resp = client.get(
                url, params=params, timeout=TIMEOUT, follow_redirects=True,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            reason = f"{type(exc).__name__}: {exc}"
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise StoreError(f"GET {url} failed: {type(exc).__name__}: {exc}") from exc
        else:
            status = resp.status_code
            if 200 <= status < 300:
                return resp
            if status not in RETRY_STATUSES and status < 500:
                raise StoreError(f"GET {url} returned HTTP {status}")
            reason = f"HTTP {status}"
            advised = _retry_after(resp)
            if advised is not None:
                if advised > MAX_RETRY_AFTER:
                    raise StoreError(
                        f"GET {url} returned HTTP {status} with Retry-After {advised:.0f}s "
                        f"(more than the {MAX_RETRY_AFTER:.0f}s we will wait)"
                    )
                wait = advised
        if attempt < MAX_RETRIES:
            if wait > 0:
                _sleep(wait)
    raise StoreError(f"GET {url} failed after {MAX_RETRIES} retries: {reason}")


def _page_params(page: int, limit: int) -> dict:
    return {"limit": limit, "page": page}


def _get_products_page(client: httpx.Client, endpoint: str, page: int, limit: int) -> list:
    """One page of raw product objects. Raises StoreError unless it is a valid list."""
    resp = _get(client, endpoint, _page_params(page, limit))
    asked = _page_params(page, limit)
    if {k: resp.url.params.get(k) for k in asked} != {k: str(v) for k, v in asked.items()}:
        # A redirect rewrote the query: the store would answer with its default 30-item
        # page, which would pass for a short (= final) page and drop most of the catalogue.
        raise StoreError(
            f"GET {endpoint} page {page}: a redirect changed the query string to "
            f"{_short(resp.url.query.decode('ascii', 'replace'), 80)}"
        )
    try:
        # parse_float keeps JSON numbers like 19.99 exact (see _to_cents).
        data = json.loads(resp.content, parse_float=_parse_float)
    except (ValueError, RecursionError):
        ctype = _short(resp.headers.get("content-type", "unknown"), 80)
        raise StoreError(
            f"GET {endpoint} page {page}: response is not valid JSON (content-type {ctype})"
        ) from None
    products = data.get("products") if isinstance(data, dict) else None
    if not isinstance(products, list):
        raise StoreError(f"GET {endpoint} page {page}: JSON has no 'products' list")
    return products


def _convert(raw_products: list, base_url: str, endpoint: str, page: int) -> list[RawProduct]:
    out = []
    for raw in raw_products:
        try:
            out.append(_parse_product(raw, base_url))
        except (KeyError, TypeError, ValueError) as exc:
            ident = raw.get("id") if isinstance(raw, dict) else None
            raise StoreError(
                f"GET {endpoint} page {page}: malformed product {_short(ident)}: {exc}"
            ) from exc
    return out


# --- store addressing / robots ---------------------------------------------------

def _base_url_problem(base: str) -> str | None:
    """Why `base` cannot be a store root, or None. It is prefixed to every request and to every
    product URL (which the exporter publishes), so credentials, queries and junk are refused."""
    if any(c.isspace() or not c.isprintable() for c in base):
        return "contains whitespace or control characters"
    try:
        parts = urllib.parse.urlsplit(base)
        parts.port  # noqa: B018 - raises ValueError for a non-numeric or out-of-range port
    except ValueError as exc:
        return f"is not a valid URL ({exc})"
    if parts.scheme not in ("http", "https"):
        return "must be an http(s) URL"
    if not parts.hostname:
        return "has no host name"
    if "@" in parts.netloc:
        return "must not contain credentials"
    if "?" in base or "#" in base:
        return "must not contain a query string or fragment"
    return None


def _base_url(store: dict) -> str:
    base = str(store.get("base_url") or "").strip().rstrip("/")
    if not base:
        raise StoreError(f"store {store.get('id', '?')!r} has no base_url")
    problem = _base_url_problem(base)
    if problem:
        shown = re.sub(r"//[^/?#]*@", "//***@", base)  # never echo credentials into logs
        raise StoreError(f"store {store.get('id', '?')!r}: base_url {_short(shown, 100)} {problem}")
    return base


def _endpoint(store: dict, base_url: str) -> str:
    collection = str(store.get("collection") or "").strip().strip("/")
    if collection:
        return f"{base_url}/collections/{urllib.parse.quote(collection, safe='')}/products.json"
    return f"{base_url}/products.json"


def _quote_non_ascii(text: str) -> str:
    return "".join(c if c.isascii() else urllib.parse.quote(c, errors="replace") for c in text)


def _glob_match(pattern: str, target: str) -> bool:
    """robots.txt pattern match: a prefix match where `*` is any run of characters and a
    trailing `$` anchors the end. Greedy and linear: no regex, so hostile rules cannot blow up."""
    anchored = pattern.endswith("$")
    if anchored:
        pattern = pattern[:-1]
    first, *rest = pattern.split("*")
    if not target.startswith(first):
        return False
    if not rest:
        return target == first if anchored else True
    pos = len(first)
    for part in rest[:-1]:
        found = target.find(part, pos)
        if found < 0:
            return False
        pos = found + len(part)
    last = rest[-1]
    if anchored:
        return len(target) - len(last) >= pos and target.endswith(last)
    return target.find(last, pos) >= 0


class _Robots:
    """The robots.txt rules that apply to ROBOTS_TOKEN, per RFC 9309.

    Written out instead of using urllib.robotparser because that parser's behaviour
    depends on the Python version (3.12 has no wildcards and lets the first matching rule
    win; it ignores a second `*` group, a byte order mark and query strings, and raises a
    bare ValueError on a Crawl-delay written with a non-ASCII digit), and a scraper must
    not guess about permission.
    The group naming ROBOTS_TOKEN wins over the `*` group; groups with the same name are
    merged; the longest matching rule wins and Allow beats Disallow on a tie.
    """

    def __init__(self, text: str):
        groups: list[dict] = []
        group: dict | None = None
        reading_agents = False
        for line in re.split(r"\r\n|\r|\n", text.lstrip("\ufeff")):
            name, colon, value = line.split("#", 1)[0].partition(":")
            if not colon:
                continue
            name = name.strip().lower().replace("-", "").replace(" ", "")
            value = value.strip()
            if name == "useragent":
                if not reading_agents:  # consecutive User-agent lines share one group
                    group = {"agents": set(), "rules": [], "delay": None}
                    groups.append(group)
                    reading_agents = True
                group["agents"].add(value.lower().split("/", 1)[0].strip())
            elif name in ("allow", "disallow", "crawldelay"):
                reading_agents = False
                if group is None:
                    continue
                if name == "crawldelay":
                    if _CRAWL_DELAY.fullmatch(value):
                        group["delay"] = max(group["delay"] or 0.0, float(value))
                elif value:  # an empty Disallow allows everything: nothing to record
                    if not value.startswith(("/", "*")):
                        value = "/" + value
                    group["rules"].append((name == "allow", _quote_non_ascii(value)))
            # anything else (Sitemap, Host, ...) neither applies nor ends a group
        mine = [g for g in groups if ROBOTS_TOKEN in g["agents"]]
        chosen = mine or [g for g in groups if "*" in g["agents"]]
        self.rules = [rule for g in chosen for rule in g["rules"]]
        delays = [g["delay"] for g in chosen if g["delay"] is not None]
        self.crawl_delay = min(max(delays), MAX_CRAWL_DELAY) if delays else 0.0

    def allows(self, target: str) -> bool:
        """May we fetch `target` (a path, optionally followed by ?query)?"""
        target = _quote_non_ascii(target)
        best = max(((len(pattern), allow) for allow, pattern in self.rules
                    if _glob_match(pattern, target)), default=None)
        return best is None or best[1]


def _fetch_robots(client: httpx.Client, base_url: str) -> _Robots | None:
    """Fetch and parse `{base_url}/robots.txt` once. One attempt, no retries: a missing,
    unreadable or non-200 robots.txt means "allowed" (DESIGN.md), so None."""
    try:
        resp = client.get(
            f"{base_url}/robots.txt", timeout=TIMEOUT, follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "text/plain"},
        )
        if resp.status_code != 200:
            return None
        text = resp.text[:ROBOTS_MAX_CHARS]
    except (httpx.HTTPError, httpx.InvalidURL):
        return None
    return _Robots(text)


def _require_allowed(robots: _Robots | None, base_url: str, endpoint: str, params: dict) -> None:
    """Raise StoreError if robots.txt forbids this request. Both the bare path (the thing
    DESIGN.md names) and the path with the query string we really send are checked, so a
    rule such as `Disallow: /products.json$` or `Disallow: /*?*page=2` is honoured too."""
    if robots is None:
        return
    path = urllib.parse.urlsplit(endpoint).path
    for target in (path, f"{path}?{urllib.parse.urlencode(params)}"):
        if not robots.allows(target):
            raise StoreError(f"{base_url}/robots.txt disallows {target} for {ROBOTS_TOKEN}")


# --- public API ------------------------------------------------------------------

def fetch_store(store: dict, client: httpx.Client | None = None, delay: float = 1.0,
                max_pages: int = 100, respect_robots: bool = True) -> list[RawProduct]:
    """Return every product of a store, or raise StoreError (never a partial list).

    Pages of 250 are requested until one comes back short or empty. `delay`
    seconds are slept between pages (a robots.txt Crawl-delay raises it, capped
    at MAX_CRAWL_DELAY). Products are de-duplicated by id: a catalogue edited
    mid-scrape can shift an item onto two pages, but a full page with nothing
    new in it means the server is ignoring `page`, which is an error.
    """
    if max_pages < 1:
        raise ValueError("max_pages must be at least 1")
    base = _base_url(store)
    endpoint = _endpoint(store, base)
    with _use_client(client) as http:
        robots = _fetch_robots(http, base) if respect_robots else None
        if robots is not None:
            delay = max(delay, robots.crawl_delay)
        products: list[RawProduct] = []
        seen: set[int] = set()
        for page in range(1, max_pages + 1):
            _require_allowed(robots, base, endpoint, _page_params(page, PAGE_SIZE))
            if page > 1 and delay > 0:
                _sleep(delay)
            raw = _get_products_page(http, endpoint, page, PAGE_SIZE)
            fresh = 0
            for product in _convert(raw, base, endpoint, page):
                if product.product_id not in seen:
                    seen.add(product.product_id)
                    products.append(product)
                    fresh += 1
            if len(raw) < PAGE_SIZE:
                return products
            if fresh == 0:
                raise StoreError(
                    f"GET {endpoint}: page {page} repeats products already seen; "
                    "the server seems to ignore ?page"
                )
        raise StoreError(
            f"GET {endpoint}: page {max_pages} (max_pages) was still full; "
            "refusing to return a partial catalogue"
        )


def check_store(store: dict, client: httpx.Client | None = None) -> dict:
    """Probe a store without scraping it. Never raises for store problems.

    Returns {"ok": bool, "detail": str, "sample_count": int}. A store that
    answers with zero products is not ok, because `scrape` would refuse it.
    """
    try:
        base = _base_url(store)
        endpoint = _endpoint(store, base)
        with _use_client(client) as http:
            _require_allowed(_fetch_robots(http, base), base, endpoint, _page_params(1, CHECK_SAMPLE))
            raw = _get_products_page(http, endpoint, 1, CHECK_SAMPLE)
            products = _convert(raw, base, endpoint, 1)
    except StoreError as exc:
        return {"ok": False, "detail": str(exc), "sample_count": 0}
    if not products:
        return {"ok": False, "detail": f"{endpoint} is reachable but returned no products",
                "sample_count": 0}
    return {
        "ok": True,
        "detail": f"{endpoint} ok, {len(products)} sample products (first: {products[0].title!r})",
        "sample_count": len(products),
    }
