"""DB -> static JSON for the dashboard (DESIGN.md section 5).

Everything is loaded in a handful of bulk queries and computed in Python: the
daily series is a sweep over each disc's change-only observations, so the cost
is proportional to (changes + scrape dates), never one query per variant/date.

Semantics worth knowing:
* Only `matched` listings become discs; review/ignored/unparsed listings are
  counted in `stats` but never exported. Series are built from the *current*
  parse of a listing (condition included), so a re-parse rewrites history views.
* A store contributes to a date only if it has an `ok` run on that date: a
  variant's state is forward-filled from its latest observation, but never
  across a store outage (error/missing run).
* `median` is taken over in-stock variants (one price per variant), so a store
  with many in-stock copies weighs more than a store with one.
* A "live" block (`new`/`used` in index.json) exists when at least one non-gone
  priced variant of that condition exists. If none is in stock, `min`/`median`
  fall back to the last listed prices, `stores_in_stock` is 0 and `change_*`
  are null; consumers should key "available" off `stores_in_stock`.
* A disc is exported if it has a live block or any series point; otherwise
  there is nothing to show and it is skipped.
* A variant only counts (live block, series, listings) when it has a price > 0:
  NULL or 0.00 prices are placeholders, and a $0 minimum would wreck the charts.
* Scraped strings are untrusted: listing URLs are only exported as http(s) URLs
  (otherwise rebuilt from the store's base_url + handle), and slugs are clipped
  so a long scraped name can never exceed the filesystem's file name limit.
* Stale history files are deleted only after the new index.json is in place, so a
  failed export never leaves the published index pointing at deleted files.

Marketplace (eBay) additions, DESIGN.md section 10.4, all additive:
* A store has a `kind` (`retail` | `marketplace`); history listings carry it as `store_kind`.
  A marketplace is one "store" for the series rules: it counts on a date only if its own run was
  `ok`, and its listings are forward-filled across days its searches did not see them.
* `series` stays the all-stores aggregate. Only a disc that has marketplace data also gets
  `series_by_kind` (history) and `retail` (index; the `new`/`used` blocks computed from the retail
  stores alone, which the home page needs to hide marketplace prices; a block, not a flag, because
  min/median/change cannot be derived from the blended ones).
* Sales come from the `sales` table, for listings whose *current* parse is this disc. A sale is
  `confirmed` only if the DB says so (confidence `confirmed` from a sold-data source); everything else
  - above all `inferred_disappeared` - is exported with confidence `low`, because a listing that
  vanished may simply have been delisted. A confirmed sale supersedes an inferred one of the same
  listing (it is the same event). Placeholder prices (<= 0) and unparseable dates are skipped.
  `sales_30d` covers sale dates from `today - 30 days` to `today` inclusive; `count` is the number of
  sales (rows), `median` the median price in dollars. `stats.sales_*` count every sale row in the DB.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from bisect import bisect_right
from collections import Counter
from datetime import date, timedelta
from operator import itemgetter
from pathlib import Path
from typing import NamedTuple, Sequence
from urllib.parse import urlsplit

from .models import key_slug

CONDITIONS = ("new", "used")
KINDS = ("retail", "marketplace")
CHANGE_DAYS = (7, 30)
SALES_DAYS = 30  # window of the index `sales_30d`
MAX_SALES = 100  # history `sales` rows per disc
INFERRED_SOURCE = "inferred_disappeared"
MAX_SLUG = 100  # chars; history/<slug>.json.tmp must stay far below the 255 byte file name limit


class _Listing(NamedTuple):
    id: int
    store_id: str
    title: str
    handle: str | None
    url: str | None
    last_seen: str
    gone: bool
    manufacturer: str
    mold: str
    plastic: str
    edition: str
    player: str
    condition: str
    disc_type: str
    key: str


class _Variant(NamedTuple):
    pk: int
    listing: _Listing
    variant_id: int
    weight_g: int | None
    price_cents: int | None
    compare_at_cents: int | None
    available: bool
    gone: bool
    last_seen: str


class _Sale(NamedTuple):
    listing: _Listing
    sold_on: str  # YYYY-MM-DD
    price_cents: int
    source: str
    confirmed: bool


class _Disc:
    __slots__ = ("key", "listings", "variants")

    def __init__(self, key: str) -> None:
        self.key = key
        self.listings: list[_Listing] = []
        self.variants: list[_Variant] = []


# --------------------------------------------------------------------------- loading

_MATCHED = "l.status = 'matched' AND l.disc_key IS NOT NULL AND l.disc_key != ''"


def _load_stores(conn: sqlite3.Connection) -> list[tuple[str, str, str, str, str]]:
    """(id, name, base_url, currency, kind) sorted by name; an unknown kind counts as retail."""
    rows = [(r[0], r[1], r[2], r[3] or "USD", "marketplace" if r[4] == "marketplace" else "retail")
            for r in conn.execute("SELECT id, name, base_url, currency, kind FROM stores")]
    rows.sort(key=lambda r: (r[1].casefold(), r[0]))
    return rows


def _load_ok_dates(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """store_id -> sorted scrape dates (days with at least one ok run)."""
    out: dict[str, list[str]] = {}
    for store_id, on in conn.execute(
            "SELECT DISTINCT store_id, observed_on FROM runs WHERE status = 'ok' "
            "ORDER BY store_id, observed_on"):
        out.setdefault(store_id, []).append(on)
    return out


def _load_discs(conn: sqlite3.Connection) -> dict[str, _Disc]:
    listings: dict[int, _Listing] = {}
    discs: dict[str, _Disc] = {}
    for r in conn.execute(
            "SELECT l.id, l.store_id, l.title, l.handle, l.url, l.last_seen, l.gone, "
            "l.manufacturer, l.mold, l.plastic, l.edition, l.player, l.condition, "
            "l.disc_type, l.disc_key FROM listings l WHERE " + _MATCHED):
        lst = _Listing(r[0], r[1], r[2], r[3], r[4], r[5], bool(r[6]), r[7] or "", r[8] or "",
                       r[9] or "", r[10] or "", r[11] or "", "used" if r[12] == "used" else "new",
                       r[13] or "", r[14])
        listings[lst.id] = lst
        discs.setdefault(lst.key, _Disc(lst.key)).listings.append(lst)
    for r in conn.execute(
            "SELECT v.id, v.listing_id, v.variant_id, v.weight_g, v.price_cents, "
            "v.compare_at_cents, v.available, v.gone, v.last_seen FROM variants v "
            "JOIN listings l ON l.id = v.listing_id WHERE " + _MATCHED):
        lst = listings.get(r[1])
        if lst is not None:
            discs[lst.key].variants.append(
                _Variant(r[0], lst, r[2], r[3], r[4], r[5], bool(r[6]), bool(r[7]), r[8] or ""))
    return discs


def _load_observations(conn: sqlite3.Connection) -> dict[int, list[tuple[str, int | None, bool]]]:
    """variant pk -> [(date, price_cents, in_stock)] ascending, matched listings only."""
    obs: dict[int, list[tuple[str, int | None, bool]]] = {}
    for pk, on, price, avail, gone in conn.execute(
            "SELECT o.variant_pk, o.observed_on, o.price_cents, o.available, o.gone "
            "FROM observations o JOIN variants v ON v.id = o.variant_pk "
            "JOIN listings l ON l.id = v.listing_id WHERE " + _MATCHED +
            " ORDER BY o.variant_pk, o.observed_on"):
        obs.setdefault(pk, []).append((on, price, bool(avail) and not gone and _priced(price)))
    return obs


_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def _is_confirmed(source: str | None, confidence: str | None) -> bool:
    """Only a sold-data source that the DB marks `confirmed` is a confirmed sale. An inferred
    (disappeared) listing can never be one, whatever its confidence column says."""
    return confidence == "confirmed" and source != INFERRED_SOURCE


def _sale_day(value: str) -> str | None:
    """YYYY-MM-DD of a stored sale date (a datetime is cut to its day); None when malformed."""
    day = value[:10]
    if not _DAY.fullmatch(day):
        return None
    try:
        date.fromisoformat(day)
    except ValueError:
        return None
    return day


def _load_sales(conn: sqlite3.Connection) -> list[tuple[int, str, int, str, bool]]:
    """(listing_id, sold_on, price_cents, source, confirmed) for every sale row."""
    return [(r[0], str(r[1] or ""), r[2], str(r[3] or ""), _is_confirmed(r[3], r[4]))
            for r in conn.execute(
                "SELECT listing_id, sold_on, price_cents, source, confidence FROM sales ORDER BY id")]


def _sale_totals(rows: Sequence[tuple[int, str, int, str, bool]]) -> tuple[int, int]:
    """(confirmed, inferred) over all sale rows; an inferred sale of a listing that also has a
    confirmed one is the same event and counts once."""
    confirmed_ids = {r[0] for r in rows if r[4]}
    return (sum(1 for r in rows if r[4]),
            sum(1 for r in rows if not r[4] and r[0] not in confirmed_ids))


def _disc_sales(rows: Sequence[tuple[int, str, int, str, bool]],
                listings: dict[int, _Listing]) -> dict[str, list[_Sale]]:
    """disc key -> its sales: matched listings only, dated, priced, inferred ones superseded
    by a confirmed sale of the same listing dropped."""
    confirmed_ids = {r[0] for r in rows if r[4]}
    out: dict[str, list[_Sale]] = {}
    for listing_id, sold_on, price, source, confirmed in rows:
        lst = listings.get(listing_id)
        day = _sale_day(sold_on)
        if lst is None or day is None or not _priced(price):
            continue
        if not confirmed and listing_id in confirmed_ids:
            continue
        out.setdefault(lst.key, []).append(_Sale(lst, day, price, source, confirmed))
    return out


# --------------------------------------------------------------------------- math

def _min_median(prices: Sequence[int]) -> tuple[int, int]:
    """(min, median) in cents of a non-empty ascending list; a half cent rounds up."""
    n = len(prices)
    mid = n // 2
    median = prices[mid] if n % 2 else (prices[mid - 1] + prices[mid] + 1) // 2
    return prices[0], median


def _dollars(cents: int | None) -> float | None:
    return None if cents is None else cents / 100


def _priced(cents: int | None) -> bool:
    return cents is not None and cents > 0


def _http_url(url: str | None) -> str | None:
    """`url` if it is an absolute http(s) URL without credentials, else None (scraped data is untrusted).

    A backslash, whitespace or a control character anywhere in it also refuses the URL: browsers read
    `https://evil.example\\www.ebay.com/x` as host `evil.example` (and drop tabs/newlines), while urllib
    sees another host, so such a string would pass the checks here and open somewhere else."""
    if not url:
        return None
    url = url.strip()
    if "\\" in url or any(c.isspace() or not c.isprintable() for c in url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc or "@" in parts.netloc:
        return None
    return url


def _change(dates: Sequence[str], mins: Sequence[int], today: date, days: int,
            current: int | None) -> float | None:
    """Fractional change of `current` vs the series point at or before today - days."""
    if current is None:
        return None
    i = bisect_right(dates, (today - timedelta(days=days)).isoformat()) - 1
    if i < 0 or mins[i] <= 0:
        return None
    return round((current - mins[i]) / mins[i], 4) or 0.0  # `or` drops a -0.0


def _display(values) -> str:
    """Most common whitespace-normalised value; ties prefer mixed case, then sort order."""
    counts = Counter(" ".join(v.split()) for v in values)
    if not counts:
        return ""
    return min(counts, key=lambda s: (-counts[s], s.isupper(), s.islower(), s))


class _SeriesBuilder:
    """Daily min/median/store-count series per (disc, condition[, store kind])."""

    def __init__(self, ok_dates: dict[str, list[str]],
                 obs: dict[int, list[tuple[str, int | None, bool]]],
                 kinds: dict[str, str] | None = None) -> None:
        self._ok_dates = ok_dates
        self._ok_sets = {s: frozenset(d) for s, d in ok_dates.items()}
        self._obs = obs
        self._kinds = kinds or {}  # store id -> retail | marketplace
        self._axes: dict[tuple[str, ...], list[str]] = {}

    def has_kind(self, disc: _Disc, kind: str) -> bool:
        """True if some variant of the disc belongs to a store of this kind and has observations."""
        return any(v.pk in self._obs and self._kinds.get(v.listing.store_id, "retail") == kind
                   for v in disc.variants)

    def _axis(self, stores: tuple[str, ...]) -> list[str]:
        axis = self._axes.get(stores)
        if axis is None:
            if len(stores) == 1:
                axis = self._ok_dates.get(stores[0], [])
            else:
                axis = sorted(set().union(*(self._ok_sets.get(s, ()) for s in stores)))
            self._axes[stores] = axis
        return axis

    def build(self, disc: _Disc, condition: str,
              kind: str | None = None) -> list[tuple[str, int, int, int]]:
        """[(date, min_cents, median_cents, stores_in_stock)], one point per scrape date
        on which at least one counted variant is in stock. `kind` restricts the series to the
        stores of that kind (None = all stores); the date rules do not change, so a store outage
        day is skipped for a marketplace exactly as for a retail store."""
        var_obs = [(v.listing.store_id, self._obs[v.pk]) for v in disc.variants
                   if v.listing.condition == condition and v.pk in self._obs
                   and (kind is None or self._kinds.get(v.listing.store_id, "retail") == kind)]
        if not var_obs:
            return []
        stores = tuple(sorted({s for s, _ in var_obs}))
        sidx = {s: i for i, s in enumerate(stores)}
        vstore = [sidx[s] for s, _ in var_obs]
        events = sorted(
            ((d, i, price if in_stock else None)
             for i, (_, ob) in enumerate(var_obs) for d, price, in_stock in ob),
            key=itemgetter(0))
        ok = [self._ok_sets.get(s, frozenset()) for s in stores]
        multi = len(stores) > 1

        # In-stock price of every variant that counts right now. Only these are visited when a day's point is
        # recomputed: a marketplace accumulates a gone variant for every listing it ever saw, and walking all
        # of them on every changed day made the export cost (variants ever seen) x (scrape dates).
        cur: dict[int, int] = {}
        out: list[tuple[str, int, int, int]] = []
        ev, nev = 0, len(events)
        active: tuple[bool, ...] | None = None
        point: tuple[int, int, int] | None = None
        dirty = True
        for d in self._axis(stores):
            while ev < nev and events[ev][0] <= d:
                _, i, price = events[ev]
                ev += 1
                if price is None:
                    dirty = cur.pop(i, None) is not None or dirty
                elif cur.get(i) != price:
                    cur[i] = price
                    dirty = True
            if multi:  # the set of stores with an ok run today can change without any observation
                today_active = tuple(d in o for o in ok)
                if today_active != active:
                    active = today_active
                    dirty = True
            if dirty:
                prices: list[int] = []
                seen: set[int] = set()
                for i, price in cur.items():
                    s = vstore[i]
                    if active is None or active[s]:
                        prices.append(price)
                        seen.add(s)
                if prices:
                    prices.sort()
                    point = (*_min_median(prices), len(seen))
                else:
                    point = None
                dirty = False
            if point is not None:
                out.append((d, *point))
        return out


# --------------------------------------------------------------------------- assembly

def _live_variants(disc: _Disc, kind: str | None = None,
                   kinds: dict[str, str] | None = None) -> dict[str, list[_Variant]]:
    """Priced, non-gone variants of non-gone listings, split by condition (and, with `kind`, by
    the kind of store they are sold at)."""
    live: dict[str, list[_Variant]] = {c: [] for c in CONDITIONS}
    for v in disc.variants:
        if not v.gone and not v.listing.gone and _priced(v.price_cents) and (
                kind is None or (kinds or {}).get(v.listing.store_id, "retail") == kind):
            live[v.listing.condition].append(v)
    return live


def _block(live: list[_Variant], series: list[tuple[str, int, int, int]],
           today: date) -> dict | None:
    if not live:
        return None
    in_stock = [v for v in live if v.available]
    lo, med = _min_median(sorted(v.price_cents for v in (in_stock or live)))
    dates = [p[0] for p in series]
    mins = [p[1] for p in series]
    block = {
        "min": _dollars(lo),
        "median": _dollars(med),
        "stores_in_stock": len({v.listing.store_id for v in in_stock}),
        "stores_listing": len({v.listing.store_id for v in live}),
    }
    for days in CHANGE_DAYS:
        block[f"change_{days}d"] = _change(dates, mins, today, days, lo if in_stock else None)
    return block


def _assign_slugs(keys: Sequence[str]) -> dict[str, str]:
    """key_slug per key; a (theoretical) collision gets -2, -3... and never steals
    another key's natural slug. `keys` must be sorted for deterministic results."""
    natural = {k: key_slug(k)[:MAX_SLUG].rstrip("-") or "disc" for k in keys}
    reserved = set(natural.values())
    used: set[str] = set()
    slugs: dict[str, str] = {}
    for k in keys:
        base = slug = natural[k]
        n = 1
        while slug in used or (slug != base and slug in reserved):
            n += 1
            slug = f"{base}-{n}"
        used.add(slug)
        slugs[k] = slug
    return slugs


def _listing_url(lst: _Listing, kinds: dict[str, str], base_urls: dict[str, str]) -> str | None:
    """The listing's own http(s) URL; a retail listing without a usable one falls back to the
    store's /products/<handle> page, a marketplace listing has no such page (None)."""
    url = _http_url(lst.url)
    if url or not lst.handle or kinds.get(lst.store_id, "retail") == "marketplace":
        return url
    return _http_url(f"{base_urls.get(lst.store_id, '')}/products/{lst.handle}")


def _history_listings(disc: _Disc, store_names: dict[str, str], base_urls: dict[str, str],
                      kinds: dict[str, str]) -> list[dict]:
    rows = []
    for v in disc.variants:
        if v.gone or v.listing.gone or not _priced(v.price_cents):
            continue
        lst = v.listing
        url = _listing_url(lst, kinds, base_urls)
        sort_key = (v.price_cents, not v.available,
                    lst.store_id, lst.title, v.weight_g or 0, url or "", v.variant_id)
        rows.append((sort_key, {
            "store": store_names.get(lst.store_id, lst.store_id),
            "store_id": lst.store_id,
            "store_kind": kinds.get(lst.store_id, "retail"),
            "title": lst.title,
            "url": url,
            "condition": lst.condition,
            "weight_g": v.weight_g,
            "price": _dollars(v.price_cents),
            "compare_at": _dollars(v.compare_at_cents),
            "available": v.available,
            "last_seen": v.last_seen,
        }))
    rows.sort(key=itemgetter(0))
    return [r[1] for r in rows]


def _sales_30d(sales: Sequence[_Sale], today: date) -> dict:
    """{"confirmed": {"count", "median"} | None, "inferred": {...} | None} for sales dated
    from `today - 30 days` to `today` inclusive (a median in dollars, a half cent rounds up)."""
    lo, hi = (today - timedelta(days=SALES_DAYS)).isoformat(), today.isoformat()
    prices: dict[bool, list[int]] = {True: [], False: []}
    for s in sales:
        if lo <= s.sold_on <= hi:
            prices[s.confirmed].append(s.price_cents)

    def summary(cents: list[int]) -> dict | None:
        if not cents:
            return None
        cents.sort()
        return {"count": len(cents), "median": _dollars(_min_median(cents)[1])}

    return {"confirmed": summary(prices[True]), "inferred": summary(prices[False])}


def _history_sales(sales: Sequence[_Sale], store_names: dict[str, str], kinds: dict[str, str],
                   base_urls: dict[str, str]) -> list[dict]:
    """Newest first (a confirmed sale before an inferred one on the same day), at most MAX_SALES.
    An unconfirmed sale always carries confidence `low`: the page words it as inferred."""
    ordered = sorted(sales, reverse=True, key=lambda s: (
        s.sold_on, s.confirmed, s.price_cents, s.listing.store_id, s.listing.id, s.source))
    return [{
        "date": s.sold_on,
        "price": _dollars(s.price_cents),
        "condition": s.listing.condition,
        "source": s.source,
        "confidence": "confirmed" if s.confirmed else "low",
        "store": store_names.get(s.listing.store_id, s.listing.store_id),
        "url": _listing_url(s.listing, kinds, base_urls),
    } for s in ordered[:MAX_SALES]]


def _points(series: Sequence[tuple[str, int, int, int]]) -> list[dict]:
    return [{"date": d, "min": lo / 100, "median": med / 100, "stores_in_stock": n}
            for d, lo, med, n in series]


def _dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True)


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _prune_history(hist_dir: Path, keep: set[str]) -> None:
    """Delete history/*.json that no longer belongs to an exported disc, plus leftover temp files."""
    for p in hist_dir.iterdir():
        if not (p.is_file() or p.is_symlink()):
            continue
        if (p.name.endswith(".json") and p.name not in keep) or p.name.endswith(".json.tmp"):
            p.unlink()


def export_site(conn: sqlite3.Connection, out_dir: Path, today: str) -> dict:
    """Write out_dir/index.json and out_dir/history/<slug>.json; return the stats dict.

    `today` (YYYY-MM-DD) is the `generated_at` stamp and the anchor for the
    7/30-day changes. Output is deterministic: same DB + same `today` gives
    byte-identical files. Every file is written via temp file + os.replace; the
    index comes last and stale history files are removed after it.
    """
    out_dir = Path(out_dir)
    today_d = date.fromisoformat(today)

    stores = _load_stores(conn)
    store_names = {s[0]: s[1] for s in stores}
    base_urls = {s[0]: s[2] for s in stores}
    kinds = {s[0]: s[4] for s in stores}
    ok_dates = _load_ok_dates(conn)
    discs = _load_discs(conn)
    builder = _SeriesBuilder(ok_dates, _load_observations(conn), kinds)
    sale_rows = _load_sales(conn)
    disc_sales = _disc_sales(sale_rows, {l.id: l for d in discs.values() for l in d.listings})

    # Phase A: decide which discs are exported (live listing or any history).
    export_keys: list[str] = []
    cached: dict[str, dict[str, list]] = {}  # series already built while deciding
    for key in sorted(discs):
        disc = discs[key]
        live = _live_variants(disc)
        if live["new"] or live["used"]:
            export_keys.append(key)
            continue
        series = {c: builder.build(disc, c) for c in CONDITIONS}
        if series["new"] or series["used"]:
            export_keys.append(key)
            cached[key] = series
    slugs = _assign_slugs(export_keys)

    hist_dir = out_dir / "history"
    hist_dir.mkdir(parents=True, exist_ok=True)

    # Phase B: build and write each disc, keeping only the index entry in memory.
    entries: list[dict] = []
    for key in export_keys:
        disc = discs[key]
        series = cached.pop(key, None) or {c: builder.build(disc, c) for c in CONDITIONS}
        live = _live_variants(disc)
        ident = {
            "manufacturer": _display(l.manufacturer for l in disc.listings),
            "mold": _display(l.mold for l in disc.listings),
            "plastic": _display(l.plastic for l in disc.listings),
            "edition": _display(l.edition for l in disc.listings),
            "player": _display(l.player for l in disc.listings),
            "disc_type": _display(l.disc_type for l in disc.listings if l.disc_type),
        }
        slug = slugs[key]
        sales = disc_sales.get(key, [])
        history = {
            "key": key, "slug": slug, **ident,
            "series": {c: _points(series[c]) for c in CONDITIONS},
            "listings": _history_listings(disc, store_names, base_urls, kinds),
            "sales": _history_sales(sales, store_names, kinds, base_urls),
        }
        entry = {
            "key": key, "slug": slug, **ident,
            "new": _block(live["new"], series["new"], today_d),
            "used": _block(live["used"], series["used"], today_d),
            "sales_30d": _sales_30d(sales, today_d),
            "last_seen": max(l.last_seen for l in disc.listings),
        }
        if builder.has_kind(disc, "marketplace"):
            by_kind = {k: {c: builder.build(disc, c, k) for c in CONDITIONS} for k in KINDS}
            live_mp = _live_variants(disc, "marketplace", kinds)
            if any(by_kind["marketplace"].values()) or any(live_mp.values()):
                live_retail = _live_variants(disc, "retail", kinds)
                history["series_by_kind"] = {k: {c: _points(by_kind[k][c]) for c in CONDITIONS}
                                             for k in KINDS}
                entry["retail"] = {c: _block(live_retail[c], by_kind["retail"][c], today_d)
                                   for c in CONDITIONS}
        _write_atomic(hist_dir / f"{slug}.json", _dumps(history))
        entries.append(entry)

    entries.sort(key=lambda e: tuple(x.casefold() for x in (
        e["manufacturer"], e["mold"], e["plastic"], e["edition"], e["player"])) + (
        e["manufacturer"], e["mold"], e["plastic"], e["edition"], e["player"], e["key"]))

    counts = {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM listings GROUP BY status")}
    stats = {
        "listings": sum(counts.values()),
        "matched": counts.get("matched", 0),
        "review": counts.get("review", 0),
        "ignored": counts.get("ignored", 0),
        "unparsed": counts.get("unparsed", 0),
        "discs": len(entries),
        "stores": len(stores),
    }
    stats["sales_confirmed"], stats["sales_inferred"] = _sale_totals(sale_rows)
    currencies = Counter(s[3] for s in stores)
    currency = min(currencies, key=lambda c: (-currencies[c], c)) if currencies else "USD"
    index = {
        "generated_at": today,
        "currency": currency,
        "stores": [{"id": s[0], "name": s[1], "base_url": s[2], "kind": s[4],
                    "last_ok": ok_dates[s[0]][-1] if s[0] in ok_dates else None} for s in stores],
        "stats": stats,
        "discs": entries,
    }
    _write_atomic(out_dir / "index.json", _dumps(index))
    # Only now that no published file can reference them any more.
    _prune_history(hist_dir, {f"{s}.json" for s in slugs.values()})
    return stats
