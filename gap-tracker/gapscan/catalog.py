"""Build the candidate universe from pokemontcg.io (free) + the curated seeds.

This step costs nothing: pokemontcg.io serves card metadata and TCGplayer raw
prices for free, so the whole universe can be rebuilt as often as you like.
Paid credits are only ever spent on graded prices, in scan.py.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import FIXTURES, Thresholds





@dataclass(frozen=True)
class SweepTarget:
    """One `backfill` query: a set, addressed the most exact way we can."""

    label: str
    priority: int = 0
    set_id: str | None = None
    set_name: str | None = None
    aliases: frozenset = field(default_factory=frozenset)


def sweep_targets(universe: dict, seeds: list[dict] | None = None,
                  wanted: list[str] | None = None,
                  aliases: dict[str, str] | None = None) -> list[SweepTarget]:
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

    # A name a sweep has already resolved to a set id is covered whether or
    # not any of its own cards were pinned. One card outside the price band is
    # never returned, never pinned, and kept its whole set on the crawl.
    for name, set_id in (aliases or {}).items():
        if set_id in by_id and name in by_name:
            by_name[name][1] = True
            by_id[set_id][2].add(name)

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


def build(seeds: dict, thresholds: Thresholds, fixture: Path,
          only: set[str] | None = None) -> tuple[dict, dict]:
    """Return (universe, meta) from the offline fixture. Demo only.

    This used to fetch pokemontcg.io. Its card ids disagree with PPT's, which
    made every catalogued card a duplicate of its swept twin and kept seven
    set names on the crawl at 1,125 credits a run; it also 500'd on one to
    three sets every time. Live cards come from sweeps now. The fixture path
    survives because `demo` needs a universe with no network and no key.
    """
    rarities = {r.lower() for r in seeds.get("rarities", [])}
    named = seeds.get("cards", [])
    universe: dict[str, dict] = {}
    empty_sets: list[str] = []
    skipped = {"rarity": 0, "no_price": 0, "price_band": 0}

    wanted = [e for e in seeds.get("sets", []) if not only or e["id"] in only]
    for entry in wanted:
        set_id = entry["id"]
        cards = [c for c in json.loads(fixture.read_text())
                 if (c.get("set") or {}).get("id") == set_id]
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
        "skipped": skipped,
        "universe_size": len(universe),
        "source": "fixture",
    }
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
