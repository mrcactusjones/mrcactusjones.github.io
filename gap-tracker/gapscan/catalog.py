"""Build the candidate universe from pokemontcg.io (free) + the curated seeds.

This step costs nothing: pokemontcg.io serves card metadata and TCGplayer raw
prices for free, so the whole universe can be rebuilt as often as you like.
Paid credits are only ever spent on graded prices, in scan.py.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .config import FIXTURES, Thresholds

API = "https://api.pokemontcg.io/v2"
PAGE_SIZE = 250
USER_AGENT = "gap-tracker/0.1 (personal research tool)"


# Unauthenticated pokemontcg.io throttles aggressively, and it surfaces as
# intermittent 500s rather than clean 429s. Pace the requests; a free API key
# from dev.pokemontcg.io raises the ceiling and lets us go faster.
_UNAUTHED_INTERVAL = 1.2
_AUTHED_INTERVAL = 0.25
_last_call = 0.0


@dataclass(frozen=True)
class SweepTarget:
    """One `backfill` query: a set, addressed the most exact way we can."""

    label: str
    priority: int = 0
    set_id: str | None = None
    set_name: str | None = None
    aliases: frozenset = field(default_factory=frozenset)


def sweep_targets(universe: dict, seeds: list[dict] | None = None,
                  wanted: list[str] | None = None) -> list[SweepTarget]:
    """Which sets `backfill` should sweep, each addressed exactly once.

    The API's `set` filter matches names loosely and PPT's names are not the
    seed list's, so "Delta Species" and "EX Delta Species" were two queries
    returning one set -- each billed in full. A set whose cards carry a pinned
    `ppt_set_id` is therefore addressed by that id, and the names that resolve
    to it stop being targets in their own right. Only a set nothing has ever
    fetched falls back to a name.

    Seeded sets are included even when no card in the universe claims them, so
    a failed catalog build cannot silently drop one from the crawl -- which is
    how `Expedition` went missing from a full sweep.
    """
    by_id: dict[str, list] = {}          # set_id -> [priority, label, aliases]
    by_name: dict[str, list] = {}        # set_name -> [priority, covered by an id]

    for entry in universe.values():
        name = entry.get("set_name")
        set_id = entry.get("ppt_set_id")
        priority = entry.get("priority") or 0
        if set_id:
            label = entry.get("ppt_set_name") or name or set_id
            slot = by_id.setdefault(set_id, [priority, label, set()])
            if priority > slot[0]:
                slot[0], slot[1] = priority, label
            slot[2].update(n for n in (name, entry.get("ppt_set_name")) if n)
        if name:
            slot = by_name.setdefault(name, [priority, False])
            slot[0] = max(slot[0], priority)
            slot[1] = slot[1] or bool(set_id)

    for seed in seeds or []:
        name = seed.get("name")
        if not name:
            continue
        slot = by_name.setdefault(name, [seed.get("priority") or 0, False])
        slot[0] = max(slot[0], seed.get("priority") or 0)

    # A set's priority is the highest claimed under any of its names. Without
    # this a pinned set drops to the priority of whichever cards happened to
    # carry the id -- often the ones the sweep discovered, all at zero -- and
    # the seed list's ordering quietly stops applying.
    targets = []
    for set_id, (priority, label, aliases) in by_id.items():
        aliases = aliases | {label}
        priority = max([priority] + [by_name[a][0] for a in aliases if a in by_name])
        targets.append(SweepTarget(label=label, priority=priority, set_id=set_id,
                                   aliases=frozenset(aliases)))
    targets += [
        SweepTarget(label=name, priority=priority, set_name=name,
                    aliases=frozenset({name}))
        for name, (priority, covered) in by_name.items() if not covered
    ]

    if wanted:
        want = {w.strip().casefold() for w in wanted if w.strip()}
        targets = [t for t in targets
                   if want & {alias.casefold() for alias in t.aliases}]

    # Priority first, then name, so a run's order is reproducible.
    targets.sort(key=lambda t: (-t.priority, t.label))
    return targets


def _get(path: str, params: dict) -> dict:
    global _last_call
    key = os.environ.get("POKEMONTCG_API_KEY")
    interval = _AUTHED_INTERVAL if key else _UNAUTHED_INTERVAL
    wait = interval - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)

    url = f"{API}/{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if key:
        req.add_header("X-Api-Key", key)
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return json.loads(resp.read().decode())
    finally:
        _last_call = time.monotonic()


class SetFetchError(Exception):
    """One set could not be fetched. Never fatal: the build skips it."""


def fetch_set(set_id: str, retries: int = 4) -> list[dict]:
    """All cards in a set, following pagination.

    pokemontcg.io returns intermittent 500s, so server errors and timeouts are
    retried with backoff. A 4xx means the request itself is wrong -- usually a
    bad set id -- and is reported immediately rather than retried.
    """
    out: list[dict] = []
    page = 1
    while True:
        blob = None
        last_error = "unknown"
        for attempt in range(retries):
            try:
                blob = _get("cards", {"q": f"set.id:{set_id}",
                                      "pageSize": PAGE_SIZE, "page": page})
                break
            except urllib.error.HTTPError as exc:
                last_error = f"HTTP {exc.code} {exc.reason}"
                if 400 <= exc.code < 500 and exc.code != 429:
                    raise SetFetchError(last_error) from exc
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                if retry_after and str(retry_after).isdigit():
                    time.sleep(min(int(retry_after), 60))
                    continue
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = str(getattr(exc, "reason", exc))
            if attempt < retries - 1:
                time.sleep(3 * (2 ** attempt))  # 3s, 6s, 12s
        if blob is None:
            raise SetFetchError(last_error)

        cards = blob.get("data") or []
        out.extend(cards)
        if len(cards) < PAGE_SIZE:
            return out
        page += 1


def raw_price(card: dict) -> float | None:
    """Best available raw market price, preferring the printing people grade."""
    prices = ((card.get("tcgplayer") or {}).get("prices")) or {}
    for variant in ("1stEditionHolofoil", "holofoil", "unlimitedHolofoil",
                    "normal", "reverseHolofoil", "1stEdition"):
        market = (prices.get(variant) or {}).get("market")
        if market:
            return float(market)
    # Cardmarket is euro-denominated; only used when TCGplayer has nothing.
    cm = (card.get("cardmarket") or {}).get("prices") or {}
    for key in ("trendPrice", "averageSellPrice"):
        if cm.get(key):
            return float(cm[key])
    return None


def _named_bonus(card: dict, named: list[dict]) -> tuple[int, str | None]:
    set_id = (card.get("set") or {}).get("id")
    name = (card.get("name") or "").lower()
    for entry in named:
        if entry["set"] != set_id:
            continue
        if entry["name"].lower() in name:
            return entry.get("priority", 5), entry.get("why")
    return 0, None


def build(seeds: dict, thresholds: Thresholds, fixture: Path | None = None,
          verify_sets: bool = False, only: set[str] | None = None) -> tuple[dict, dict]:
    """Return (universe, meta). Universe is keyed by pokemontcg.io card id."""
    rarities = {r.lower() for r in seeds.get("rarities", [])}
    named = seeds.get("cards", [])
    universe: dict[str, dict] = {}
    empty_sets: list[str] = []
    failed_sets: dict[str, str] = {}
    skipped = {"rarity": 0, "no_price": 0, "price_band": 0}

    wanted = [e for e in seeds.get("sets", []) if not only or e["id"] in only]
    for entry in wanted:
        set_id = entry["id"]
        if fixture is not None:
            cards = [c for c in json.loads(fixture.read_text())
                     if (c.get("set") or {}).get("id") == set_id]
        else:
            try:
                cards = fetch_set(set_id)
            except SetFetchError as exc:
                # One flaky or misnamed set must not throw away the whole run.
                failed_sets[set_id] = str(exc)
                print(f"  ! {set_id}: {exc} (skipped)")
                continue
        if not cards:
            empty_sets.append(set_id)
            continue

        for card in cards:
            rarity = (card.get("rarity") or "")
            bonus, why = _named_bonus(card, named)
            # A card named in the seed list bypasses the rarity filter; the
            # community flagged it for a reason the rarity string won't show.
            if not bonus and rarity.lower() not in rarities:
                skipped["rarity"] += 1
                continue
            price = raw_price(card)
            if price is None:
                skipped["no_price"] += 1
                continue
            if not (thresholds.raw_price_min <= price <= thresholds.raw_price_max):
                skipped["price_band"] += 1
                continue

            universe[card["id"]] = {
                "id": card["id"],
                "name": card.get("name"),
                "number": card.get("number"),
                "set_id": set_id,
                "set_name": (card.get("set") or {}).get("name") or entry.get("name"),
                "rarity": rarity or None,
                "raw_hint": round(price, 2),
                "priority": entry.get("priority", 5) + bonus,
                "seed_reason": why or entry.get("why"),
                "image": (card.get("images") or {}).get("small"),
                "tier": "candidate",
                "source": "fixture" if fixture else "api",
            }

    meta = {
        "sets_requested": len(wanted),
        "empty_sets": empty_sets,
        "failed_sets": failed_sets,
        "skipped": skipped,
        "universe_size": len(universe),
        "source": "fixture" if fixture else "api.pokemontcg.io",
    }
    if verify_sets and empty_sets:
        print("WARNING: these set ids returned no cards -- check them against "
              "pokemontcg.io: " + ", ".join(empty_sets))
    if failed_sets:
        print(f"WARNING: {len(failed_sets)} set(s) failed and were skipped. "
              f"Retry just those with:  run.py catalog --sets "
              + ",".join(failed_sets))
    return universe, meta


# Sources a catalog build owns. Anything else in the universe -- cards found by
# sweeping, cards picked by hand -- was paid for or chosen elsewhere.
CATALOG_SOURCES = frozenset({"api", "fixture"})


def merge_universe(existing: dict, built: dict, source: str) -> dict:
    """Fold a freshly built catalog into the universe already on disk.

    Three rules, in order of how much damage getting them wrong does:

    * Cards from a non-catalog source always survive. A rebuild deleting the
      results of a paid sweep is silent and expensive.
    * A card from the *other* catalog source is dropped, so fixture and live
      data never mix in one ranking.
    * A card from this source survives only if its set wasn't rebuilt, which
      is what makes a partial build (--sets, or sets that failed) additive.
    """
    rebuilt = {entry.get("set_id") for entry in built.values()}
    merged = {
        card_id: entry for card_id, entry in existing.items()
        if entry.get("source", "api") not in CATALOG_SOURCES
        or (entry.get("source", "api") == source and entry.get("set_id") not in rebuilt)
    }
    merged.update(built)
    return merged
