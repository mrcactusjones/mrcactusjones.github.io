"""Hand-picked cards that always get tracked.

These bypass the seed list's rarity and price filters: if you name a card
here, it is scanned. Because a name and number alone can match the wrong
printing, an entry must first be *resolved* to an exact provider record.
"""
from __future__ import annotations

import json
from pathlib import Path

from .config import DATA, SEEDS
from .providers.ppt import _norm, _norm_number, results_of

# The tracked list of what to track, edited by hand.
PATH = SEEDS / "watchlist.json"
# Machine-written lookups, kept out of git so `git pull` never fights them.
LOCAL = DATA / "watchlist.local.json"

RESOLVED_FIELDS = ("set_name", "external_id", "ppt_id", "resolved_name",
                   "resolved_number", "rarity")


def _key(entry: dict) -> str:
    return f"{_norm(entry.get('name'))}|{_norm_number(entry.get('number'))}"


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return {}


def load(path: Path | None = None, local: Path | None = None) -> dict:
    """The hand-edited list, with locally stored resolutions layered on."""
    blob = _read(path or PATH) or {"cards": []}
    blob.setdefault("cards", [])
    resolutions = dict((_read(local or LOCAL) or {}).get("resolutions", {}))

    for entry in blob["cards"]:
        key = _key(entry)
        # Migration: adopt any resolution still sitting in the tracked file.
        if key not in resolutions and is_resolved(entry):
            resolutions[key] = {f: entry[f] for f in RESOLVED_FIELDS if f in entry}
        entry.update(resolutions.get(key, {}))

    blob["_resolutions"] = resolutions
    return blob


def save(blob: dict, local: Path | None = None) -> None:
    """Write resolutions only, and only to the untracked local file."""
    resolutions = dict(blob.get("_resolutions", {}))
    for entry in blob.get("cards", []):
        if is_resolved(entry):
            resolutions[_key(entry)] = {f: entry[f] for f in RESOLVED_FIELDS if f in entry}
    target = local or LOCAL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"resolutions": resolutions}, indent=2) + "\n")


def is_resolved(entry: dict) -> bool:
    return bool(entry.get("set_name")) and bool(entry.get("external_id") or entry.get("ppt_id"))


def candidates_for(entry: dict, records: list[dict]) -> list[dict]:
    """Records whose card number matches the entry's, and its set if one is set.

    Card numbers repeat across sets -- Meowth #106 exists in Phantasmal
    Flames, Boundaries Crossed, Legends Awakened and a Burger King promo -- so
    the number alone leaves the resolver with several candidates and no way to
    choose. A hand-set `set_name` is how you choose, and until now nothing
    read it: the tool printed "put its set name in watchlist.json" and then
    ignored the field.

    Returns nothing when a `set_name` matches none of the number-matched
    records, rather than falling back to all of them. A set name that matches
    nothing is a typo worth surfacing, not a filter worth dropping.
    """
    want = _norm_number(entry.get("number"))
    if not want:
        return []
    hits = [r for r in records
            if _norm_number(r.get("cardNumber") or r.get("number")) == want]
    wanted_set = _norm(entry.get("set_name") or "")
    if wanted_set:
        hits = [r for r in hits if _norm(r.get("setName") or "") == wanted_set]
    return hits


def find_by_number(fetch_page, entry: dict, pages: int,
                   page_size: int) -> tuple[list[dict], list[dict]]:
    """Scan pages of search results until the card number matches.

    Returns (all records seen, matching records). `fetch_page(offset)` returns
    one page.

    A single page is not enough: these entries are identified by number, and
    a name search for "Clefairy" returns whichever Clefairy the API ranks
    first out of hundreds. Billing is per card requested, so a miss on page
    one costs the same whether we stop or keep looking -- and stopping there
    is what left every 2026 card in the list unresolved.
    """
    records: list[dict] = []
    for page in range(max(1, pages)):
        got = fetch_page(page * page_size)
        records.extend(got)
        hits = candidates_for(entry, records)
        if hits:
            return records, hits
        if len(got) < page_size:
            break   # the results ran out; more pages would cost and return nothing
    return records, []


def summarise(record: dict) -> str:
    return (f"{record.get('setName')} #{record.get('cardNumber')} "
            f"{record.get('name')} [{record.get('rarity')}] "
            f"id={record.get('externalCatalogId') or record.get('id')}")


def apply_resolution(entry: dict, record: dict) -> dict:
    """Copy the identifying fields of a chosen record onto the entry."""
    entry["set_name"] = record.get("setName")
    entry["external_id"] = record.get("externalCatalogId")
    entry["ppt_id"] = record.get("id")
    entry["resolved_name"] = record.get("name")
    entry["resolved_number"] = record.get("cardNumber")
    entry["rarity"] = record.get("rarity")
    return entry


def to_universe(blob: dict) -> dict:
    """Resolved entries as universe rows, keyed like any other card."""
    out: dict[str, dict] = {}
    for entry in blob.get("cards", []):
        if not is_resolved(entry):
            continue
        card_id = entry.get("external_id") or f"manual-{_norm(entry['name'])}-{entry['number']}"
        out[card_id] = {
            "id": card_id,
            "name": entry.get("resolved_name") or entry["name"],
            "number": entry.get("resolved_number") or entry["number"],
            "set_id": "manual",
            "set_name": entry.get("set_name"),
            "rarity": entry.get("rarity") or entry.get("note"),
            "raw_hint": None,          # the provider supplies the raw price
            "priority": 100,           # always ahead of the generic seed list
            "seed_reason": f"hand-picked: {entry.get('note', '')}".strip(),
            "image": None,
            "tier": "watchlist",
            "source": "manual",
        }
    return out
