"""Turn provider records into database rows."""
from __future__ import annotations

import sqlite3
from datetime import date

from . import db
from .providers.ppt import extract_quote, parse_history

# Grades worth storing. Anything else in the response is noise for our purposes.
KEEP_GRADES = {"raw", "psa8", "psa9", "psa10", "cgc9", "cgc10", "bgs9", "bgs10"}


def card_from_record(record: dict, universe: dict | None = None) -> dict:
    """Identify a card, preferring the id our universe already uses.

    A set sweep returns cards the seed list never chose. We have paid for them
    either way, so they are kept rather than discarded.
    """
    external = record.get("externalCatalogId")
    card_id = external or f"ppt-{record.get('id')}"
    known = (universe or {}).get(card_id, {})
    return {
        "id": card_id,
        "name": known.get("name") or record.get("name"),
        "set_name": known.get("set_name") or record.get("setName"),
        "number": known.get("number") or record.get("cardNumber"),
        "rarity": known.get("rarity") or record.get("rarity"),
        "tcgplayer_id": str(record["tcgPlayerId"]) if record.get("tcgPlayerId") else None,
        "source": known.get("source") or ("api" if external else "ppt-only"),
        "tier": known.get("tier", "candidate"),
        "priority": known.get("priority", 0),
    }


def ingest_record(conn: sqlite3.Connection, record: dict, universe: dict | None = None,
                  today: str | None = None) -> tuple[str, int]:
    """Store one card's history and today's prices. Returns (card_id, points)."""
    today = today or date.today().isoformat()
    card = card_from_record(record, universe)
    db.upsert_card(conn, card, scanned_at=today)

    rows: list[tuple] = []
    for grade, points in parse_history(record).items():
        if grade in KEEP_GRADES:
            rows.extend((card["id"], stamp, grade, price, None, "history")
                        for stamp, price in points)

    # Today's observation, which the history window may not include yet.
    quote = extract_quote(record)
    for grade, price, sales in (("raw", quote.raw, None),
                                ("psa8", quote.psa8, None),
                                ("psa9", quote.psa9, quote.sales_9),
                                ("psa10", quote.psa10, quote.sales_10),
                                ("cgc9", quote.cgc9, quote.cgc9_sales),
                                ("cgc10", quote.cgc10, quote.cgc10_sales)):
        if price:
            rows.append((card["id"], today, grade, float(price), sales, "snapshot"))

    if rows:
        db.insert_points(conn, rows)
    return card["id"], len(rows)
