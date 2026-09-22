"""eBay Browse API: live listings, with the printing still attached.

Why this exists. PPT reports graded prices by reading grades out of eBay
listing *titles*, and a title carries no printing -- so one card's "PSA 9
price" can be two printings averaged, and the pooled-printings check can only
infer that from how the sales scatter. On the cards worth buying it cannot
even do that: they sell too rarely to show a shape.

A listing does not have that problem. It names one card, one printing, one
price, and you can read it.

What this can and cannot get, which is not what it first appears:

  Browse API (open to any developer)      active listings -- asking prices
  Marketplace Insights API (restricted)   sold items, last 90 days

Sold comps are the gated one, and approval is commonly declined for
individuals. So this is built on Browse, and what it returns is *asks*, not
sales. That is a weaker thing in one way and a stronger thing in another:

  - Weaker: an ask is what someone hopes for, not what anyone paid. Never
    price a floor off one.
  - Stronger: it is a card you can actually buy today, at a price you can
    actually pay, of a printing you can actually read -- which is exactly the
    trade the ranking has been describing in the abstract.

Two honest uses follow. Find live raw copies under the walk-away price from
`Economics.max_raw_price`; and pull the active PSA 9 asks for a card to see
whether two printings are being conflated in them, which is a printing-aware
look at the question the split detector guesses at.

Stdlib only, like the PPT provider -- no dependency worth a listing search.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_BASE = "https://api.ebay.com"
TOKEN_PATH = "/identity/v1/oauth2/token"
SEARCH_PATH = "/buy/browse/v1/item_summary/search"
# The only scope the client-credentials grant needs for Browse. Anything
# user-specific (watchlists, offers) needs a user token and a redirect flow,
# and none of it is wanted here.
SCOPE = "https://api.ebay.com/oauth/api_scope"
MARKETPLACE = "EBAY_US"

# A token lasts about two hours. Cached so a run that searches thirty cards
# authenticates once, and so does the run after it.
TOKEN_CACHE = Path(__file__).resolve().parents[2] / "data" / ".ebay-token.json"
TOKEN_EARLY_REFRESH = 120.0   # seconds of slack, so a call never races expiry


def basic_auth(client_id: str, client_secret: str) -> str:
    """The Authorization header value for the client-credentials grant.

    Extracted so it can be checked against RFC 7617's own worked example
    rather than trusted. When eBay answers `invalid_client` to credentials
    that look correct, the first question is whether we are encoding them
    properly, and that question deserves an answer that is not "probably".
    """
    return "Basic " + base64.b64encode(
        f"{client_id}:{client_secret}".encode()).decode()


class EbayError(RuntimeError):
    """A request failed in a way the caller should see rather than retry."""


class EbayAuthError(EbayError):
    """Credentials were rejected. Retrying will not help."""


class EbayRateLimited(EbayError):
    """Too many calls. The daily Browse quota is generous but finite."""


def _post_form(url: str, data: dict, headers: dict, timeout: int = 30) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        if exc.code in (400, 401):
            # eBay answers a bad client id or secret with 400 invalid_client
            # as often as 401, so both are the same problem to the caller.
            raise EbayAuthError(
                f"eBay rejected the credentials ({exc.code}). Check "
                f"EBAY_CLIENT_ID and EBAY_CLIENT_SECRET are the *Production* "
                f"keyset, not Sandbox.\n  {detail}") from None
        raise EbayError(f"token request failed ({exc.code}): {detail}") from None


class EbayClient:
    """Application-level access to the Browse API."""

    name = "ebay"

    def __init__(self, client_id: str | None = None,
                 client_secret: str | None = None,
                 base: str | None = None):
        self.client_id = client_id or os.environ.get("EBAY_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("EBAY_CLIENT_SECRET")
        self.base = (base or os.environ.get("EBAY_API_BASE") or API_BASE).rstrip("/")
        self.calls = 0
        self._token: str | None = None
        self._expires: float = 0.0

    # -- auth ------------------------------------------------------------
    def _cached_token(self) -> str | None:
        if self._token and time.time() < self._expires - TOKEN_EARLY_REFRESH:
            return self._token
        try:
            blob = json.loads(TOKEN_CACHE.read_text())
        except (OSError, ValueError):
            return None
        if blob.get("base") != self.base or blob.get("id") != self._id_fingerprint():
            return None          # different app or environment; do not reuse
        if time.time() >= float(blob.get("expires", 0)) - TOKEN_EARLY_REFRESH:
            return None
        self._token, self._expires = blob.get("token"), float(blob["expires"])
        return self._token

    def _id_fingerprint(self) -> str:
        """Enough to tell two keysets apart in the cache; not the secret."""
        cid = self.client_id or ""
        return f"{cid[:6]}...{cid[-4:]}" if len(cid) > 12 else "unset"

    def token(self) -> str:
        cached = self._cached_token()
        if cached:
            return cached
        if not (self.client_id and self.client_secret):
            raise EbayAuthError(
                "EBAY_CLIENT_ID / EBAY_CLIENT_SECRET are not set. Put them in "
                "gap-tracker/.env (git-ignored) -- see .env.example.")
        blob = _post_form(
            self.base + TOKEN_PATH,
            {"grant_type": "client_credentials", "scope": SCOPE},
            {"Authorization": basic_auth(self.client_id, self.client_secret),
             "Content-Type": "application/x-www-form-urlencoded"})
        token = blob.get("access_token")
        if not token:
            raise EbayAuthError(f"no access_token in the response: {blob}")
        # `expires_in` is seconds from now; store the absolute time so a cache
        # read does not have to know when it was written.
        self._token = token
        self._expires = time.time() + float(blob.get("expires_in", 7200))
        try:
            TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
            TOKEN_CACHE.write_text(json.dumps(
                {"token": token, "expires": self._expires,
                 "base": self.base, "id": self._id_fingerprint()}))
            # The token is a bearer credential: readable only by this user.
            try:
                TOKEN_CACHE.chmod(0o600)
            except OSError:
                pass
        except OSError:
            pass          # a cache we cannot write is slower, not broken
        return token

    # -- requests --------------------------------------------------------
    def get(self, path: str, params: dict) -> dict:
        url = f"{self.base}{path}?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.token()}",
            "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE,
            "Accept": "application/json",
        })
        self.calls += 1
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            if exc.code == 401:
                # The cached token may simply have gone stale early.
                self._token, self._expires = None, 0.0
                raise EbayAuthError(f"unauthorised: {detail}") from None
            if exc.code == 403:
                raise EbayAuthError(
                    "eBay refused the call (403). The keyset is valid but this "
                    "API is not enabled for it -- Browse is open to all "
                    "developers, so this usually means the app is still "
                    f"Sandbox-only.\n  {detail}") from None
            if exc.code == 429:
                raise EbayRateLimited(f"rate limited: {detail}") from None
            raise EbayError(f"request failed ({exc.code}): {detail}") from None

    def search(self, query: str, limit: int = 10, *,
               filters: str | None = None, sort: str | None = None,
               category: str | None = None) -> dict:
        """Active listings matching `query`.

        `filters` is eBay's own filter syntax, passed through rather than
        wrapped: it is well documented, and a wrapper would be one more thing
        to keep in step with an API we cannot read from here.
        """
        params = {"q": query, "limit": max(1, min(int(limit), 200))}
        if filters:
            params["filter"] = filters
        if sort:
            params["sort"] = sort
        if category:
            params["category_ids"] = category
        return self.get(SEARCH_PATH, params)


# Fields that identify an eBay *user* rather than a card. eBay will not
# activate a production keyset until the application either runs a Marketplace
# Account Deletion endpoint or claims exemption, and the exemption is for
# applications that do not store eBay users' data. This tool takes the
# exemption, so these fields are shown at the moment of the search and never
# written anywhere. `persistable` is the only supported way to put a listing
# on disk, and `tests/test_ebay.py` fails if it stops removing them.
USER_FIELDS = ("seller", "feedback")


def persistable(row: dict) -> dict:
    """One summarised listing, with eBay user data removed.

    Anything that gets cached, logged or ranked goes through here first. The
    card fields -- title, price, condition, printing -- are what the tool is
    about; who is selling it matters only while you are looking at the screen.
    """
    return {k: v for k, v in row.items() if k not in USER_FIELDS}


def summarise(blob: dict, count: int = 5) -> list[dict]:
    """The fields worth having, pulled out of one search response.

    Deliberately small. What a listing is *for* here is the title and the
    price -- the title because it carries the printing that PPT's graded
    figures threw away, the price because it is a number you can act on.

    Carries `seller` and `feedback` for display only; see USER_FIELDS. Route
    anything bound for disk through `persistable` first.
    """
    out = []
    for item in (blob.get("itemSummaries") or [])[:count]:
        price = (item.get("price") or {})
        ship = None
        for opt in (item.get("shippingOptions") or []):
            cost = (opt.get("shippingCost") or {}).get("value")
            if cost is not None:
                ship = float(cost)
                break
        out.append({
            "title": item.get("title"),
            "price": float(price.get("value")) if price.get("value") else None,
            "currency": price.get("currency"),
            "shipping": ship,
            "condition": item.get("condition"),
            "seller": (item.get("seller") or {}).get("username"),
            "feedback": (item.get("seller") or {}).get("feedbackPercentage"),
            "buying": item.get("buyingOptions"),
            "url": item.get("itemWebUrl"),
            "image": (item.get("image") or {}).get("imageUrl"),
        })
    return out


# --- reading a listing title -------------------------------------------
#
# The point of the whole eBay detour. PPT matches graded sales by reading
# these same titles, but keeps only the grade -- so two printings of a card
# land in one "PSA 9 price" and the pooled-printings check can only guess at
# it from how the sales scatter, which on a card with five sales it cannot do
# at all.
#
# The printing is right there in the text. "1999 POKEMON BASE SET UNLIMITED #4
# CHARIZARD-HOLO PSA 9" says which Charizard. Reading it is not sophisticated;
# it is just work nobody did.

GRADERS = ("PSA", "BGS", "CGC", "SGC", "ACE", "TAG")

# Ordered: the first match wins, so the more specific pattern comes first.
# "1st Edition Shadowless" is a real combination and 1st Edition is the one
# that moves the price, but both are reported.
_PRINTINGS = (
    ("1st Edition", r"\b(?:1st|first)[\s\-]*ed(?:ition)?\b"),
    ("Shadowless", r"\bshadowless\b"),
    ("Unlimited", r"\bunlimited\b"),
    ("Reverse Holo", r"\breverse[\s\-]*(?:holo|foil)\w*\b"),
    ("Holo", r"\bholo(?:graphic|foil)?\b"),
    ("Promo", r"\b(?:promo|prerelease|pre[\s\-]release|staff)\b"),
)

_GRADE_RE = None
_PRINTING_RES = None


def _compiled():
    """Compile once, lazily, so importing the module stays cheap."""
    global _GRADE_RE, _PRINTING_RES
    import re
    if _GRADE_RE is None:
        # "PSA 9", "PSA9", "PSA 9.5", "BGS 9.5" -- and not "PSA 10 POPULATION"
        # style noise, which is why the number is bounded.
        _GRADE_RE = re.compile(
            r"\b(" + "|".join(GRADERS) + r")\s*[\-#]?\s*(10(?:\.0)?|[1-9](?:\.5)?)\b",
            re.IGNORECASE)
        _PRINTING_RES = [(name, re.compile(pat, re.IGNORECASE))
                         for name, pat in _PRINTINGS]
    return _GRADE_RE, _PRINTING_RES


def parse_title(title: str, condition: str | None = None) -> dict:
    """Pull grade and printing out of an eBay listing title.

    Returns {grader, grade, printings, graded}. `printings` is a list because
    "1st Edition Shadowless" is one card and naming only the first would lose
    half of what makes it expensive.

    `condition` is eBay's own field and settles the graded question better
    than the title does: a slab is listed as "Graded" whether or not the
    seller typed the grade. A title with no grade in it and a Graded condition
    is a slab we cannot read, which is worth knowing and different from a raw
    card.
    """
    grade_re, printing_res = _compiled()
    text = title or ""
    grader = grade = None
    match = grade_re.search(text)
    if match:
        grader = match.group(1).upper()
        grade = float(match.group(2))
    printings = [name for name, rx in printing_res if rx.search(text)]
    # "Reverse Holo" already implies holo; reporting both is noise.
    if "Reverse Holo" in printings and "Holo" in printings:
        printings.remove("Holo")
    graded = bool(match) or (condition or "").strip().lower() == "graded"
    return {"grader": grader, "grade": grade, "printings": printings,
            "graded": graded}


def search_text(name: str, set_name: str | None, number: str | None) -> str:
    """A query eBay will actually match.

    Our card names carry characters eBay titles do not: "Rayquaza ex δ" gets
    percent-encoded into the query and comes back with one raw match, where
    "Rayquaza ex Dragon Frontiers 97" finds the card. Non-ASCII is dropped
    rather than transliterated, because sellers write the same card as
    "delta", "Delta Species", "δ" or nothing at all, and the set and number
    already identify it.
    """
    bits = [name or "", set_name or "", (str(number or "").split("/")[0])]
    text = " ".join(b for b in bits if b)
    ascii_only = "".join(c if c.isascii() else " " for c in text)
    return " ".join(ascii_only.split())


def is_same_card(title: str, number: str | None,
                 set_name: str | None = None) -> bool:
    """Is this listing the card we asked for, or something the search dragged in?

    eBay keyword search is generous. A query for Rayquaza ex δ returned a
    $20,250 PSA 9 -- thirteen times PPT's figure for the card -- and listings
    marked 1st Edition for a 2006 set that never had one. Grouping those by
    printing and comparing the medians produced a confident, false "pooling
    costs little here".

    The card number is the discriminator: it is in nearly every title, and it
    is specific. Accepts "97/101", "#97" or 97 standing alone -- and not the
    97 inside 1997, which a bare substring test would take.
    """
    import re
    if not number:
        return True                     # nothing to check against
    want = str(number).split("/")[0].strip().lstrip("0") or "0"
    text = title or ""
    # \b will not match inside 1997, which is what makes this safe on the
    # dated titles that dominate vintage listings.
    if re.search(rf"(?<![\d/]){re.escape(want)}\s*/\s*\d+", text, re.IGNORECASE):
        return True                     # 97/101, the unambiguous form
    if re.search(rf"#\s*{re.escape(want)}\b", text, re.IGNORECASE):
        return True
    # A bare number is weaker, so ask for corroboration from the set name.
    if re.search(rf"\b{re.escape(want)}\b", text):
        if not set_name:
            return True
        words = [w for w in re.split(r"[^A-Za-z]+", set_name) if len(w) > 3]
        return any(re.search(rf"\b{re.escape(w)}", text, re.IGNORECASE)
                   for w in words)
    return False


def spread_of(values) -> float | None:
    """max/min for a group of asks, to say whether it is one population.

    A "Holo" group ranging $2,006 to $20,250 is not a printing, it is a
    mixture -- and a median taken across it means nothing. Cheap to compute
    and the difference between a finding and a fabrication.
    """
    vals = [v for v in values if v and v > 0]
    if len(vals) < 2:
        return None
    return max(vals) / min(vals)


def delivered(item: dict) -> tuple[float, bool]:
    """(what it costs to your door, whether that is actually known).

    Shipping is None whenever eBay returns no cost for a listing -- local
    pickup, freight, or a rate it will only calculate against a real address.
    Folding that into the total as zero is the one rounding this tool must
    never make: it quietly turns an unknown into the cheapest possible answer,
    on the number that decides whether you buy. So it is returned as a flag,
    and the caller says "+ shipping" rather than inventing a figure.
    """
    price = float(item.get("price") or 0)
    ship = item.get("shipping")
    if ship is None:
        return price, False
    return price + float(ship), True
