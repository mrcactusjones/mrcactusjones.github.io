"""PokemonPriceTracker adapter.

IMPORTANT: the exact endpoint and response shape could not be verified while
this was written (their docs were unreachable from the build environment), so
this adapter does not hard-code field paths. It walks the response and matches
keys by pattern, which survives most shape differences. If it comes back empty:

    python3 run.py probe --card base1-4

prints the raw JSON so the mapping can be pinned down in `EXPLICIT_PATHS`
below, or corrected in one place here.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from ..econ import Quote
from ..store import iso, utcnow

DEFAULT_BASE = "https://www.pokemonpricetracker.com/api/v2"
# Wide enough that the right printing is in the page; matching is exact,
# so extra results cost nothing but are cheap insurance.
SEARCH_LIMIT = 1  # per-card billing: every extra result is another credit

# Fill these in after a `probe` run to bypass pattern matching entirely.
# Values are dotted paths into the response, e.g. "data.0.prices.psa10.market".
EXPLICIT_PATHS: dict[str, str] = {}

_PRICEY = re.compile(r"(price|market|value|average|avg|median|mean|last)", re.I)
_COUNTY = re.compile(r"(count|sales|volume|num|qty|listings)", re.I)
_PSA9 = re.compile(r"(psa[^0-9]{0,3}9|grade[^0-9]{0,3}9)(?![0-9])", re.I)
_PSA10 = re.compile(r"(psa[^0-9]{0,3}10|grade[^0-9]{0,3}10)", re.I)
_PSA8 = re.compile(r"(psa[^0-9]{0,3}8|grade[^0-9]{0,3}8)(?![0-9])", re.I)
_RAW = re.compile(r"(raw|ungraded|loose|tcgplayer|market)", re.I)
# "ungraded" contains "graded", so the guard must not trip on the raw field.
_GRADED_ANY = re.compile(r"(psa|bgs|cgc|sgc|(?<!un)grade)", re.I)


def _flatten(node, prefix: str = "") -> list[tuple[str, object]]:
    out: list[tuple[str, object]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            out.extend(_flatten(value, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(node, list):
        for idx, value in enumerate(node[:25]):  # bound pathological responses
            out.extend(_flatten(value, f"{prefix}.{idx}"))
    else:
        out.append((prefix, node))
    return out


def _dig(blob, path: str):
    node = blob
    for part in path.split("."):
        if isinstance(node, list):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(node, dict):
            node = node.get(part)
        else:
            return None
    return node


def _as_number(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace("$", "").replace(",", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _pick(pairs, grade_re, want_count: bool = False) -> float | None:
    """Best numeric value whose path names this grade and looks like a price."""
    shape_re = _COUNTY if want_count else _PRICEY
    best: tuple[int, float] | None = None
    for path, value in pairs:
        if not grade_re.search(path):
            continue
        number = _as_number(value)
        if number is None or number < 0:
            continue
        leaf = path.rsplit(".", 1)[-1]
        if want_count and number != int(number):
            continue
        # Prefer a price-ish leaf name; fall back to any number under the grade.
        score = 2 if shape_re.search(leaf) else (1 if shape_re.search(path) else 0)
        if not want_count and score == 0 and number <= 0:
            continue
        if best is None or score > best[0]:
            best = (score, number)
    return best[1] if best else None


def _pick_raw(pairs) -> float | None:
    best: tuple[int, float] | None = None
    for path, value in pairs:
        if _GRADED_ANY.search(path):
            continue  # never let a graded field masquerade as the raw price
        if not _RAW.search(path):
            continue
        number = _as_number(value)
        if number is None or number <= 0:
            continue
        leaf = path.rsplit(".", 1)[-1]
        score = 2 if _PRICEY.search(leaf) else 1
        if best is None or score > best[0]:
            best = (score, number)
    return best[1] if best else None


def parse_population(blob: dict) -> dict | None:
    """PSA grade counts out of a /population response.

    Shapes vary (populationByGrader.PSA.grades.g10, or flat g10 keys), so pull
    anything that looks like a grade bucket.
    """
    node = blob
    for key in ("data", "populationByGrader", "PSA", "psa"):
        if isinstance(node, dict) and key in node:
            node = node[key]
    if isinstance(node, list) and node:
        node = node[0]
    if not isinstance(node, dict):
        return None

    source = node.get("grades") if isinstance(node.get("grades"), dict) else node
    grades: dict[str, int] = {}
    for key, value in (source or {}).items():
        match = re.fullmatch(r"g?(\d{1,2})(?:\.\d)?", str(key).lower())
        if match and isinstance(value, (int, float)):
            grades[match.group(1)] = int(value)
    if not grades:
        return None
    total = sum(grades.values())
    return {"grades": grades, "total": total,
            "gem_rate": (grades.get("10", 0) / total) if total else None}


class PopulationUnavailable(Exception):
    """The plan does not include population data."""


class OutOfCredits(Exception):
    """The daily credit allowance is gone; stop, don't keep asking."""


class PPTError(Exception):
    """An HTTP error that keeps the server's own explanation attached."""

    def __init__(self, code: int, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"HTTP {code}: {detail}")


def _norm(value) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def results_of(blob) -> list[dict]:
    """The list of card records, whatever the envelope is called."""
    if isinstance(blob, list):
        return [r for r in blob if isinstance(r, dict)]
    if isinstance(blob, dict):
        for key in ("data", "cards", "results", "items"):
            value = blob.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
        if any(k in blob for k in ("name", "cardNumber", "setName")):
            return [blob]
    return []


def _norm_number(value) -> str:
    """Card numbers come as '19', 'H14/H32', '25/165'. Compare the first part."""
    return _norm(str(value or "").split("/")[0])


def pick_match(results: list[dict], card: dict) -> tuple[dict | None, str]:
    """Choose the record that really is this card.

    Their search is fuzzy, and a set can hold two cards with the same name --
    Aquapolis has both Kingdra #19 and holo Kingdra H14. Name and set agreeing
    is therefore NOT identification; the card number has to agree too, or the
    catalog id has to match outright.
    """
    want_id = _norm(card.get("id"))
    want_num = _norm_number(card.get("number"))
    want_set = _norm(card.get("set_name"))

    # Best case: they carry a pokemontcg.io-style id, so this is exact.
    if want_id:
        for record in results:
            if _norm(record.get("externalCatalogId")) == want_id:
                return record, f"exact externalCatalogId ({record.get('externalCatalogId')})"

    for record in results:
        got_num = _norm_number(record.get("cardNumber") or record.get("number"))
        got_set = _norm(record.get("setName") or record.get("set"))
        if not (want_num and got_num and want_num == got_num):
            continue
        if want_set and got_set and not (
                want_set == got_set or want_set in got_set or got_set in want_set):
            continue
        return record, f"set+number ({record.get('setName')} #{record.get('cardNumber')})"

    seen = ", ".join(
        f"{r.get('setName')} #{r.get('cardNumber')}" for r in results[:4]) or "nothing"
    return None, f"no confident match; need id or set+number. Saw: {seen}"


def _grade_block(block) -> tuple[float | None, str | None, str | None, int]:
    """(price, confidence, last_sale, count) for one grade.

    Prefers the provider's own weighted estimate over a raw average: with one
    or two sales, `averagePrice` is just that sale, while smartMarketPrice is
    filtered and carries a confidence rating.
    """
    if not isinstance(block, dict):
        return None, None, None, 0
    smart = block.get("smartMarketPrice") or {}
    price = _as_number(smart.get("price"))
    confidence = smart.get("confidence")
    if price is None:
        price = _as_number(block.get("medianPrice"))
    if price is None:
        price = _as_number(block.get("averagePrice"))
    count = int(_as_number(block.get("count")) or 0)
    return price, confidence, block.get("lastSaleDate"), count


def extract_graded(record: dict) -> Quote | None:
    """Read the documented ebay.salesByGrade shape.

    Returns None when the record isn't in that shape, so the generic
    pattern-matching fallback can take over.
    """
    ebay = record.get("ebay")
    if not isinstance(ebay, dict):
        return None
    grades = ebay.get("salesByGrade")
    if not isinstance(grades, dict):
        return None
    outliers = ebay.get("smartPriceOutlierByGrade") or {}

    psa9, c9, last9, n9 = _grade_block(grades.get("psa9"))
    psa10, c10, last10, n10 = _grade_block(grades.get("psa10"))
    psa8, _, _, _ = _grade_block(grades.get("psa8"))
    cgc9, _, _, ncgc9 = _grade_block(grades.get("cgc9"))
    cgc10, _, _, ncgc10 = _grade_block(grades.get("cgc10"))

    # Every PSA grade present, for inferring the grade mix.
    mix: dict[str, int] = {}
    for key, block in grades.items():
        match = re.fullmatch(r"psa(\d{1,2})", str(key).lower())
        if match and isinstance(block, dict):
            count = int(_as_number(block.get("count")) or 0)
            if count:
                mix[match.group(1)] = count

    prices = record.get("prices") or {}
    raw = _as_number(prices.get("market")) or _as_number(prices.get("low"))

    return Quote(
        raw=raw, psa9=psa9, psa10=psa10, psa8=psa8,
        sales_9=n9, sales_10=n10,
        psa9_confidence=c9, psa10_confidence=c10,
        psa9_last_sale=last9, psa10_last_sale=last10,
        psa9_outlier=bool(outliers.get("psa9")),
        psa10_outlier=bool(outliers.get("psa10")),
        cgc9=cgc9, cgc10=cgc10, cgc9_sales=ncgc9, cgc10_sales=ncgc10,
        psa_sales_mix=mix or None,
        tcgplayer_id=str(record.get("tcgPlayerId")) if record.get("tcgPlayerId") else None,
        as_of=iso(utcnow()), source="ppt",
    )


def extract_quote(blob: dict) -> Quote:
    """Pull a Quote out of a card record.

    Tries the documented shape first, then falls back to pattern matching so a
    response format change degrades instead of breaking.
    """
    known = extract_graded(blob)
    if known is not None and (known.psa9 is not None or known.psa10 is not None):
        return known

    pairs = _flatten(blob)
    if EXPLICIT_PATHS:
        get = lambda k: _as_number(_dig(blob, EXPLICIT_PATHS[k])) if k in EXPLICIT_PATHS else None
        return Quote(
            raw=get("raw"), psa9=get("psa9"), psa10=get("psa10"), psa8=get("psa8"),
            sales_9=int(get("sales_9") or 0), sales_10=int(get("sales_10") or 0),
            as_of=iso(utcnow()), source="ppt",
        )
    return Quote(
        raw=_pick_raw(pairs),
        psa9=_pick(pairs, _PSA9),
        psa10=_pick(pairs, _PSA10),
        psa8=_pick(pairs, _PSA8),
        sales_9=int(_pick(pairs, _PSA9, want_count=True) or 0),
        sales_10=int(_pick(pairs, _PSA10, want_count=True) or 0),
        as_of=iso(utcnow()),
        source="ppt",
    )


CANDIDATE_BASES = [
    "https://www.pokemonpricetracker.com/api/v2",
    "https://www.pokemonpricetracker.com/api/v1",
    "https://www.pokemonpricetracker.com/api",
    "https://api.pokemonpricetracker.com/v2",
]
CANDIDATE_PATHS = ["cards?search=charizard&limit=1", "sets?limit=1"]


def discover(api_key: str) -> list[tuple[str, str]]:
    """Probe candidate endpoints and report what each one answers.

    A 404 costs no credits, so this is a cheap way to find the live route
    without guessing in code.
    """
    results = []
    for base in CANDIDATE_BASES:
        for path in CANDIDATE_PATHS:
            url = f"{base}/{path}"
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": "gap-tracker/0.1 (personal research tool)"})
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = resp.read(400).decode("utf-8", "replace")
                    results.append((url, f"{resp.status} OK  {body[:220]}"))
            except urllib.error.HTTPError as exc:
                detail = exc.read(200).decode("utf-8", "replace").replace("\n", " ")
                results.append((url, f"{exc.code} {exc.reason}  {detail[:160]}"))
            except Exception as exc:  # noqa: BLE001 - report anything, keep probing
                results.append((url, f"error: {exc}"))
            time.sleep(1.1)
    return results


FILTER_CANDIDATES = [
    # (param name, how to build its value from a universe card)
    ("externalCatalogId", lambda c: c.get("id")),
    ("externalId", lambda c: c.get("id")),
    ("catalogId", lambda c: c.get("id")),
    ("cardId", lambda c: c.get("id")),
    ("tcgPlayerId", lambda c: c.get("tcgplayer_id")),
    ("cardNumber", lambda c: c.get("number")),
    ("number", lambda c: c.get("number")),
    ("set", lambda c: c.get("set_name")),
    ("setName", lambda c: c.get("set_name")),
]


def credits_from_error(detail: str) -> str | None:
    """Pull the remaining-credit figure out of a 429 body."""
    try:
        blob = json.loads(detail)
    except (ValueError, TypeError):
        return None
    if "available" in blob:
        return f"{blob.get('available')} credit(s) left, resets {blob.get('resetsAt')}"
    return blob.get("message")


class PPTProvider:
    name = "ppt"

    def __init__(self, credits_per_card: int = 2, api_key: str | None = None,
                 base: str | None = None, min_interval: float = 1.1,
                 search_limit: int = SEARCH_LIMIT, include_graded: bool = True,
                 wide_limit: int = 5):
        self.search_limit = max(1, search_limit)
        self.include_graded = include_graded
        # Billing is per card returned, so the cost of one lookup is the limit.
        self.credits_per_card = self.search_limit * (2 if include_graded else 1)
        self.api_key = api_key or os.environ.get("PPT_API_KEY")
        if not self.api_key:
            raise SystemExit("PPT_API_KEY is not set. Export it, or run with --provider mock.")
        self.base = (base or os.environ.get("PPT_API_BASE") or DEFAULT_BASE).rstrip("/")
        self.wide_limit = wide_limit
        self.credits_used = 0
        self.min_interval = min_interval  # stay well under 60 req/min
        self._last_call = 0.0

    def _request(self, path: str, params: dict) -> dict:
        gap = self.min_interval - (time.monotonic() - self._last_call)
        if gap > 0:
            time.sleep(gap)
        url = f"{self.base}/{path.lstrip('/')}?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": "gap-tracker/0.1 (personal research tool)",
        })
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            # The body explains *why* -- discarding it turns a one-line fix
            # into a guessing game.
            try:
                detail = exc.read(500).decode("utf-8", "replace").strip()
            except Exception:  # noqa: BLE001
                detail = ""
            raise PPTError(exc.code, detail or exc.reason) from None
        finally:
            self._last_call = time.monotonic()

    # Kept minimal on purpose: `probe --discover` proved the API accepts
    # search+limit and rejects invented parameters with a 400.
    # includeEbay is what returns ebay.salesByGrade.psa9 / .psa10; without it
    # the response carries TCGplayer raw prices only.
    EXTRA_PARAMS: dict[str, str] = {"includeEbay": "true"}

    def search_text(self, card: dict) -> str:
        return f"{card.get('name', '')} {card.get('set_name', '')}".strip()

    def raw_response(self, card: dict, search: str | None = None,
                     graded: bool = True, filters: dict | None = None) -> dict:
        """Unparsed response -- what `run.py probe` prints.

        graded=False omits the eBay block, which is what makes a call cost two
        credits instead of one. Identity resolution doesn't need prices.
        """
        text = self.search_text(card) if search is None else search
        params = {"search": text, "limit": self.search_limit}
        if graded and self.include_graded:
            params.update(self.EXTRA_PARAMS)
        if filters:
            params.update(filters)
        return self._request("cards", params)

    def _attempt(self, card: dict, limit: int) -> tuple[list[dict], int]:
        """One lookup. Returns (records, credits spent).

        Billing is on the requested limit, not the rows that come back, so a
        miss costs exactly as much as a hit.
        """
        previous = self.search_limit
        self.search_limit = limit
        try:
            filters = {"set": card["set_name"]} if card.get("set_name") else None
            blob = self.raw_response(card, search=card.get("name"), filters=filters)
        finally:
            self.search_limit = previous
        cost = limit * (2 if self.include_graded else 1)
        self.credits_used += cost
        return results_of(blob), cost

    def fetch_population(self, tcgplayer_id: str) -> dict | None:
        """PSA population for one card. Premium data: 2 credits, higher plans only."""
        try:
            blob = self._request("population", {"tcgPlayerId": tcgplayer_id})
        except PPTError as exc:
            if exc.code in (401, 402, 403):
                raise PopulationUnavailable(exc.detail) from None
            if exc.code == 429:
                raise OutOfCredits(credits_from_error(exc.detail) or exc.detail) from None
            return None
        self.credits_used += 2
        return parse_population(blob)

    def next_cost(self) -> int:
        """Credits the next narrow lookup will cost."""
        return self.search_limit * (2 if self.include_graded else 1)

    def fetch(self, card: dict) -> Quote | None:
        try:
            records, _ = self._attempt(card, self.search_limit)
            record, why = pick_match(records, card)

            # A narrow lookup returns the provider's top guess, which for a set
            # holding two same-named cards can be the wrong printing. Widen once
            # rather than discarding the card.
            if record is None and self.wide_limit > self.search_limit:
                records, _ = self._attempt(card, self.wide_limit)
                record, why = pick_match(records, card)
                why += " (after widening)"

            if record is None:
                print(f"  ? {card['id']}: {why}")
                return None

            quote = extract_quote(record)
            if quote.psa9 is None and quote.psa10 is None:
                return None
            if quote.raw is None:
                quote.raw = card.get("raw_hint")
            return quote
        except PPTError as exc:
            if exc.code in (401, 403):
                raise SystemExit(f"PPT rejected the API key ({exc.code}): {exc.detail}")
            if exc.code == 429:
                raise OutOfCredits(credits_from_error(exc.detail) or exc.detail) from None
            print(f"  ! {card['id']}: {exc}")
            return None
        except (urllib.error.URLError, TimeoutError) as exc:
            print(f"  ! {card['id']}: {exc}")
            return None
