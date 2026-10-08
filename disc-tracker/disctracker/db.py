"""SQLite storage. Observations are stored change-only so the file stays small
enough to commit to git after every daily scrape."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable

from .models import ParsedListing, RawProduct, disc_key

SCHEMA = """
CREATE TABLE IF NOT EXISTS stores (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, base_url TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD'
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id TEXT NOT NULL REFERENCES stores(id),
    observed_on TEXT NOT NULL,          -- YYYY-MM-DD (UTC)
    started_at TEXT NOT NULL, finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',  -- running | ok | error
    n_products INTEGER NOT NULL DEFAULT 0, error TEXT
);
CREATE INDEX IF NOT EXISTS runs_store_date ON runs(store_id, observed_on);
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id TEXT NOT NULL REFERENCES stores(id),
    product_id INTEGER NOT NULL,
    handle TEXT, title TEXT NOT NULL, vendor TEXT, product_type TEXT,
    tags TEXT NOT NULL DEFAULT '[]', url TEXT,
    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
    gone INTEGER NOT NULL DEFAULT 0,
    parse_version INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'unparsed',
    manufacturer TEXT NOT NULL DEFAULT '', mold TEXT NOT NULL DEFAULT '',
    plastic TEXT NOT NULL DEFAULT '', edition TEXT NOT NULL DEFAULT '',
    player TEXT NOT NULL DEFAULT '', year INTEGER,
    condition TEXT NOT NULL DEFAULT 'new', grade REAL,
    disc_type TEXT NOT NULL DEFAULT '', flags TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL DEFAULT 0, disc_key TEXT,
    UNIQUE(store_id, product_id)
);
CREATE INDEX IF NOT EXISTS listings_key ON listings(disc_key);
CREATE TABLE IF NOT EXISTS variants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id INTEGER NOT NULL REFERENCES listings(id),
    variant_id INTEGER NOT NULL,
    title TEXT, sku TEXT, weight_g INTEGER,
    last_seen TEXT NOT NULL, gone INTEGER NOT NULL DEFAULT 0,
    price_cents INTEGER, compare_at_cents INTEGER, available INTEGER NOT NULL DEFAULT 0,
    UNIQUE(listing_id, variant_id)
);
-- Change-only: a row exists only when price/compare_at/available/gone differs
-- from the previous row for that variant (or it is the first sighting).
CREATE TABLE IF NOT EXISTS observations (
    variant_pk INTEGER NOT NULL REFERENCES variants(id),
    observed_on TEXT NOT NULL,
    price_cents INTEGER, compare_at_cents INTEGER,
    available INTEGER NOT NULL, gone INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (variant_pk, observed_on)
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def upsert_store(conn: sqlite3.Connection, store: dict) -> None:
    conn.execute(
        "INSERT INTO stores(id, name, base_url, currency) VALUES (?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, base_url=excluded.base_url, "
        "currency=excluded.currency",
        (store["id"], store["name"], store["base_url"].rstrip("/"), store.get("currency", "USD")),
    )
    conn.commit()


def start_run(conn: sqlite3.Connection, store_id: str, observed_on: str, started_at: str) -> int:
    cur = conn.execute(
        "INSERT INTO runs(store_id, observed_on, started_at) VALUES (?,?,?)",
        (store_id, observed_on, started_at),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, finished_at: str, status: str,
               n_products: int = 0, error: str | None = None) -> None:
    conn.execute(
        "UPDATE runs SET finished_at=?, status=?, n_products=?, error=? WHERE id=?",
        (finished_at, status, n_products, error, run_id),
    )
    conn.commit()


def record_products(conn: sqlite3.Connection, store_id: str, observed_on: str,
                    products: Iterable[RawProduct], weight_parser=None) -> dict:
    """Persist one complete scrape of a store.

    Must only be called with a COMPLETE product list: anything previously seen
    and absent now is marked gone. Returns counters.
    """
    products = list(products)
    stats = {"products": len(products), "new_listings": 0, "changed": 0, "gone": 0}
    seen_listing_ids: set[int] = set()
    seen_variant_pks: set[int] = set()

    with conn:
        for p in products:
            row = conn.execute(
                "SELECT id FROM listings WHERE store_id=? AND product_id=?",
                (store_id, p.product_id),
            ).fetchone()
            tags = json.dumps(sorted(p.tags))
            if row is None:
                cur = conn.execute(
                    "INSERT INTO listings(store_id, product_id, handle, title, vendor, "
                    "product_type, tags, url, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (store_id, p.product_id, p.handle, p.title, p.vendor, p.product_type,
                     tags, p.url, observed_on, observed_on),
                )
                listing_id = int(cur.lastrowid)
                stats["new_listings"] += 1
            else:
                listing_id = int(row["id"])
                # Title/vendor/type/tags changes invalidate the parse.
                conn.execute(
                    "UPDATE listings SET handle=?, vendor=?, product_type=?, url=?, last_seen=?, gone=0, "
                    "parse_version = CASE WHEN title=? AND tags=? THEN parse_version ELSE 0 END, "
                    "title=?, tags=? WHERE id=?",
                    (p.handle, p.vendor, p.product_type, p.url, observed_on,
                     p.title, tags, p.title, tags, listing_id),
                )
            seen_listing_ids.add(listing_id)

            for v in p.variants:
                weight = weight_parser(v.title) if weight_parser else None
                vrow = conn.execute(
                    "SELECT id, price_cents, compare_at_cents, available, gone FROM variants "
                    "WHERE listing_id=? AND variant_id=?", (listing_id, v.variant_id),
                ).fetchone()
                avail = 1 if v.available else 0
                if vrow is None:
                    cur = conn.execute(
                        "INSERT INTO variants(listing_id, variant_id, title, sku, weight_g, last_seen, "
                        "price_cents, compare_at_cents, available) VALUES (?,?,?,?,?,?,?,?,?)",
                        (listing_id, v.variant_id, v.title, v.sku, weight, observed_on,
                         v.price_cents, v.compare_at_cents, avail),
                    )
                    vpk, changed = int(cur.lastrowid), True
                else:
                    vpk = int(vrow["id"])
                    changed = (vrow["price_cents"], vrow["compare_at_cents"], vrow["available"],
                               vrow["gone"]) != (v.price_cents, v.compare_at_cents, avail, 0)
                    conn.execute(
                        "UPDATE variants SET title=?, sku=?, weight_g=?, last_seen=?, gone=0, "
                        "price_cents=?, compare_at_cents=?, available=? WHERE id=?",
                        (v.title, v.sku, weight, observed_on, v.price_cents,
                         v.compare_at_cents, avail, vpk),
                    )
                seen_variant_pks.add(vpk)
                if changed:
                    stats["changed"] += 1
                    conn.execute(
                        "INSERT OR REPLACE INTO observations(variant_pk, observed_on, price_cents, "
                        "compare_at_cents, available, gone) VALUES (?,?,?,?,?,0)",
                        (vpk, observed_on, v.price_cents, v.compare_at_cents, avail),
                    )

        # Anything from this store that we did not see is gone (delisted/sold out of catalogue).
        stale = conn.execute(
            "SELECT v.id, v.price_cents, v.compare_at_cents, l.id AS lid FROM variants v "
            "JOIN listings l ON l.id=v.listing_id WHERE l.store_id=? AND v.gone=0", (store_id,),
        ).fetchall()
        for r in stale:
            if r["id"] in seen_variant_pks:
                continue
            stats["gone"] += 1
            conn.execute("UPDATE variants SET gone=1, available=0 WHERE id=?", (r["id"],))
            conn.execute(
                "INSERT OR REPLACE INTO observations(variant_pk, observed_on, price_cents, "
                "compare_at_cents, available, gone) VALUES (?,?,?,?,0,1)",
                (r["id"], observed_on, r["price_cents"], r["compare_at_cents"]),
            )
        live = conn.execute(
            "SELECT id FROM listings WHERE store_id=? AND gone=0", (store_id,)
        ).fetchall()
        conn.executemany(
            "UPDATE listings SET gone=1 WHERE id=?",
            [(r["id"],) for r in live if r["id"] not in seen_listing_ids],
        )
    return stats


def listings_needing_parse(conn: sqlite3.Connection, parse_version: int, force: bool = False):
    q = "SELECT id, title, vendor, product_type, tags FROM listings"
    if not force:
        q += " WHERE parse_version != ?"
        return conn.execute(q, (parse_version,)).fetchall()
    return conn.execute(q).fetchall()


def save_parse(conn: sqlite3.Connection, listing_id: int, parsed: ParsedListing,
               parse_version: int) -> None:
    conn.execute(
        "UPDATE listings SET parse_version=?, status=?, manufacturer=?, mold=?, plastic=?, "
        "edition=?, player=?, year=?, condition=?, grade=?, disc_type=?, flags=?, confidence=?, "
        "disc_key=? WHERE id=?",
        (parse_version, parsed.status, parsed.manufacturer, parsed.mold, parsed.plastic,
         parsed.edition, parsed.player, parsed.year, parsed.condition, parsed.grade,
         parsed.disc_type, json.dumps(parsed.flags), parsed.confidence, disc_key(parsed),
         listing_id),
    )
