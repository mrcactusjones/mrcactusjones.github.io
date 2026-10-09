"""Collect eBay asking prices through the official Browse API (DESIGN.md section 10).

Browse API sees active listings only, so everything here is an *asking* price. A search sees a
slice of eBay, never the whole marketplace, which is why `collect` records results with
`complete=False` and only lets a query that ran to the end (`SearchResult.complete`) expire
the listings it used to return.

Failure policy:
* `EbayAuthError`  - credentials missing/rejected: abort, record nothing for the query in flight.
* `EbayQuotaError` - HTTP 429 twice in a row: stop the run, keep what was already recorded.
* `EbayError`      - anything else wrong with one query: that query is skipped, the run goes on
                     (a streak of failures stops the run, see MAX_CONSECUTIVE_FAILURES) and the
                     query is moved to the back of the rotation so it cannot block the others.

A result only counts as `complete` (and so may expire listings) when eBay's own count was reached
by distinct, readable items: a total that shrinks while paging, or a response in which most items
cannot be read, leaves the query incomplete.

Credentials are read from the environment only (`load_credentials`) and are scrubbed from every
message this module produces.
"""
from __future__ import annotations

import base64
import email.utils
import json
import math
import os
import re
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Callable, Iterable, Mapping, NamedTuple

import httpx

from . import db
from .models import RawProduct, RawVariant

EBAY_STORE = {"id": "ebay", "name": "eBay", "base_url": "https://www.ebay.com",
              "currency": "USD", "kind": "marketplace"}

USER_AGENT = "disc-tracker/1.0 (+https://github.com/mrcactusjones/mrcactusjones.github.io)"
DATA_DIR = Path(__file__).resolve().parent / "data"

TOKEN_PATH = "/identity/v1/oauth2/token"
SEARCH_PATH = "/buy/browse/v1/item_summary/search"
TOKEN_SCOPE = "https://api.ebay.com/oauth/api_scope"
TOKEN_MARGIN = 60.0  # seconds before expiry at which a cached token is no longer used
DEFAULT_TOKEN_TTL = 300.0  # used only when the token response carries no usable expires_in

PAGE_SIZE = 200  # Browse API maximum `limit`
MAX_RESULTS = 10_000  # the API refuses offset + limit > 10,000
BUYING_FILTER = "buyingOptions:{FIXED_PRICE}"  # asking prices only: an auction's price is a bid

TIMEOUT = 30.0
MAX_RETRIES = 3  # retries of timeouts / 5xx per request (so 4 attempts)
BACKOFF_BASE = 1.0  # seconds; retry n waits BACKOFF_BASE * 2**n unless Retry-After says otherwise
RATE_LIMIT_WAIT = 5.0  # a 429 without Retry-After is retried once after this long
MAX_RETRY_AFTER = 120.0  # a server asking for more than this is not waited for
MAX_CONSECUTIVE_FAILURES = 10  # collect() gives up after this many failed queries in a row
MIN_UNUSABLE = 5  # this many unreadable / foreign-currency summaries, and a majority, mean "format drift"
MIN_PREVIOUS_FOR_DROP_CHECK = 20  # a query that returned fewer results than this is too small to judge
MAX_DROP = 0.5  # a complete result this much smaller than last time is not believed (see collect)

NEW_CONDITION_IDS = frozenset({"1000", "1500"})  # everything else with an id counts as used
MAX_PRICE = Decimal("10000000")  # dollars; above this is garbage, not a disc price

# v1|<legacy id>|<variation id or 0>; at most 18 digits keeps ids inside SQLite's 64-bit INTEGER
_ITEM_ID = re.compile(r"v1\|([0-9]{1,18})\|([0-9]{1,18})")
_DECIMAL = re.compile(r"\s*[0-9]{1,12}(\.[0-9]{1,6})?\s*")
_END_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[0-9TZ:.+\-]{0,30}")
_SURROGATES = re.compile("[\ud800-\udfff]")  # lone UTF-16 halves: unencodable, SQLite rejects them


class EbayError(Exception):
    """Something went wrong talking to eBay. `calls` = search requests spent before it was raised."""

    calls = 0


class EbayAuthError(EbayError):
    """Credentials missing or rejected: abort, record nothing."""


class EbayQuotaError(EbayError):
    """Rate or daily limit hit: stop the run gracefully, keep what we have."""


class _BudgetExhausted(Exception):
    """Internal: the search ran out of its call allowance."""


class _OutOfBudget(EbayError):
    """The call allowance ran out before the first page of a query was fetched. Not the query's fault."""


@dataclass
class SearchResult:
    items: list[RawProduct]
    total: int  # what eBay said matched (the largest `total` any page announced)
    complete: bool  # every one of `total` items was fetched and read; only then may absence expire a listing
    calls: int  # search requests made (retries included); token requests are not counted
    # Extras beyond DESIGN.md 10.2 (defaults keep the 4-field constructor valid):
    skipped_currency: int = 0  # items priced in a currency other than the store's
    skipped_variations: int = 0  # extra variations of an item already kept (one listing per item)
    skipped_other: int = 0  # unusable summaries: bad id/price/title, auctions, ...
    error: str = ""  # why a result is incomplete, when a page failed or the budget ran out


@dataclass
class _Tally:
    limit: int
    used: int = 0


class _Skip(Exception):
    """Internal: this summary cannot become a listing. `reason` is 'currency' (not the store's
    currency), 'other' (unreadable) or 'auction' (deliberately not an asking price)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# --- credentials / queries ---------------------------------------------------------

def load_credentials(env: Mapping[str, str] | None = None) -> tuple[str, str] | None:
    """(client id, client secret) from EBAY_CLIENT_ID / EBAY_CLIENT_SECRET, or None if either is
    unset, empty or only whitespace. Surrounding whitespace (a stray newline in a CI secret) is
    stripped. Credentials are never read from files or the database."""
    env = os.environ if env is None else env

    def get(name: str) -> str:
        value = env.get(name)
        return value.strip() if isinstance(value, str) else ""

    client_id, secret = get("EBAY_CLIENT_ID"), get("EBAY_CLIENT_SECRET")
    return (client_id, secret) if client_id and secret else None


def _load_data(name: str):
    with (DATA_DIR / name).open(encoding="utf-8") as f:
        return json.load(f)


def _clean(text) -> str:
    return " ".join(str(text).split())


def _norm(text) -> str:
    return _clean(text).lower()


def _plastic_names(entries) -> list[str]:
    """Plastic names from plastics.json entries ({"name": ...} or plain strings), in order,
    without blanks or case-insensitive duplicates."""
    names, seen = [], set()
    for entry in entries or []:
        name = _clean(entry.get("name", "") if isinstance(entry, Mapping) else entry)
        if name and name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)
    return names


def build_queries(molds=None, plastics=None) -> list[tuple[str, str]]:
    """[(query_key, query_text)], one per (mold, plastic of that mold's manufacturer).

    `molds` is the "molds" list of data/molds.json (the whole document is accepted too) and
    `plastics` the parsed data/plastics.json; both default to the shipped files. A mold whose
    manufacturer has no plastics gets the generic plastics instead, never an empty plastic.
    query_text = "<plastic> <mold> <manufacturer>" (eBay ANDs the words) and is sent unescaped;
    query_key = "manufacturer|mold|plastic", lowercased. Keys are unique (first entry wins) and
    the order follows the data files, so it is deterministic.
    """
    if molds is None:
        molds = _load_data("molds.json")
    if plastics is None:
        plastics = _load_data("plastics.json")
    if isinstance(molds, Mapping):
        molds = molds.get("molds") or []
    generic = _plastic_names(plastics.get("generic"))
    by_maker = {_norm(maker): _plastic_names(entries)
                for maker, entries in (plastics.get("manufacturers") or {}).items()}

    queries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for entry in molds:
        if not isinstance(entry, Mapping):
            continue
        maker, mold = _clean(entry.get("manufacturer") or ""), _clean(entry.get("mold") or "")
        if not maker or not mold:
            continue
        for plastic in by_maker.get(_norm(maker)) or generic:
            key = "|".join((_norm(maker), _norm(mold), _norm(plastic)))
            if key not in seen:
                seen.add(key)
                queries.append((key, f"{plastic} {mold} {maker}"))
    return queries


# --- field normalisation ----------------------------------------------------------------

def _scrub(text: str) -> str:
    return _SURROGATES.sub("�", text)


def _parse_float(text: str) -> Decimal:
    """json `parse_float`: JSON numbers stay exact decimals; an absurd exponent becomes NaN
    (rejected later) instead of aborting the whole parse."""
    try:
        return Decimal(text)
    except ArithmeticError:
        return Decimal("NaN")


def _to_cents(value) -> int:
    """A price ("24.99", Decimal) -> integer cents, never through float.

    Raises ValueError unless it is a sane positive amount."""
    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, int) and not isinstance(value, bool):
        amount = Decimal(value)
    elif isinstance(value, str) and _DECIMAL.fullmatch(value):
        amount = Decimal(value.strip())
    else:
        raise ValueError("not a price")
    # is_finite() first: a NaN cannot be ordered
    if not amount.is_finite() or amount <= 0 or amount > MAX_PRICE:
        raise ValueError("price out of range")
    cents = int((amount * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if cents < 1:  # 0.001 passes the checks above but is a free disc once rounded
        raise ValueError("price out of range")
    return cents


def _clean_url(value) -> str:
    """itemWebUrl reduced to scheme + host + path (tracking params and fragments dropped).

    Returns "" for anything that is not a plain http(s) link with a path."""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    # A backslash is never legitimate here, and browsers read "https://evil.example\\www.ebay.com/x"
    # as host evil.example while urllib sees the host as www.ebay.com.
    if not text or "\\" in text or any(c.isspace() or not c.isprintable() for c in text):
        return ""
    try:
        parts = urllib.parse.urlsplit(text)
        parts.port  # noqa: B018 - raises ValueError for a non-numeric or out-of-range port
    except ValueError:
        return ""
    if (parts.scheme not in ("http", "https") or not parts.hostname or "@" in parts.netloc
            or "%" in parts.netloc or parts.path in ("", "/")):
        return ""
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc.lower(), parts.path, "", ""))


def _end_date(value) -> str:
    """itemEndDate as given when it looks like an ISO timestamp, else ""."""
    if isinstance(value, str) and _END_DATE.fullmatch(value.strip()):
        return value.strip()
    return ""


def _condition_tag(condition_id) -> str | None:
    """conditionId 1000/1500 -> new, any other id -> used, no usable id -> no tag."""
    if isinstance(condition_id, bool) or not isinstance(condition_id, (int, str)):
        return None
    text = str(condition_id).strip()
    if not text.isdigit():
        return None
    return "condition:new" if text in NEW_CONDITION_IDS else "condition:used"


def _convert(raw, query_key: str) -> tuple[RawProduct, int]:
    """One itemSummaries entry -> (RawProduct, variation id). Raises _Skip when unusable."""
    if not isinstance(raw, dict):
        raise _Skip("other")
    item_id = raw.get("itemId")
    match = _ITEM_ID.fullmatch(item_id) if isinstance(item_id, str) else None
    if match is None:
        raise _Skip("other")
    legacy, variation = int(match[1]), int(match[2])
    if legacy == 0:
        raise _Skip("other")

    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        raise _Skip("other")
    options = raw.get("buyingOptions")
    if isinstance(options, list) and options and ("AUCTION" in options or "FIXED_PRICE" not in options):
        raise _Skip("auction")  # the price of an auction is a bid, not an asking price

    price = raw.get("price")
    if not isinstance(price, dict):
        raise _Skip("other")
    currency = price.get("currency")
    if not isinstance(currency, str) or not currency.strip():
        raise _Skip("other")
    if currency.strip().upper() != EBAY_STORE["currency"]:
        raise _Skip("currency")
    try:
        cents = _to_cents(price.get("value"))
    except ValueError:
        raise _Skip("other") from None

    condition = raw.get("condition")
    tag = _condition_tag(raw.get("conditionId"))
    product = RawProduct(
        product_id=legacy,
        handle=str(legacy),
        title=_scrub(title),
        vendor="",
        product_type=_scrub(condition) if isinstance(condition, str) else "",
        tags=[tag] if tag else [],
        url=_clean_url(raw.get("itemWebUrl")) or f"{EBAY_STORE['base_url']}/itm/{legacy}",
        # One variant per item, keyed by the legacy id: a stable identity for the price history.
        variants=[RawVariant(variant_id=legacy, price_cents=cents, available=True)],
        ends_at=_end_date(raw.get("itemEndDate")),
        query_key=query_key,
    )
    return product, variation


# --- HTTP ---------------------------------------------------------------------------------

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
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


def _detail(response: httpx.Response) -> str:
    """The single-line reason in an error body (eBay: error_description / errors[].message).

    Not shortened here: the client redacts first and cuts afterwards, so that a secret straddling
    the cut cannot leave a readable fragment."""
    try:
        data = json.loads(response.content)
    except (ValueError, RecursionError):
        return ""
    text = ""
    if isinstance(data, dict):
        errors = data.get("errors")
        first = errors[0] if isinstance(errors, list) and errors and isinstance(errors[0], dict) else {}
        for candidate in (data.get("error_description"), first.get("message"), data.get("error"),
                          data.get("message")):
            if isinstance(candidate, str) and candidate.strip():
                text = candidate
                break
    return _clean(text)


class _Page(NamedTuple):
    """One parsed search response."""

    total: int
    summaries: list


def _read_page(data) -> _Page:
    """Validate a search response body. Raises ValueError with a credential-free reason."""
    if not isinstance(data, dict):
        raise ValueError("response is not a JSON object")
    summaries = data.get("itemSummaries")
    if summaries is None:
        summaries = []  # absent when nothing matched
    if not isinstance(summaries, list):
        raise ValueError("itemSummaries is not a list")
    total = data.get("total")
    if total is None and not summaries:
        total = 0
    if isinstance(total, str) and total.isdigit():
        total = int(total)
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise ValueError("response has no usable total")
    return _Page(total, summaries)


class EbayClient:
    """Browse API client: OAuth application token (cached), paginated search, retries.

    Waiting goes through `sleeper` so tests can make it instant; `_clock` (monotonic seconds) is
    the token-expiry clock and is likewise replaceable in tests.
    """

    def __init__(self, client_id, client_secret, http: httpx.Client | None = None, *,
                 marketplace_id="EBAY_US", category_ids=("184356",),
                 base_url="https://api.ebay.com", sleeper=time.sleep):
        if not client_id or not client_secret:
            raise EbayAuthError("missing eBay credentials")
        self._basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
        self._secrets = [str(client_id), str(client_secret), self._basic]
        self._owns_http = http is None
        self._http = http if http is not None else httpx.Client(timeout=TIMEOUT)
        self.marketplace_id = marketplace_id
        self.category_ids = (tuple(str(c) for c in category_ids) if not isinstance(category_ids, str)
                             else (category_ids,))  # (ids are often written as ints)
        self._base = base_url.rstrip("/")
        self._sleep = sleeper
        self._clock: Callable[[], float] = time.monotonic
        self._token = ""
        self._token_expires = 0.0

    def __repr__(self) -> str:  # never show the credentials or the token
        return f"EbayClient(marketplace_id={self.marketplace_id!r}, base_url={self._base!r})"

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> "EbayClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- errors never carry credentials --

    def _redact(self, text: str) -> str:
        for secret in sorted(self._secrets, key=len, reverse=True):
            if len(secret) >= 4:
                text = text.replace(secret, "***")
        return text

    def _error(self, cls: type[EbayError], text: str) -> EbayError:
        return cls(self._redact(text))

    def _reason(self, response: httpx.Response) -> str:
        """The server's explanation of an error: redacted first, then cut to 200 characters."""
        return self._redact(_detail(response))[:200]

    # -- requests --

    def _send(self, method: str, url: str, tally: _Tally | None = None, **kwargs) -> httpx.Response:
        """One request under the retry policy; every attempt counts against `tally`.

        Timeouts/network errors, 408 and 5xx are retried up to MAX_RETRIES times (Retry-After if
        sent, else exponential backoff); a 429 is retried once, the second one is a quota error.
        Anything else comes back as a response for the caller to judge.
        """
        where = f"{method} {urllib.parse.urlsplit(url).path}"
        failures, rate_limited, reason = 0, False, ""

        def pause(seconds: float) -> None:
            if tally is not None and tally.used >= tally.limit:
                raise _BudgetExhausted  # no attempt left to wait for
            if seconds > 0:
                self._sleep(seconds)

        while True:
            if tally is not None:
                if tally.used >= tally.limit:
                    raise _BudgetExhausted
                tally.used += 1
            advised, fatal = None, ""
            try:
                resp = self._http.request(method, url, timeout=TIMEOUT, **kwargs)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                reason = type(exc).__name__
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                fatal = type(exc).__name__
            else:
                status = resp.status_code
                advised = _retry_after(resp)
                if status == 429:
                    if rate_limited or (advised is not None and advised > MAX_RETRY_AFTER):
                        raise self._error(EbayQuotaError, f"{where}: rate limit reached (HTTP 429)")
                    rate_limited = True
                    pause(RATE_LIMIT_WAIT if advised is None else advised)
                    continue
                if status < 500 and status != 408:
                    return resp
                reason = f"HTTP {status}"
            if fatal:  # raised out here, not in the except block: the httpx error (its request holds
                # the Authorization header) must not stay reachable through __context__
                raise self._error(EbayError, f"{where} failed: {fatal}")
            if failures >= MAX_RETRIES:
                raise self._error(EbayError, f"{where} failed after {MAX_RETRIES} retries: {reason}")
            if advised is not None and advised > MAX_RETRY_AFTER:
                raise self._error(
                    EbayError, f"{where}: {reason} with Retry-After {advised:.0f}s "
                               f"(more than the {MAX_RETRY_AFTER:.0f}s we will wait)")
            wait = BACKOFF_BASE * 2 ** failures if advised is None else advised
            failures += 1
            pause(wait)

    def _fetch_token(self) -> str:
        started = self._clock()
        resp = self._send(
            "POST", self._base + TOKEN_PATH,
            headers={"Authorization": f"Basic {self._basic}", "User-Agent": USER_AGENT,
                     "Accept": "application/json"},
            data={"grant_type": "client_credentials", "scope": TOKEN_SCOPE},
        )
        if not 200 <= resp.status_code < 300:
            detail = self._reason(resp)
            raise self._error(
                EbayAuthError,
                f"eBay refused the token request (HTTP {resp.status_code}"
                f"{': ' + detail if detail else ''}); check EBAY_CLIENT_ID / EBAY_CLIENT_SECRET")
        try:
            data = json.loads(resp.content, parse_float=_parse_float)
        except (ValueError, RecursionError):
            data = None
        token = data.get("access_token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise self._error(EbayAuthError, "eBay's token response had no access_token")
        ttl = data.get("expires_in")
        try:
            usable = isinstance(ttl, (int, str, Decimal)) and not isinstance(ttl, bool)
            ttl = float(ttl) if usable else math.nan
        except (ValueError, OverflowError):
            ttl = math.nan
        if not math.isfinite(ttl) or not 0 < ttl < 1e9:
            ttl = DEFAULT_TOKEN_TTL
        self._token = token.strip()
        self._token_expires = started + ttl - min(TOKEN_MARGIN, ttl / 2)
        self._secrets.append(self._token)
        return self._token

    def _access_token(self) -> str:
        if self._token and self._clock() < self._token_expires:
            return self._token
        return self._fetch_token()

    def _get_page(self, params: dict, tally: _Tally) -> _Page:
        """One search request: bearer auth, a single token refresh on 401, body validation."""
        refreshed = False
        while True:
            resp = self._send(
                "GET", self._base + SEARCH_PATH, tally, params=params,
                headers={"Authorization": f"Bearer {self._access_token()}",
                         "X-EBAY-C-MARKETPLACE-ID": self.marketplace_id,
                         "User-Agent": USER_AGENT, "Accept": "application/json"},
            )
            status = resp.status_code
            if status == 401:
                if refreshed:
                    raise self._error(EbayAuthError, "eBay rejected a freshly issued access token (HTTP 401)")
                refreshed = True
                self._token = ""  # forces a new token on the next loop
                continue
            if not 200 <= status < 300:
                detail = self._reason(resp)
                raise self._error(
                    EbayError, f"search returned HTTP {status}{': ' + detail if detail else ''}")
            try:
                return _read_page(json.loads(resp.content, parse_float=_parse_float))
            except (ValueError, RecursionError) as exc:
                problem = str(exc)  # (a JSONDecodeError holds the whole body: do not chain to it)
            raise self._error(EbayError, f"search response unusable: {problem}")

    # -- search --

    def search(self, query: str, query_key: str, max_calls: int) -> SearchResult:
        """Fixed-price listings matching `query`, tagged with `query_key`.

        Pages through the results (200 per request) for at most `max_calls` search requests;
        every attempt, retries included, is one call. A page that fails after the first one,
        a call budget that runs out, or a total beyond eBay's 10,000-result window yield an
        incomplete result (`complete=False`) holding what was fetched. A failure before any
        page was fetched raises: EbayAuthError, EbayQuotaError or EbayError (whose `calls`
        says how many requests it cost).
        """
        tally = _Tally(limit=max_calls)
        try:
            return self._search(query, query_key, tally)
        except EbayError as exc:
            exc.calls = tally.used
            raise

    def _search(self, query: str, query_key: str, tally: _Tally) -> SearchResult:
        best: dict[int, tuple[int, int, RawProduct]] = {}  # legacy id -> (cents, variation, product)
        seen: set[str] = set()  # distinct itemIds returned (results can shift between pages)
        unreadable = 0  # summaries without a usable itemId
        skipped = {"currency": 0, "variation": 0, "other": 0, "auction": 0}
        total, pages, offset, error = 0, 0, 0, ""
        complete = False

        while offset + PAGE_SIZE <= MAX_RESULTS:
            params = {"q": query, "limit": PAGE_SIZE, "offset": offset, "filter": BUYING_FILTER}
            if self.category_ids:
                params["category_ids"] = ",".join(self.category_ids)
            try:
                page = self._get_page(params, tally)
            except _BudgetExhausted:
                if not pages:
                    raise self._error(
                        _OutOfBudget, "call budget exhausted before the first page was fetched") from None
                error = "call budget exhausted"
                break
            except (EbayAuthError, EbayQuotaError):
                raise
            except EbayError as exc:
                if not pages:
                    raise
                error = str(exc)
                break
            pages += 1
            # The largest total any page announced, not the last one: a later page reporting less
            # than what was already fetched (a glitch, or listings selling while we page, which
            # slides the rest of the list up and hides some of it) must not make the result
            # look complete.
            total = max(total, page.total)

            for raw in page.summaries:
                item_id = raw.get("itemId") if isinstance(raw, dict) else None
                if not isinstance(item_id, str):
                    unreadable += 1
                    skipped["other"] += 1
                    continue
                if item_id in seen:
                    continue
                seen.add(item_id)
                try:
                    product, variation = _convert(raw, query_key)
                except _Skip as skip:
                    skipped[skip.reason] += 1
                    continue
                cents = product.variants[0].price_cents
                held = best.get(product.product_id)
                if held is None:
                    best[product.product_id] = (cents, variation, product)
                    continue
                # Another variation of an item we already have: keep one listing per item, the
                # cheapest ("from $X"), whichever order eBay returned them in.
                skipped["variation"] += 1
                if (cents, variation) < held[:2]:
                    best[product.product_id] = (cents, variation, product)

            if len(seen) + unreadable >= total:  # (at most MAX_RESULTS are reachable, so total fits)
                complete = True
                break
            if not page.summaries:
                break  # eBay says there is more, yet returns nothing: do not loop on it
            offset += PAGE_SIZE

        fetched = len(seen) + unreadable
        unusable = skipped["currency"] + skipped["other"]  # auctions are left out on purpose, these are not
        if complete and unusable >= MIN_UNUSABLE and unusable * 2 > fetched:
            # Nearly everything eBay sent was unreadable or in another currency: more likely the
            # response format changed than that the query's listings all became unusable. Absence
            # from such a result proves nothing, so it must not expire anything.
            complete = False
            error = f"{unusable} of {fetched} results were unusable (did eBay's response format change?)"
        if not complete and not error:
            error = (f"{total} results exceed eBay's {MAX_RESULTS:,}-result window" if total > MAX_RESULTS
                     else f"eBay returned only {fetched} of {total} results")
        return SearchResult(
            items=[product for _, _, product in best.values()], total=total, complete=complete,
            calls=tally.used, skipped_currency=skipped["currency"],
            skipped_variations=skipped["variation"], skipped_other=skipped["other"] + skipped["auction"],
            error=error)


# --- orchestration ------------------------------------------------------------------------

def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _last_total(conn, query_key: str) -> int:
    """The result count eBay reported the last time this query was recorded (0 = never)."""
    row = conn.execute("SELECT last_total FROM ebay_queries WHERE query_key=?", (query_key,)).fetchone()
    return int(row[0]) if row else 0


def collect(conn, client: EbayClient, observed_on: str, call_budget: int = 3500,
            queries: Iterable[tuple[str, str]] | None = None, weight_parser=None, log=print) -> dict:
    """One eBay run: search the stalest queries first and record what they return.

    For each query: search -> db.record_products(complete=False) -> db.record_query_run -> (only
    if the query ran to completion) db.expire_missing. Stops when the call budget is spent
    ("budget"), eBay's quota is hit ("quota"), too many queries fail in a row ("errors") or the
    queries are exhausted ("done"). A single failing query is logged and skipped.

    Safety nets beyond the contract, both against wrongly retiring listings:
    * a complete result that is less than half of what the same query returned last time (and
      that was at least MIN_PREVIOUS_FOR_DROP_CHECK) is recorded but expires nothing; if it is
      still that small on the next run it is believed. (A transient "200 OK, total 0" would
      otherwise retire every listing of the query and invent a sale for each.)
    * a query that failed on its own (not through the call budget) is recorded as attempted, with
      its old total, once the run ends. Without that it stays the stalest query for ever, and
      MAX_CONSECUTIVE_FAILURES such queries at the head of the rotation would stop every run
      before it reached a working one.

    EbayAuthError is re-raised: when it comes before any query was recorded the database is not
    touched at all (the store and run rows are only written once there is something to record);
    later it ends the run row as 'error' and keeps the queries already recorded.
    The run row ends 'ok' unless not one query could be recorded although some failed or the run
    was cut off (quota / failure streak), then 'error'.
    """
    store_id = EBAY_STORE["id"]
    unique: dict[str, str] = {}
    for key, text in (build_queries() if queries is None else queries):
        unique.setdefault(key, text)
    ordered = db.order_queries(conn, list(unique.items()))

    stats = {"queries_run": 0, "queries_complete": 0, "calls": 0, "items_seen": 0,
             "new_listings": 0, "gone": 0, "inferred_sales": 0, "skipped_currency": 0,
             "stopped": "done"}
    skipped_other = 0
    failed, streak, last_error = 0, 0, ""
    failed_queries: list[tuple[str, str]] = []  # failed on their own, to be moved down the rotation
    run_id: int | None = None

    def begin() -> int:
        nonlocal run_id
        if run_id is None:
            db.upsert_store(conn, EBAY_STORE)
            run_id = db.start_run(conn, store_id, observed_on, _timestamp())
        return run_id

    try:
        for key, text in ordered:
            remaining = call_budget - stats["calls"]
            if remaining <= 0:
                stats["stopped"] = "budget"
                break
            try:
                result = client.search(text, key, remaining)
            except EbayQuotaError as exc:
                stats["calls"] += exc.calls
                log(f"ebay: rate limit reached, stopping the run: {exc}")
                stats["stopped"] = "quota"
                last_error = str(exc)
                break
            except EbayAuthError:
                raise
            except EbayError as exc:
                stats["calls"] += exc.calls
                failed, streak, last_error = failed + 1, streak + 1, str(exc)
                if not isinstance(exc, _OutOfBudget):
                    failed_queries.append((key, text))
                log(f"ebay: query {key!r} failed, skipping it: {exc}")
                if streak >= MAX_CONSECUTIVE_FAILURES:
                    log(f"ebay: {streak} queries failed in a row, stopping the run")
                    stats["stopped"] = "errors"
                    break
                continue
            streak = 0
            begin()
            previous = _last_total(conn, key)  # read before record_query_run replaces it
            recorded = db.record_products(conn, store_id, observed_on, result.items,
                                          weight_parser=weight_parser, complete=False)
            db.record_query_run(conn, key, text, observed_on, result.complete, result.total)
            stats["queries_run"] += 1
            stats["calls"] += result.calls
            stats["items_seen"] += len(result.items)
            stats["new_listings"] += recorded["new_listings"]
            stats["skipped_currency"] += result.skipped_currency
            skipped_other += result.skipped_variations + result.skipped_other
            if result.error:
                log(f"ebay: query {key!r} is incomplete: {result.error}")
            if result.complete:
                stats["queries_complete"] += 1
                if ((previous >= MIN_PREVIOUS_FOR_DROP_CHECK
                        and result.total < previous * (1 - MAX_DROP))
                        or (previous > 0 and result.total == 0)):  # a lone empty 200 is not believed
                    log(f"ebay: query {key!r} returned {result.total} results, was {previous}; "
                        "not expiring its listings until the next run confirms it")
                else:
                    expired = db.expire_missing(conn, store_id, key, observed_on)
                    stats["gone"] += expired["gone"]
                    stats["inferred_sales"] += expired["inferred_sales"]
            if stats["queries_run"] % 250 == 0:
                log(f"ebay: {stats['queries_run']} queries done, {stats['calls']} calls")

        for key, text in failed_queries:
            db.record_query_run(conn, key, text, observed_on, False, _last_total(conn, key))
        status, error = "ok", None
        if not stats["queries_run"] and (failed or stats["stopped"] in ("quota", "errors")):
            status = "error"
            error = (f"no query succeeded ({failed} failed, stopped: {stats['stopped']}); "
                     f"last error: {last_error}")[:500]
        db.finish_run(conn, begin(), _timestamp(), status, stats["items_seen"], error)
    except BaseException as exc:  # incl. EbayAuthError: close the run row, then let it propagate
        if run_id is not None:
            try:
                db.finish_run(conn, run_id, _timestamp(), "error", stats["items_seen"],
                              f"{type(exc).__name__}: {exc}"[:500])
            except Exception:  # noqa: BLE001 - never mask the original error
                pass
        raise

    log(f"ebay: stopped ({stats['stopped']}) after {stats['queries_run']} queries / "
        f"{stats['calls']} calls; {failed} queries failed; skipped {stats['skipped_currency']} "
        f"non-USD and {skipped_other} other items")
    return stats
