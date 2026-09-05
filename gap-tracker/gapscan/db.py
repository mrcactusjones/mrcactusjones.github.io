"""SQLite store for price time series.

Daily JSON snapshots were right when history accrued one day at a time. With
the provider returning months of history per card, the questions worth asking
are relational -- "how many days has this gap held", "is the PSA 9 rising while
raw is flat" -- so the data belongs in a database.

The JSON outputs the dashboard reads are still generated; this sits underneath.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

from .config import DATA
from .store import iso, utcnow

PATH = DATA / "gaps.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id            TEXT PRIMARY KEY,
    name          TEXT,
    set_name      TEXT,
    number        TEXT,
    rarity        TEXT,
    tcgplayer_id  TEXT,
    source        TEXT,
    tier          TEXT,
    priority      INTEGER,
    first_seen    TEXT,
    last_scanned  TEXT
);

-- One price for one card, on one day, at one grade. The primary key makes
-- re-ingesting overlapping history windows idempotent.
CREATE TABLE IF NOT EXISTS price_points (
    card_id TEXT NOT NULL,
    date    TEXT NOT NULL,
    grade   TEXT NOT NULL,
    price   REAL NOT NULL,
    sales   INTEGER,
    origin  TEXT,
    PRIMARY KEY (card_id, date, grade)
);
CREATE INDEX IF NOT EXISTS idx_points_card_grade
    ON price_points (card_id, grade, date);
CREATE INDEX IF NOT EXISTS idx_points_date ON price_points (date);

-- What the model concluded on a given day, so verdict changes are auditable.
CREATE TABLE IF NOT EXISTS runs (
    started_at TEXT PRIMARY KEY,
    finished_at TEXT,
    cards      INTEGER,
    credits    INTEGER,
    notes      TEXT
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def session(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_card(conn: sqlite3.Connection, card: dict, scanned_at: str | None = None) -> None:
    conn.execute(
        """INSERT INTO cards (id, name, set_name, number, rarity, tcgplayer_id,
                              source, tier, priority, first_seen, last_scanned)
           VALUES (:id, :name, :set_name, :number, :rarity, :tcgplayer_id,
                   :source, :tier, :priority, COALESCE(:scanned, date('now')), :scanned)
           ON CONFLICT(id) DO UPDATE SET
               name=excluded.name, set_name=excluded.set_name,
               number=excluded.number, rarity=excluded.rarity,
               tcgplayer_id=COALESCE(excluded.tcgplayer_id, cards.tcgplayer_id),
               source=excluded.source, tier=excluded.tier,
               priority=excluded.priority,
               last_scanned=COALESCE(excluded.last_scanned, cards.last_scanned)""",
        {"id": card["id"], "name": card.get("name"), "set_name": card.get("set_name"),
         "number": card.get("number"), "rarity": card.get("rarity"),
         "tcgplayer_id": card.get("tcgplayer_id"), "source": card.get("source"),
         "tier": card.get("tier"), "priority": card.get("priority"),
         "scanned": scanned_at})


def insert_points(conn: sqlite3.Connection,
                  rows: Iterable[tuple[str, str, str, float, int | None, str]]) -> int:
    """rows of (card_id, date, grade, price, sales, origin). Last write wins."""
    cur = conn.executemany(
        """INSERT INTO price_points (card_id, date, grade, price, sales, origin)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(card_id, date, grade) DO UPDATE SET
               price=excluded.price,
               sales=COALESCE(excluded.sales, price_points.sales),
               origin=excluded.origin""",
        list(rows))
    return cur.rowcount


def series(conn: sqlite3.Connection, card_id: str, grade: str) -> list[tuple[str, float]]:
    return [(r["date"], r["price"]) for r in conn.execute(
        "SELECT date, price FROM price_points WHERE card_id=? AND grade=? ORDER BY date",
        (card_id, grade))]


def series_detail(conn: sqlite3.Connection, card_id: str,
                  grade: str) -> list[sqlite3.Row]:
    """Like `series`, but keeps where each point came from.

    A price the provider reported as history and one we snapshotted today are
    stored the same way and read back the same way, which makes a series that
    shifts between runs impossible to explain. This keeps them apart.
    """
    return list(conn.execute(
        """SELECT date, price, sales, origin FROM price_points
           WHERE card_id=? AND grade=? ORDER BY date""", (card_id, grade)))


def sales_series(conn: sqlite3.Connection, card_id: str,
                 grade: str) -> tuple[list[tuple[str, float]], str]:
    """A grade's real sales, or its snapshots when there are none.

    `history` rows are individual eBay sale prices; `snapshot` rows are the
    provider's blended figure, written once per run. They are different kinds
    of measurement and mixing them corrupts every analysis downstream: each
    daily run appends another copy of the same blended number, so the tail of
    a series becomes a flat run that swamps a trend and -- worse -- packs the
    middle of the distribution until the two-printings check stops firing.

    The fallback is not optional. For cards the provider returns no graded
    history for, our own snapshots are the only series there is, and over time
    they become a real one we built ourselves.

    Returns (points, origin) so a caller can say which it got.
    """
    rows = list(conn.execute(
        """SELECT date, price, origin FROM price_points
           WHERE card_id=? AND grade=? ORDER BY date""", (card_id, grade)))
    sales = [(r["date"], r["price"]) for r in rows if r["origin"] == "history"]
    if sales:
        return sales, "history"
    return [(r["date"], r["price"]) for r in rows], "snapshot"


def grades_for(conn: sqlite3.Connection, card_id: str) -> list[str]:
    return [r["grade"] for r in conn.execute(
        "SELECT DISTINCT grade FROM price_points WHERE card_id=? ORDER BY grade",
        (card_id,))]


def record_run(conn: sqlite3.Connection, started_at: str, credits: int,
               cards: int = 0, notes: str = "") -> None:
    """Log what a credit-spending command cost.

    The daily allowance is an account-wide budget, but each run only knew its
    own spend -- so three sweeps in a day each started believing the whole
    20,000 was free, and the third ran the account dry mid-task.
    """
    conn.execute(
        """INSERT INTO runs (started_at, finished_at, cards, credits, notes)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(started_at) DO UPDATE SET
               finished_at=excluded.finished_at, cards=excluded.cards,
               credits=excluded.credits, notes=excluded.notes""",
        (started_at, iso(utcnow()), cards, credits, notes))


def credits_spent_since(conn: sqlite3.Connection, since: str) -> int:
    """Credits logged at or after an ISO timestamp."""
    row = conn.execute(
        "SELECT COALESCE(SUM(credits), 0) FROM runs WHERE started_at >= ?",
        (since,)).fetchone()
    return int(row[0] or 0)


def allowance_resets_at(now: datetime | None = None) -> datetime:
    """Next 00:00 UTC. The provider resets the daily allowance there.

    Deliberately UTC and not the local date the snapshots use: this is the
    provider's boundary, not ours, and west of Greenwich an evening run falls
    in tomorrow's allowance while still being today locally.
    """
    now = now or utcnow()
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                             microsecond=0)


def allowance_day_start(now: datetime | None = None) -> datetime:
    """The 00:00 UTC that began the current allowance day."""
    now = now or utcnow()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def stats(conn: sqlite3.Connection) -> dict:
    def one(sql: str):
        return conn.execute(sql).fetchone()[0]
    return {
        "cards": one("SELECT COUNT(*) FROM cards"),
        "price_points": one("SELECT COUNT(*) FROM price_points"),
        "earliest": one("SELECT MIN(date) FROM price_points"),
        "latest": one("SELECT MAX(date) FROM price_points"),
    }
