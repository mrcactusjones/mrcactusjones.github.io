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


def pick_match(results: list[dict], card: dict) -> tuple[dict | None, str]:
    """Choose the record that really is this card.

    Their search is fuzzy and returns whatever is closest, so taking the first
    result would quietly price the wrong card. Require corroboration from the
    card number and the set/name before believing a match.
    """
    want_num, want_set = _norm(card.get("number")), _norm(card.get("set_name"))
    want_name = _norm(card.get("name"))
    best_score, best, why = 0, None, "no results"

    for record in results:
        got_num = _norm(record.get("cardNumber") or record.get("number"))
        got_set = _norm(record.get("setName") or record.get("set"))
        got_name = _norm(record.get("name"))
        score, notes = 0, []
        if want_num and got_num == want_num:
            score += 3; notes.append("number")
        if want_set and got_set == want_set:
            score += 2; notes.append("set")
        elif want_set and got_set and (want_set in got_set or got_set in want_set):
            score += 1; notes.append("set~")
        if want_name and got_name and (want_name in got_name or got_name.startswith(want_name)):
            score += 2; notes.append("name")
        if score > best_score:
            best_score, best, why = score, record, "+".join(notes)

    # 4 = set + name, or number + name. Anything less isn't identification.
    if best_score >= 4:
        return best, f"matched on {why} (score {best_score})"
    return None, f"no confident match (best score {best_score}, {why})"


def extract_quote(blob: dict) -> Quote:
    """Pull a Quote out of an arbitrary response shape."""
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


class PPTProvider:
    name = "ppt"

    def __init__(self, credits_per_card: int = 2, api_key: str | None = None,
                 base: str | None = None, min_interval: float = 1.1):
        self.credits_per_card = credits_per_card
        self.api_key = api_key or os.environ.get("PPT_API_KEY")
        if not self.api_key:
            raise SystemExit("PPT_API_KEY is not set. Export it, or run with --provider mock.")
        self.base = (base or os.environ.get("PPT_API_BASE") or DEFAULT_BASE).rstrip("/")
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
        finally:
            self._last_call = time.monotonic()

    def raw_response(self, card: dict) -> dict:
        """Unparsed response -- what `run.py probe` prints."""
        query = f"{card.get('name', '')} {card.get('set_name', '')}".strip()
        return self._request("cards", {
            "search": query,
            "number": card.get("number") or "",
            "limit": 5,
            "includePsa": "true",
            "includeHistory": "false",
        })

    def fetch(self, card: dict) -> Quote | None:
        try:
            blob = self.raw_response(card)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise SystemExit(f"PPT rejected the API key ({exc.code}).")
            if exc.code == 429:
                raise SystemExit("PPT rate limit / daily credits exhausted. Resume tomorrow.")
            print(f"  ! {card['id']}: HTTP {exc.code}")
            return None
        except (urllib.error.URLError, TimeoutError) as exc:
            print(f"  ! {card['id']}: {exc}")
            return None

        record, why = pick_match(results_of(blob), card)
        if record is None:
            print(f"  ? {card['id']}: {why}")
            return None

        quote = extract_quote(record)
        if quote.psa9 is None and quote.psa10 is None:
            return None
        if quote.raw is None:
            quote.raw = card.get("raw_hint")  # fall back to the free catalog price
        return quote
