"""Hand-picked cards that always get tracked.

These bypass the seed list's rarity and price filters: if you name a card
here, it is scanned. Because a name and number alone can match the wrong
printing, an entry must first be *resolved* to an exact provider record.
"""
from __future__ import annotations

import json
from pathlib import Path

from .config import SEEDS
from .providers.ppt import _norm, _norm_number, results_of

PATH = SEEDS / "watchlist.json"


def load(path: Path | None = None) -> dict:
    path = path or PATH
    if not path.exists():
        return {"cards": []}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save(blob: dict, path: Path | None = None) -> None:
    path = path or PATH
    path.write_text(json.dumps(blob, indent=2) + "\n")


def is_resolved(entry: dict) -> bool:
    return bool(entry.get("set_name")) and bool(entry.get("external_id") or entry.get("ppt_id"))


def candidates_for(entry: dict, records: list[dict]) -> list[dict]:
    """Records whose card number matches the entry's."""
    want = _norm_number(entry.get("number"))
    return [r for r in records
            if want and _norm_number(r.get("cardNumber") or r.get("number")) == want]


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
