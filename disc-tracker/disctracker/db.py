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
    currency TEXT NOT NULL DEFAULT 'USD',
    kind TEXT NOT NULL DEFAULT 'retail'          -- retail | marketplace
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
    ends_at TEXT NOT NULL DEFAULT '', last_query TEXT NOT NULL DEFAULT '',
    UNIQUE(store_id, product_id)
);
CREATE INDEX IF NOT EXISTS listings_key ON listings(disc_key);
-- Marketplace API calls spent per UTC day (the free Browse API limit is per day).
CREATE TABLE IF NOT EXISTS api_calls (day TEXT NOT NULL, source TEXT NOT NULL, calls INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, source));
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
-- Marketplace search queries and when each last ran to completion.
CREATE TABLE IF NOT EXISTS ebay_queries (
    query_key TEXT PRIMARY KEY, query TEXT NOT NULL,
    last_run TEXT, last_complete INTEGER NOT NULL DEFAULT 0,
    last_total INTEGER NOT NULL DEFAULT 0, runs INTEGER NOT NULL DEFAULT 0
);
-- Sales: confirmed (sold-data API) or inferred (a fixed-price listing vanished
-- before its end date; may also be a seller delisting, so confidence is low).
CREATE TABLE IF NOT EXISTS sales (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id INTEGER NOT NULL REFERENCES listings(id),
    sold_on TEXT NOT NULL, price_cents INTEGER NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL,        -- inferred_disappeared | marketplace_insights
    confidence TEXT NOT NULL,    -- low | confirmed
    UNIQUE(listing_id, source)
);
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
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the first release to pre-existing databases."""
    def cols(table: str) -> set[str]:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}

    for table, col, ddl in (
        ("stores", "kind", "TEXT NOT NULL DEFAULT 'retail'"),
        ("listings", "ends_at", "TEXT NOT NULL DEFAULT ''"),
        ("listings", "last_query", "TEXT NOT NULL DEFAULT ''"),
    ):
        if col not in cols(table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    # Created here, not in SCHEMA, because on an old database the column may not exist yet.
    conn.execute("CREATE INDEX IF NOT EXISTS listings_query ON listings(store_id, last_query)")
    conn.commit()


def upsert_store(conn: sqlite3.Connection, store: dict) -> None:
    conn.execute(
        "INSERT INTO stores(id, name, base_url, currency, kind) VALUES (?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, base_url=excluded.base_url, "
        "currency=excluded.currency, kind=excluded.kind",
        (store["id"], store["name"], store["base_url"].rstrip("/"), store.get("currency", "USD"),
         store.get("kind", "retail")),
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
                    products: Iterable[RawProduct], weight_parser=None,
                    complete: bool = True) -> dict:
    """Persist a scrape of a store.

    With complete=True (retail stores) the list must be the WHOLE catalogue:
    anything previously seen and absent now is marked gone. Marketplace scrapes
    pass complete=False (a search only sees a slice) and rely on expire_missing().
    Returns counters.
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
                    "product_type, tags, url, first_seen, last_seen, ends_at, last_query) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (store_id, p.product_id, p.handle, p.title, p.vendor, p.product_type,
                     tags, p.url, observed_on, observed_on, p.ends_at, p.query_key),
                )
                listing_id = int(cur.lastrowid)
                stats["new_listings"] += 1
            else:
                listing_id = int(row["id"])
                # Title/vendor/type/tags changes invalidate the parse.
                conn.execute(
                    "UPDATE listings SET handle=?, url=?, last_seen=?, gone=0, ends_at=?, "
                    "last_query=CASE WHEN ?='' THEN last_query ELSE ? END, "
                    "parse_version = CASE WHEN title=? AND tags=? AND vendor IS ? AND product_type IS ? "
                    "THEN parse_version ELSE 0 END, vendor=?, product_type=?, title=?, tags=? WHERE id=?",
                    (p.handle, p.url, observed_on, p.ends_at, p.query_key, p.query_key,
                     p.title, tags, p.vendor, p.product_type,
                     p.vendor, p.product_type, p.title, tags, listing_id),
                )
                # Back after being inferred "sold": it was a delisting/relisting, not a sale.
                conn.execute("DELETE FROM sales WHERE listing_id=? AND source='inferred_disappeared'",
                             (listing_id,))
            seen_listing_ids.add(listing_id)

            for v in p.variants:
                # Variant title first ("175g"); single-variant used discs carry the weight in the
                # product title. Shopify's `grams` is shipping weight, so it is deliberately unused.
                weight = (weight_parser(v.title) or weight_parser(p.title)) if weight_parser else None
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

        if not complete:
            return stats
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


def last_ok_count(conn: sqlite3.Connection, store_id: str) -> int | None:
    """Product count of the store's most recent successful run, or None."""
    row = conn.execute(
        "SELECT n_products FROM runs WHERE store_id=? AND status='ok' ORDER BY id DESC LIMIT 1",
        (store_id,),
    ).fetchone()
    return None if row is None else int(row["n_products"])


def known_store_ids(conn: sqlite3.Connection, kind: str = "retail") -> list[str]:
    """Ids of stores of one kind. Marketplaces are excluded by default: they are searched, not
    scraped in full, so "not in stores.json" must never retire them."""
    return [r["id"] for r in conn.execute("SELECT id FROM stores WHERE kind=? ORDER BY id", (kind,))]


def calls_today(conn: sqlite3.Connection, day: str, source: str = "ebay") -> int:
    row = conn.execute("SELECT calls FROM api_calls WHERE day=? AND source=?", (day, source)).fetchone()
    return 0 if row is None else int(row["calls"])


def add_calls(conn: sqlite3.Connection, day: str, calls: int, source: str = "ebay") -> None:
    conn.execute(
        "INSERT INTO api_calls(day, source, calls) VALUES (?,?,?) "
        "ON CONFLICT(day, source) DO UPDATE SET calls=calls+excluded.calls", (day, source, calls))
    conn.commit()


def expire_stale(conn: sqlite3.Connection, store_id: str, observed_on: str, max_age_days: int) -> int:
    """Mark marketplace listings gone that no search has returned for max_age_days.

    Backstop for listings that no completed query can retire (for example the last query that
    returned them is too large to ever complete). No sale is inferred: the reason is unknown."""
    from datetime import date, timedelta

    cutoff = (date.fromisoformat(observed_on) - timedelta(days=max_age_days)).isoformat()
    rows = conn.execute(
        "SELECT id FROM listings WHERE store_id=? AND gone=0 AND last_seen < ?", (store_id, cutoff)
    ).fetchall()
    with conn:
        for r in rows:
            for v in conn.execute(
                    "SELECT id, price_cents, compare_at_cents FROM variants WHERE listing_id=? AND gone=0",
                    (r["id"],)).fetchall():
                conn.execute("UPDATE variants SET gone=1, available=0 WHERE id=?", (v["id"],))
                conn.execute(
                    "INSERT OR REPLACE INTO observations(variant_pk, observed_on, price_cents, "
                    "compare_at_cents, available, gone) VALUES (?,?,?,?,0,1)",
                    (v["id"], observed_on, v["price_cents"], v["compare_at_cents"]))
            conn.execute("UPDATE listings SET gone=1 WHERE id=?", (r["id"],))
    return len(rows)


def order_queries(conn: sqlite3.Connection, queries: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Order (query_key, query) pairs so never-run queries go first, then the stalest.

    Ties keep the input order, so the rotation is deterministic."""
    last = {r["query_key"]: r["last_run"] or "" for r in conn.execute("SELECT query_key, last_run FROM ebay_queries")}
    return [q for _, q in sorted(((last.get(k, ""), (k, text)) for k, text in queries),
                                 key=lambda t: t[0])]


def record_query_run(conn: sqlite3.Connection, query_key: str, query: str, observed_on: str,
                     complete: bool, total: int) -> None:
    conn.execute(
        "INSERT INTO ebay_queries(query_key, query, last_run, last_complete, last_total, runs) "
        "VALUES (?,?,?,?,?,1) ON CONFLICT(query_key) DO UPDATE SET query=excluded.query, "
        "last_run=excluded.last_run, last_complete=excluded.last_complete, "
        "last_total=excluded.last_total, runs=runs+1",
        (query_key, query, observed_on, 1 if complete else 0, total),
    )
    conn.commit()


def record_sale(conn: sqlite3.Connection, listing_id: int, sold_on: str, price_cents: int,
                source: str, confidence: str, quantity: int = 1) -> None:
    """Record a sale; a listing has at most one sale per source."""
    conn.execute(
        "INSERT OR REPLACE INTO sales(listing_id, sold_on, price_cents, quantity, source, confidence) "
        "VALUES (?,?,?,?,?,?)",
        (listing_id, sold_on, price_cents, quantity, source, confidence),
    )


def expire_missing(conn: sqlite3.Connection, store_id: str, query_key: str, observed_on: str) -> dict:
    """After a query ran COMPLETELY on observed_on (and its results were recorded):
    listings last returned by that query and not seen today are gone.

    A listing that disappears before its end date is recorded as a LOW-confidence
    inferred sale (the seller may simply have delisted it); one whose end date has
    passed is just recorded as ended. Returns counters.
    """
    stats = {"gone": 0, "inferred_sales": 0}
    rows = conn.execute(
        "SELECT id, last_seen, ends_at FROM listings WHERE store_id=? AND last_query=? "
        "AND gone=0 AND last_seen < ?", (store_id, query_key, observed_on),
    ).fetchall()
    with conn:
        for r in rows:
            variants = conn.execute(
                "SELECT id, price_cents, compare_at_cents FROM variants WHERE listing_id=? AND gone=0",
                (r["id"],),
            ).fetchall()
            for v in variants:
                conn.execute("UPDATE variants SET gone=1, available=0 WHERE id=?", (v["id"],))
                conn.execute(
                    "INSERT OR REPLACE INTO observations(variant_pk, observed_on, price_cents, "
                    "compare_at_cents, available, gone) VALUES (?,?,?,?,0,1)",
                    (v["id"], observed_on, v["price_cents"], v["compare_at_cents"]),
                )
            conn.execute("UPDATE listings SET gone=1 WHERE id=?", (r["id"],))
            stats["gone"] += 1
            ended_naturally = bool(r["ends_at"]) and r["ends_at"][:10] <= observed_on
            if variants and not ended_naturally:
                record_sale(conn, r["id"], r["last_seen"], int(variants[0]["price_cents"] or 0),
                            "inferred_disappeared", "low")
                stats["inferred_sales"] += 1
    return stats
