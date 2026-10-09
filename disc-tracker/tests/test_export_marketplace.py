"""Marketplace (eBay) additions of the exporter, DESIGN.md section 10.4.

Everything runs on real in-memory databases built through disctracker.db (record_products,
record_query_run, expire_missing, record_sale), exactly the calls the eBay collector makes.
"""
from __future__ import annotations

import json
import random
import time

import pytest
from test_export import (DESTROYER, TODAY, build_big, day, disc_entry, load, make_conn, parse, prod,
                         pt, scrape)

from disctracker import db, export
from disctracker.models import ParsedListing, RawProduct, RawVariant

EBAY = {"id": "ebay", "name": "eBay", "base_url": "https://www.ebay.com", "currency": "USD",
        "kind": "marketplace"}
QUERY = "innova|destroyer|star"
SLUG = "innova-destroyer-star"
FAR = "2030-01-01T00:00:00.000Z"
INFERRED = "inferred_disappeared"
INSIGHTS = "marketplace_insights"

SALE_KEYS = {"date", "price", "condition", "source", "confidence", "store", "url"}


# --------------------------------------------------------------------------- builders

def make_world(*retail: str):
    """In-memory DB with the given retail stores plus eBay (a marketplace)."""
    conn = make_conn(*retail)
    db.upsert_store(conn, EBAY)
    return conn


def item(item_id: int, cents: int, title: str = "NEW Innova Star Destroyer 175g", *,
         ends_at: str = FAR, query: str = QUERY, url: str | None = None, available: bool = True,
         used: bool = False) -> RawProduct:
    """One eBay item as the collector hands it to record_products."""
    return RawProduct(
        item_id, "", title, vendor="", product_type="Used" if used else "New",
        tags=["condition:used" if used else "condition:new"],
        url=f"https://www.ebay.com/itm/{item_id}" if url is None else url,
        variants=[RawVariant(item_id, "", price_cents=cents, available=available)],
        ends_at=ends_at, query_key=query)


def ebay_run(conn, on: str, items=(), *, status: str = "ok", query: str = QUERY,
             complete: bool = True) -> None:
    """One eBay collection run for one search query (what ebay.collect does per query)."""
    items = list(items)
    run = db.start_run(conn, "ebay", on, on + "T05:00:00+00:00")
    if status == "ok":
        db.record_products(conn, "ebay", on, items, weight_parser=None, complete=False)
        db.record_query_run(conn, query, "star destroyer innova", on, complete, len(items))
        if complete:
            db.expire_missing(conn, "ebay", query, on)
    db.finish_run(conn, run, on + "T05:05:00+00:00", status, len(items) if status == "ok" else 0,
                  None if status == "ok" else "boom")


def listing_id(conn, store_id: str, product_id: int) -> int:
    return conn.execute("SELECT id FROM listings WHERE store_id=? AND product_id=?",
                        (store_id, product_id)).fetchone()[0]


def sale(conn, store_id: str, product_id: int, sold_on: str, cents: int, *, source: str = INFERRED,
         confidence: str = "low") -> None:
    db.record_sale(conn, listing_id(conn, store_id, product_id), sold_on, cents, source, confidence)
    conn.commit()


def export_to(conn, tmp_path):
    stats = export.export_site(conn, tmp_path, TODAY)
    index, hist = load(tmp_path)
    return stats, index, hist


# --------------------------------------------------------------------------- the scenario
# Destroyer Star over days -40..0. Retail shop: $20.00 new in stock from day -12.
#   1001 new  $15.00  eBay days -10..-6, then gone            -> inferred sale, -6, $15.00
#   1002 new  $18.00  eBay days -10..0 ($17.00 from day -3)   -> live; confirmed sale -2, $17.50
#   1003 used $ 9.00  eBay days -8..-3, then gone             -> inferred sale, -3, $9.00
#   1004 new  $17.00  eBay days -40..-35, then gone           -> inferred sale, -35 (outside 30 days)
#   1005 new  $16.00  eBay days -20..-15, ends_at day -15     -> ended on schedule: no sale at all
#   1006 new  $19.00  eBay days -20..-15, then gone           -> inferred -15 + confirmed -14 $18.50
#   1007 (review)     eBay days -9..-8, then gone             -> inferred sale, but not a matched disc

def items_on(off: int):
    out = []
    if -40 <= off <= -35:
        out.append(item(1004, 1700))
    if -20 <= off <= -15:
        out.append(item(1005, 1600, ends_at=day(-15) + "T12:00:00.000Z"))
        out.append(item(1006, 1900))
    if -10 <= off <= -6:
        out.append(item(1001, 1500))
    if -10 <= off <= 0:
        out.append(item(1002, 1800 if off < -3 else 1700))
    if -8 <= off <= -3:
        out.append(item(1003, 900, "USED Innova Star Destroyer 9/10", used=True))
    if -9 <= off <= -8:
        out.append(item(1007, 1000, "Innova mystery thing"))
    return out


def build_scenario():
    conn = make_world("shop")
    for off in range(-40, 1):
        if off >= -12:
            scrape(conn, "shop", day(off), [prod(1, (2000, True), title="Innova Star Destroyer")])
        ebay_run(conn, day(off), items_on(off))
    parse(conn, "shop", 1)
    for pid in (1001, 1002, 1004, 1005, 1006):
        parse(conn, "ebay", pid)
    parse(conn, "ebay", 1003, condition="used")
    parse(conn, "ebay", 1007, status="review", mold="Rare", plastic="")
    sale(conn, "ebay", 1002, day(-2), 1750, source=INSIGHTS, confidence="confirmed")
    sale(conn, "ebay", 1006, day(-14), 1850, source=INSIGHTS, confidence="confirmed")
    return conn


@pytest.fixture(scope="module")
def scenario(tmp_path_factory):
    conn = build_scenario()
    out = tmp_path_factory.mktemp("marketplace")
    stats, index, hist = export_to(conn, out)
    return conn, out, stats, index, hist


def test_scenario_is_what_the_comment_says(scenario):
    # guard for the builders above: the collector's own bookkeeping produced the sales we describe
    conn, *_ = scenario
    rows = conn.execute("SELECT l.product_id, s.sold_on, s.price_cents, s.source, s.confidence "
                        "FROM sales s JOIN listings l ON l.id = s.listing_id ORDER BY l.product_id, s.source").fetchall()
    assert [tuple(r) for r in rows] == [
        (1001, day(-6), 1500, INFERRED, "low"),
        (1002, day(-2), 1750, INSIGHTS, "confirmed"),
        (1003, day(-3), 900, INFERRED, "low"),
        (1004, day(-35), 1700, INFERRED, "low"),
        (1006, day(-15), 1900, INFERRED, "low"),
        (1006, day(-14), 1850, INSIGHTS, "confirmed"),
        (1007, day(-8), 1000, INFERRED, "low"),
    ]


# --------------------------------------------------------------------------- kinds

def test_store_kind_in_index_and_history_listings(scenario):
    _, _, _, index, hist = scenario
    assert {s["id"]: s["kind"] for s in index["stores"]} == {"ebay": "marketplace", "shop": "retail"}
    assert all(set(s) == {"id", "name", "base_url", "kind", "last_ok"} for s in index["stores"])
    rows = hist[SLUG]["listings"]
    assert {(r["store_id"], r["store_kind"]) for r in rows} == {("shop", "retail"), ("ebay", "marketplace")}
    assert [(r["store"], r["store_kind"], r["price"]) for r in rows] == [
        ("eBay", "marketplace", 17.0), ("Shop Discs", "retail", 20.0)]  # cheapest first, eBay included
    assert rows[0]["url"] == "https://www.ebay.com/itm/1002"


def test_unknown_store_kind_counts_as_retail(tmp_path):
    conn = make_world("shop")
    conn.execute("UPDATE stores SET kind = 'bazaar' WHERE id = 'shop'")
    scrape(conn, "shop", TODAY, [prod(1, (2000, True))])
    parse(conn, "shop", 1)
    conn.commit()
    _, index, hist = export_to(conn, tmp_path)
    assert {s["id"]: s["kind"] for s in index["stores"]} == {"ebay": "marketplace", "shop": "retail"}
    assert hist[SLUG]["listings"][0]["store_kind"] == "retail"
    assert "series_by_kind" not in hist[SLUG]


def test_marketplace_listing_url_has_no_products_path_fallback(tmp_path):
    # a retail listing without a usable URL falls back to <base>/products/<handle>; an eBay item has no such page
    conn = make_world("shop")
    ebay_run(conn, TODAY, [item(1, 1500, url="javascript:alert(1)")])
    parse(conn, "ebay", 1)
    conn.execute("UPDATE listings SET handle = 'oops' WHERE store_id = 'ebay'")
    conn.commit()
    _, _, hist = export_to(conn, tmp_path)
    assert hist[SLUG]["listings"][0]["url"] is None


# --------------------------------------------------------------------------- series_by_kind

def test_series_by_kind_only_when_the_disc_has_marketplace_data(tmp_path):
    conn = make_world("shop")
    scrape(conn, "shop", TODAY, [prod(1, (2000, True)), prod(2, (1000, True), title="Innova DX Aviar")])
    ebay_run(conn, TODAY, [item(1, 1500)])
    parse(conn, "shop", 1)
    parse(conn, "shop", 2, mold="Aviar", plastic="DX")
    parse(conn, "ebay", 1)
    _, index, hist = export_to(conn, tmp_path)
    assert "series_by_kind" in hist[SLUG] and "retail" in disc_entry(index, DESTROYER)
    assert "series_by_kind" not in hist["innova-aviar-dx"]
    assert "retail" not in disc_entry(index, "innova|aviar|dx||")  # the retail-only blocks are the plain ones


def test_series_by_kind_shape_values_and_the_all_series_stays_the_aggregate(tmp_path):
    conn = make_world("shop")
    for off in (-1, 0):
        scrape(conn, "shop", day(off), [prod(1, (2000, True)), prod(2, (700, True), title="[Used] Star Destroyer")])
        ebay_run(conn, day(off), [item(1, 1500), item(3, 1600, "Innova Star Destroyer new")])
    parse(conn, "shop", 1)
    parse(conn, "shop", 2, condition="used")
    parse(conn, "ebay", 1)
    parse(conn, "ebay", 3)
    _, _, hist = export_to(conn, tmp_path)
    h = hist[SLUG]
    assert set(h["series_by_kind"]) == {"retail", "marketplace"}
    assert all(set(v) == {"new", "used"} for v in h["series_by_kind"].values())
    # all stores: 1500 1600 2000 -> min 1500, median 1600, two stores
    assert h["series"]["new"] == [pt(day(-1), 15.0, 16.0, 2), pt(day(0), 15.0, 16.0, 2)]
    assert h["series_by_kind"]["retail"]["new"] == [pt(day(-1), 20.0, 20.0, 1), pt(day(0), 20.0, 20.0, 1)]
    # a marketplace is one "store": two eBay items still count as one store in stock
    assert h["series_by_kind"]["marketplace"]["new"] == [pt(day(-1), 15.0, 15.5, 1), pt(day(0), 15.0, 15.5, 1)]
    assert h["series"]["used"] == h["series_by_kind"]["retail"]["used"] == [pt(day(-1), 7.0, 7.0, 1), pt(day(0), 7.0, 7.0, 1)]
    assert h["series_by_kind"]["marketplace"]["used"] == []


def test_scenario_series_by_kind(scenario):
    _, _, _, _, hist = scenario
    h = hist[SLUG]
    by = h["series_by_kind"]
    retail = {p["date"]: p for p in by["retail"]["new"]}
    market = {p["date"]: p for p in by["marketplace"]["new"]}
    assert sorted(retail) == [day(o) for o in range(-12, 1)]               # retail only since day -12
    assert all(p["min"] == p["median"] == 20.0 and p["stores_in_stock"] == 1 for p in retail.values())
    assert day(-30) not in market and day(-36) in market                    # 1004 listed days -40..-35
    assert market[day(-36)] == pt(day(-36), 17.0, 17.0, 1)
    assert day(-25) not in market                                           # nothing live between -34 and -21
    assert market[day(-18)] == pt(day(-18), 16.0, 17.5, 1)                  # 1600 and 1900 -> median 1750
    assert market[day(-6)] == pt(day(-6), 15.0, 16.5, 1)                    # 1500 and 1800
    assert market[day(-5)] == pt(day(-5), 18.0, 18.0, 1)                    # 1001 gone
    assert market[day(0)] == pt(day(0), 17.0, 17.0, 1)                      # 1002's price drop
    # the aggregate is untouched by the split: day -6 sees retail 2000 + ebay 1500, 1800
    allp = {p["date"]: p for p in h["series"]["new"]}
    assert allp[day(-6)] == pt(day(-6), 15.0, 18.0, 2)
    assert allp[day(-18)] == pt(day(-18), 16.0, 17.5, 1)
    # used: only eBay's 1003, days -8..-3
    assert [p["date"] for p in by["marketplace"]["used"]] == [day(o) for o in range(-8, -2)]
    assert by["retail"]["used"] == []
    assert h["series"]["used"] == by["marketplace"]["used"]


def test_outage_day_is_not_forward_filled_for_a_marketplace(tmp_path):
    conn = make_world("shop")
    for off in (-3, -2, -1, 0):
        scrape(conn, "shop", day(off), [prod(1, (2000, True))])
        ebay_run(conn, day(off), [item(1, 1500)] if off != -2 else [], status="ok" if off != -2 else "error")
    parse(conn, "shop", 1)
    parse(conn, "ebay", 1)
    _, _, hist = export_to(conn, tmp_path)
    h = hist[SLUG]
    # eBay failed on day -2: no marketplace point that day, and the aggregate falls back to the retail store alone
    assert [p["date"] for p in h["series_by_kind"]["marketplace"]["new"]] == [day(-3), day(-1), day(0)]
    assert [p["date"] for p in h["series_by_kind"]["retail"]["new"]] == [day(o) for o in (-3, -2, -1, 0)]
    assert [(p["date"], p["min"], p["stores_in_stock"]) for p in h["series"]["new"]] == [
        (day(-3), 15.0, 2), (day(-2), 20.0, 1), (day(-1), 15.0, 2), (day(0), 15.0, 2)]


def test_unseen_marketplace_listing_is_forward_filled_on_ok_days(tmp_path):
    # a search that did not run to completion neither sees nor expires the item: its last price carries on
    conn = make_world()
    ebay_run(conn, day(-2), [item(1, 1500)])
    ebay_run(conn, day(-1), [], complete=False)
    ebay_run(conn, day(0), [], complete=False)
    parse(conn, "ebay", 1)
    _, index, hist = export_to(conn, tmp_path)
    assert [p["date"] for p in hist[SLUG]["series_by_kind"]["marketplace"]["new"]] == [day(-2), day(-1), day(0)]
    assert disc_entry(index, DESTROYER)["new"]["min"] == 15.0


def test_vanished_listing_leaves_the_series_and_the_live_block(tmp_path):
    conn = make_world()
    ebay_run(conn, day(-1), [item(1, 1500)])
    ebay_run(conn, day(0), [])  # complete search without it: gone
    parse(conn, "ebay", 1)
    _, index, hist = export_to(conn, tmp_path)
    entry = disc_entry(index, DESTROYER)
    assert entry["new"] is None and entry["used"] is None  # no live listing, but the history keeps the disc
    assert [p["date"] for p in hist[SLUG]["series_by_kind"]["marketplace"]["new"]] == [day(-1)]
    assert hist[SLUG]["listings"] == []
    assert [r["date"] for r in hist[SLUG]["sales"]] == [day(-1)]  # ... and the (inferred) sale


def test_ebay_only_disc(tmp_path):
    conn = make_world("shop")
    scrape(conn, "shop", TODAY, [prod(1, (2000, True), title="Innova DX Aviar")])
    parse(conn, "shop", 1, mold="Aviar", plastic="DX")
    for off in (-1, 0):
        ebay_run(conn, day(off), [item(1, 1500)])
    parse(conn, "ebay", 1)
    _, index, hist = export_to(conn, tmp_path)
    entry = disc_entry(index, DESTROYER)
    assert entry["new"] == {"min": 15.0, "median": 15.0, "stores_in_stock": 1, "stores_listing": 1,
                            "change_7d": None, "change_30d": None}
    assert entry["retail"] == {"new": None, "used": None}
    h = hist[SLUG]
    assert h["series_by_kind"]["retail"] == {"new": [], "used": []}
    assert h["series_by_kind"]["marketplace"]["new"] == h["series"]["new"] == [
        pt(day(-1), 15.0, 15.0, 1), pt(day(0), 15.0, 15.0, 1)]
    assert [r["store_kind"] for r in h["listings"]] == ["marketplace"]
    assert h["sales"] == [] and entry["sales_30d"] == {"confirmed": None, "inferred": None}


# --------------------------------------------------------------------------- index: retail blocks

def test_retail_blocks_are_computed_from_the_retail_stores_alone(scenario):
    _, _, _, index, _ = scenario
    entry = disc_entry(index, DESTROYER)
    # blended: shop $20 + eBay $17 (1002) in stock
    assert entry["new"] == {"min": 17.0, "median": 18.5, "stores_in_stock": 2, "stores_listing": 2,
                            "change_7d": 0.1333, "change_30d": 0.0}  # min was 15.00 on day -7, 17.00 on day -35
    assert entry["retail"]["new"] == {"min": 20.0, "median": 20.0, "stores_in_stock": 1, "stores_listing": 1,
                                      "change_7d": 0.0, "change_30d": None}
    assert entry["used"] is None and entry["retail"]["used"] is None  # the used eBay copy is gone
    assert set(entry["retail"]) == {"new", "used"}


def test_retail_block_is_null_when_only_the_marketplace_has_that_condition(tmp_path):
    conn = make_world("shop")
    scrape(conn, "shop", TODAY, [prod(1, (2000, True))])
    ebay_run(conn, TODAY, [item(2, 800, "USED Innova Star Destroyer", used=True)])
    parse(conn, "shop", 1)
    parse(conn, "ebay", 2, condition="used")
    _, index, _ = export_to(conn, tmp_path)
    entry = disc_entry(index, DESTROYER)
    assert entry["used"]["min"] == 8.0 and entry["retail"]["used"] is None
    assert entry["retail"]["new"]["min"] == 20.0 and entry["new"]["min"] == 20.0


def test_index_entry_keys(scenario):
    _, _, _, index, _ = scenario
    assert set(disc_entry(index, DESTROYER)) == {
        "key", "slug", "manufacturer", "mold", "plastic", "edition", "player", "disc_type", "new", "used",
        "retail", "sales_30d", "last_seen"}


# --------------------------------------------------------------------------- sales

def test_history_sales_rows(scenario):
    _, _, _, _, hist = scenario
    rows = hist[SLUG]["sales"]
    assert all(set(r) == SALE_KEYS for r in rows)
    assert rows == [  # newest first; 1006's inferred row is superseded by its confirmed sale, 1005 never sold
        {"date": day(-2), "price": 17.5, "condition": "new", "source": INSIGHTS, "confidence": "confirmed",
         "store": "eBay", "url": "https://www.ebay.com/itm/1002"},
        {"date": day(-3), "price": 9.0, "condition": "used", "source": INFERRED, "confidence": "low",
         "store": "eBay", "url": "https://www.ebay.com/itm/1003"},
        {"date": day(-6), "price": 15.0, "condition": "new", "source": INFERRED, "confidence": "low",
         "store": "eBay", "url": "https://www.ebay.com/itm/1001"},
        {"date": day(-14), "price": 18.5, "condition": "new", "source": INSIGHTS, "confidence": "confirmed",
         "store": "eBay", "url": "https://www.ebay.com/itm/1006"},
        {"date": day(-35), "price": 17.0, "condition": "new", "source": INFERRED, "confidence": "low",
         "store": "eBay", "url": "https://www.ebay.com/itm/1004"},
    ]


def test_sales_30d_in_the_index(scenario):
    _, _, _, index, _ = scenario
    # confirmed: -2 ($17.50), -14 ($18.50); inferred: -3 ($9), -6 ($15); the sale of day -35 is outside the window
    assert disc_entry(index, DESTROYER)["sales_30d"] == {
        "confirmed": {"count": 2, "median": 18.0}, "inferred": {"count": 2, "median": 12.0}}


def test_stats_sale_totals(scenario):
    _, _, stats, index, _ = scenario
    # inferred rows: 1001, 1003, 1004, 1007 (an unmatched listing still counts here); 1006's inferred row is
    # the same event as its confirmed one and counts once
    assert (stats["sales_confirmed"], stats["sales_inferred"]) == (2, 4)
    assert index["stats"] == stats
    assert stats["listings"] == 8 and stats["review"] == 1 and stats["matched"] == 7


def test_unmatched_listings_have_no_sales_in_any_disc(scenario):
    _, out, _, index, hist = scenario
    assert [d["key"] for d in index["discs"]] == [DESTROYER]
    assert sorted(hist) == [SLUG]
    assert "Rare" not in (out / "index.json").read_text()


def test_sale_window_boundaries(tmp_path):
    conn = make_world()
    offsets = [-31, -30, -1, 0, 1]
    ebay_run(conn, day(-40), [item(i, 1000 + i) for i in range(1, 6)])
    for i, off in enumerate(offsets, start=1):
        parse(conn, "ebay", i)
        sale(conn, "ebay", i, day(off), 1000 + i)
    _, index, hist = export_to(conn, tmp_path)
    # today - 30 (item 2), today - 1 (item 3) and today (item 4) are inside; today - 31 and tomorrow are not
    assert disc_entry(index, DESTROYER)["sales_30d"] == {
        "confirmed": None, "inferred": {"count": 3, "median": 10.03}}
    # the history lists every sale, windows only apply to the index summary
    assert [r["date"] for r in hist[SLUG]["sales"]] == [day(off) for off in sorted(offsets, reverse=True)]


def test_sales_30d_median_rounds_a_half_cent_up(tmp_path):
    conn = make_world()
    ebay_run(conn, day(-5), [item(1, 1000), item(2, 1001)])
    for pid in (1, 2):
        parse(conn, "ebay", pid)
        sale(conn, "ebay", pid, day(-2), 1000 + pid - 1)
    _, index, _ = export_to(conn, tmp_path)
    assert disc_entry(index, DESTROYER)["sales_30d"]["inferred"] == {"count": 2, "median": 10.01}  # 1000.5 -> 1001


def test_a_confirmed_sale_is_never_downgraded_or_mixed_up(tmp_path):
    conn = make_world()
    ebay_run(conn, day(-5), [item(1, 1500), item(2, 1600)])
    for pid in (1, 2):
        parse(conn, "ebay", pid)
    sale(conn, "ebay", 1, day(-3), 1500, source=INSIGHTS, confidence="confirmed")
    sale(conn, "ebay", 2, day(-3), 1600)  # an inferred sale on the very same day
    _, index, hist = export_to(conn, tmp_path)
    assert [(r["confidence"], r["price"]) for r in hist[SLUG]["sales"]] == [("confirmed", 15.0), ("low", 16.0)]
    sales = disc_entry(index, DESTROYER)["sales_30d"]
    assert sales["confirmed"] == {"count": 1, "median": 15.0} and sales["inferred"] == {"count": 1, "median": 16.0}


@pytest.mark.parametrize("source,confidence", [
    (INFERRED, "confirmed"),    # contradictory row: a vanished listing is never a confirmed sale
    (INFERRED, "high"),
    (INSIGHTS, "low"),          # a sold-data source that is not confirmed stays unconfirmed
    (INSIGHTS, ""),
    ("manual", "maybe"),
])
def test_only_confirmed_sold_data_is_exported_as_confirmed(tmp_path, source, confidence):
    conn = make_world()
    ebay_run(conn, day(-2), [item(1, 1500)])
    parse(conn, "ebay", 1)
    sale(conn, "ebay", 1, day(-1), 1500, source=source, confidence=confidence)
    stats, index, hist = export_to(conn, tmp_path)
    (row,) = hist[SLUG]["sales"]
    assert (row["confidence"], row["source"]) == ("low", source)
    assert disc_entry(index, DESTROYER)["sales_30d"] == {
        "confirmed": None, "inferred": {"count": 1, "median": 15.0}}
    assert (stats["sales_confirmed"], stats["sales_inferred"]) == (0, 1)


def test_inferred_rows_come_out_with_confidence_low_from_the_collector(tmp_path):
    # the full collector path (no hand-written sale): vanished before ends_at -> inferred, low
    conn = make_world()
    ebay_run(conn, day(-2), [item(1, 1500)])
    ebay_run(conn, day(-1), [])
    parse(conn, "ebay", 1)
    _, _, hist = export_to(conn, tmp_path)
    (row,) = hist[SLUG]["sales"]
    assert (row["source"], row["confidence"], row["date"], row["price"]) == (INFERRED, "low", day(-2), 15.0)


def test_sales_of_gone_listings_are_exported(scenario):
    conn, _, _, _, hist = scenario
    gone = {r[0] for r in conn.execute("SELECT product_id FROM listings WHERE store_id = 'ebay' AND gone = 1")}
    assert {1001, 1003, 1004, 1006} <= gone
    urls = {r["url"] for r in hist[SLUG]["sales"]}
    assert {f"https://www.ebay.com/itm/{i}" for i in (1001, 1003, 1004, 1006)} <= urls


def test_a_relisted_item_cancels_its_inferred_sale(tmp_path):
    conn = make_world()
    ebay_run(conn, day(-3), [item(1, 1500)])
    ebay_run(conn, day(-2), [])
    ebay_run(conn, day(-1), [item(1, 1500)])  # the seller relisted it: it was never sold
    parse(conn, "ebay", 1)
    stats, index, hist = export_to(conn, tmp_path)
    assert hist[SLUG]["sales"] == [] and stats["sales_inferred"] == 0
    assert disc_entry(index, DESTROYER)["sales_30d"] == {"confirmed": None, "inferred": None}


def test_sales_follow_the_current_parse(tmp_path):
    conn = make_world()
    ebay_run(conn, day(-2), [item(1, 1500)])
    ebay_run(conn, day(-1), [])
    parse(conn, "ebay", 1)
    _, index, hist = export_to(conn, tmp_path)
    assert len(hist[SLUG]["sales"]) == 1

    parse(conn, "ebay", 1, mold="Aviar", plastic="DX")  # re-parsed as another disc: the sale moves with it
    _, index, hist = export_to(conn, tmp_path)
    assert sorted(hist) == ["innova-aviar-dx"] and len(hist["innova-aviar-dx"]["sales"]) == 1
    assert disc_entry(index, "innova|aviar|dx||")["sales_30d"]["inferred"] == {"count": 1, "median": 15.0}

    parse(conn, "ebay", 1, status="ignored")  # a lot/bundle: not a disc at all
    stats, index, hist = export_to(conn, tmp_path)
    assert index["discs"] == [] and hist == {}
    assert stats["sales_inferred"] == 1  # still a recorded sale row


def test_sale_rows_with_placeholder_prices_or_bad_dates_are_skipped(tmp_path):
    conn = make_world()
    ebay_run(conn, day(-5), [item(i, 1000 + i) for i in range(1, 7)])
    for i in range(1, 7):
        parse(conn, "ebay", i)
    sale(conn, "ebay", 1, day(-2), 0)                 # $0.00 placeholder
    sale(conn, "ebay", 2, "not-a-date", 1200)
    sale(conn, "ebay", 3, "2026-13-45", 1300)
    sale(conn, "ebay", 4, "", 1400)
    sale(conn, "ebay", 5, day(-2) + "T09:30:00Z", 1500)  # a datetime is cut to its day
    sale(conn, "ebay", 6, day(-1), 1600)
    stats, index, hist = export_to(conn, tmp_path)
    assert [(r["date"], r["price"]) for r in hist[SLUG]["sales"]] == [(day(-1), 16.0), (day(-2), 15.0)]
    assert disc_entry(index, DESTROYER)["sales_30d"]["inferred"] == {"count": 2, "median": 15.5}
    assert stats["sales_inferred"] == 6  # the totals count rows, whatever their content


def test_sales_url_is_validated_like_listing_urls(tmp_path):
    conn = make_world()
    ebay_run(conn, day(-5), [item(1, 1500, url="javascript:alert(1)"), item(2, 1600, url="//evil.example/x"),
                            item(3, 1700, url="https://user:pw@evil.example/x"),
                            item(4, 1800, url="HTTPS://www.ebay.com/itm/4?x=1")])
    for i in range(1, 5):
        parse(conn, "ebay", i)
        sale(conn, "ebay", i, day(-i), 1000 + i)
    _, _, hist = export_to(conn, tmp_path)
    by_day = {r["date"]: r["url"] for r in hist[SLUG]["sales"]}
    assert by_day[day(-1)] is None and by_day[day(-2)] is None and by_day[day(-3)] is None
    assert by_day[day(-4)] == "HTTPS://www.ebay.com/itm/4?x=1"


@pytest.mark.parametrize("bad", [
    "https://evil.example\\www.ebay.com/itm/1",      # a browser reads the host as evil.example, urllib as www.ebay.com
    "https://www.ebay.com\\@evil.example/x",
    "https://www.ebay.com/itm/1\t.evil.example/x",   # browsers drop tabs and newlines inside a URL
    "https://www.ebay.com/itm/ 1",
    "https:www.ebay.com/itm/1",                      # no authority: a browser would still open it
])
def test_urls_that_a_browser_reads_differently_are_never_exported(tmp_path, bad):
    conn = make_world()
    ebay_run(conn, day(-5), [item(1, 1500, url=bad)])
    parse(conn, "ebay", 1)
    sale(conn, "ebay", 1, day(-1), 1000)
    _, _, hist = export_to(conn, tmp_path)
    assert [r["url"] for r in hist[SLUG]["listings"]] == [None]
    assert [r["url"] for r in hist[SLUG]["sales"]] == [None]


def test_hostile_store_names_stay_data(tmp_path):
    conn = make_world()
    evil = '<img src=x onerror=alert(1)> "quoted" & co'
    conn.execute("UPDATE stores SET name = ? WHERE id = 'ebay'", (evil,))
    conn.commit()
    ebay_run(conn, day(-2), [item(1, 1500)])
    ebay_run(conn, day(-1), [])
    parse(conn, "ebay", 1)
    _, index, hist = export_to(conn, tmp_path)
    assert hist[SLUG]["sales"][0]["store"] == evil
    assert index["stores"][0]["name"] == evil
    json.loads((tmp_path / "history" / f"{SLUG}.json").read_text(encoding="utf-8"))  # still valid JSON


def test_disc_without_sales(tmp_path):
    conn = make_world("shop")
    scrape(conn, "shop", TODAY, [prod(1, (2000, True))])
    ebay_run(conn, TODAY, [item(1, 1500)])
    parse(conn, "shop", 1)
    parse(conn, "ebay", 1)
    _, index, hist = export_to(conn, tmp_path)
    assert hist[SLUG]["sales"] == []
    assert disc_entry(index, DESTROYER)["sales_30d"] == {"confirmed": None, "inferred": None}
    assert "series_by_kind" in hist[SLUG]


def test_history_sales_are_capped_at_the_newest_100(tmp_path):
    conn = make_world()
    ebay_run(conn, day(-200), [item(i, 1000 + i) for i in range(1, 131)])
    for i in range(1, 131):
        parse(conn, "ebay", i)
        sale(conn, "ebay", i, day(-i), 1000 + i)  # item i sold i days ago
    _, index, hist = export_to(conn, tmp_path)
    rows = hist[SLUG]["sales"]
    assert len(rows) == 100
    assert [r["date"] for r in rows] == [day(-i) for i in range(1, 101)]
    assert [r["price"] for r in rows][:2] == [10.01, 10.02]
    assert disc_entry(index, DESTROYER)["sales_30d"]["inferred"]["count"] == 30  # days -1..-30


def test_same_day_sales_order_is_stable(tmp_path):
    conn = make_world()
    ebay_run(conn, day(-5), [item(i, 1000) for i in range(1, 6)])
    for i in range(1, 6):
        parse(conn, "ebay", i)
        sale(conn, "ebay", i, day(-1), 1000 + (i % 3) * 100)
    sale(conn, "ebay", 3, day(-1), 5000, source=INSIGHTS, confidence="confirmed")
    _, _, hist = export_to(conn, tmp_path)
    rows = hist[SLUG]["sales"]
    # confirmed first, then higher prices first, ties by newest listing; item 3's own inferred sale is superseded
    assert [(r["confidence"], r["price"], r["url"][-1]) for r in rows] == [
        ("confirmed", 50.0, "3"), ("low", 12.0, "5"), ("low", 12.0, "2"), ("low", 11.0, "4"), ("low", 11.0, "1")]
    export.export_site(conn, tmp_path / "again", TODAY)
    assert (tmp_path / "history" / f"{SLUG}.json").read_bytes() == (tmp_path / "again" / "history" / f"{SLUG}.json").read_bytes()


# --------------------------------------------------------------------------- output files

def test_marketplace_export_is_deterministic_and_compact(scenario, tmp_path):
    conn, out, _, _, _ = scenario
    export.export_site(conn, tmp_path, TODAY)
    for name in ("index.json", f"history/{SLUG}.json"):
        assert (out / name).read_bytes() == (tmp_path / name).read_bytes(), name
        text = (out / name).read_text(encoding="utf-8")
        assert text == json.dumps(json.loads(text), separators=(",", ":"), sort_keys=True)


def test_every_marketplace_write_is_still_temp_then_replace(scenario, tmp_path, monkeypatch):
    conn, *_ = scenario
    calls = []
    real = export.os.replace

    def spy(src, dst):
        assert str(src) == str(dst) + ".tmp"
        calls.append(dst.name)
        real(src, dst)

    monkeypatch.setattr(export.os, "replace", spy)
    export.export_site(conn, tmp_path, TODAY)
    assert calls == [f"{SLUG}.json", "index.json"]
    assert not list(tmp_path.rglob("*.tmp"))


def test_only_the_marketplace_disc_gets_the_new_history_keys(tmp_path):
    conn = make_world("shop")
    scrape(conn, "shop", TODAY, [prod(1, (2000, True)), prod(2, (1000, True), title="Innova DX Aviar")])
    ebay_run(conn, TODAY, [item(1, 1500)])
    parse(conn, "shop", 1)
    parse(conn, "shop", 2, mold="Aviar", plastic="DX")
    parse(conn, "ebay", 1)
    _, _, hist = export_to(conn, tmp_path)
    base = {"key", "slug", "manufacturer", "mold", "plastic", "edition", "player", "disc_type", "series",
            "listings", "sales"}
    assert set(hist[SLUG]) == base | {"series_by_kind"}
    assert set(hist["innova-aviar-dx"]) == base


# --------------------------------------------------------------------------- series vs. a brute-force oracle

def random_world(seed: int):
    """Two retail stores plus eBay over 15 days with outages, sell-outs, partial eBay searches and churn."""
    rnd = random.Random(seed)
    conn = make_world("a", "b")
    for off in range(-14, 1):
        d = day(off)
        for sid in ("a", "b"):
            if rnd.random() < 0.15:  # store outage
                run = db.start_run(conn, sid, d, d + "T06:00:00+00:00")
                db.finish_run(conn, run, d + "T06:05:00+00:00", "error", 0, "boom")
                continue
            prods = [RawProduct(pid, f"h{pid}", ("[Used] " if pid == 3 else "") + "Innova Star Destroyer", "Innova",
                                url=f"https://{sid}.example/products/h{pid}",
                                variants=[RawVariant(pid * 100 + v, "v", price_cents=rnd.choice([1000, 1500, 1500, 2000, 2500]),
                                                     available=rnd.random() < 0.8) for v in range(rnd.choice([1, 2]))])
                     for pid in (1, 2, 3) if rnd.random() < 0.8]
            run = db.start_run(conn, sid, d, d + "T06:00:00+00:00")
            db.record_products(conn, sid, d, prods)
            db.finish_run(conn, run, d + "T06:05:00+00:00", "ok", len(prods))
        if rnd.random() < 0.15:
            ebay_run(conn, d, [], status="error")
        else:
            found = [item(i, rnd.choice([900, 1200, 1700, 1800]), used=(i % 3 == 0))
                     for i in range(10, 16) if rnd.random() < 0.6]
            ebay_run(conn, d, found, complete=rnd.random() < 0.7)
    for sid in ("a", "b", "ebay"):
        for (pid,) in conn.execute("SELECT product_id FROM listings WHERE store_id = ?", (sid,)).fetchall():
            parse(conn, sid, pid, condition="used" if pid == 3 or (sid == "ebay" and pid % 3 == 0) else "new")
    return conn


def oracle_series(conn, condition: str, kind: str | None) -> list[dict]:
    """The daily series the slow, obvious way: for every scrape date of the stores involved, the latest observation
    of every variant on or before it, counted only for stores that have an ok run on that date."""
    kinds = {r[0]: r[1] for r in conn.execute("SELECT id, kind FROM stores")}
    ok: dict[str, set[str]] = {}
    for store, on in conn.execute("SELECT store_id, observed_on FROM runs WHERE status = 'ok'"):
        ok.setdefault(store, set()).add(on)
    variants = conn.execute("SELECT v.id, l.store_id, l.condition FROM variants v JOIN listings l "
                            "ON l.id = v.listing_id WHERE l.status = 'matched'").fetchall()
    chosen = [(pk, store) for pk, store, cond in variants
              if cond == condition and (kind is None or kinds[store] == kind)]
    obs: dict[int, list[tuple]] = {}
    for pk, on, price, avail, gone in conn.execute(
            "SELECT variant_pk, observed_on, price_cents, available, gone FROM observations ORDER BY observed_on"):
        obs.setdefault(pk, []).append((on, price, avail, gone))
    chosen = [(pk, store) for pk, store in chosen if pk in obs]
    stores = {store for _, store in chosen}
    dates = sorted(set().union(*[ok.get(s, set()) for s in stores])) if stores else []
    out = []
    for d in dates:
        prices, counted = [], set()
        for pk, store in chosen:
            if d not in ok.get(store, ()):
                continue
            latest = None
            for o in obs[pk]:
                if o[0] <= d:
                    latest = o
            if latest and latest[2] and not latest[3] and latest[1] and latest[1] > 0:
                prices.append(latest[1])
                counted.add(store)
        if prices:
            prices.sort()
            n = len(prices)
            median = prices[n // 2] if n % 2 else (prices[n // 2 - 1] + prices[n // 2] + 1) // 2
            out.append(pt(d, prices[0] / 100, median / 100, len(counted)))
    return out


def test_series_match_a_brute_force_oracle_on_random_marketplace_worlds(tmp_path):
    compared = with_kinds = 0
    for seed in range(60):
        conn = random_world(seed)
        out = tmp_path / f"s{seed}"
        export.export_site(conn, out, TODAY)
        path = out / "history" / f"{SLUG}.json"
        if not path.exists():
            continue
        compared += 1
        h = json.loads(path.read_text(encoding="utf-8"))
        for cond in ("new", "used"):
            assert h["series"][cond] == oracle_series(conn, cond, None), (seed, "all", cond)
            if "series_by_kind" in h:
                for kind in ("retail", "marketplace"):
                    assert h["series_by_kind"][kind][cond] == oracle_series(conn, cond, kind), (seed, kind, cond)
        with_kinds += "series_by_kind" in h
    assert compared >= 30 and with_kinds >= 30


# --------------------------------------------------------------------------- performance

def add_marketplace_to_big(conn, days, every: int = 3, sales_per_listing: int = 1) -> int:
    """Give every `every`-th disc of the big world an eBay listing and sales (bulk inserts)."""
    conn.execute("INSERT INTO stores(id, name, base_url, currency, kind) VALUES "
                 "('ebay', 'eBay', 'https://www.ebay.com', 'USD', 'marketplace')")
    conn.executemany(
        "INSERT INTO runs(store_id, observed_on, started_at, finished_at, status, n_products) "
        "VALUES ('ebay', ?, ?, ?, 'ok', 100)", [(d, d, d) for d in days])
    n_discs = 1250
    base_l = 10_000
    base_v = 100_000
    listings, variants, observations, sales = [], [], [], []
    count = 0
    for k in range(0, n_discs, every):
        count += 1
        lid, vpk = base_l + count, base_v + count
        listings.append((lid, "ebay", 900_000 + count, f"Innova Star Mold{k} eBay", f"https://www.ebay.com/itm/{lid}",
                         days[0], days[-1], "matched", "Innova", f"Mold{k}", "Star", "new", "Putter",
                         f"innova|mold{k}|star||"))
        variants.append((vpk, lid, vpk, "", None, days[-1], 0, 900 + k, None, 1))
        observations += [(vpk, days[0], 1000 + k, None, 1, 0), (vpk, days[200], 900 + k, None, 1, 0)]
        for s in range(sales_per_listing):
            sales.append((lid, days[300 + s], 900 + k, "inferred_disappeared" if s == 0 else f"src{s}", "low"))
    conn.executemany(
        "INSERT INTO listings(id, store_id, product_id, title, url, first_seen, last_seen, status, "
        "manufacturer, mold, plastic, condition, disc_type, disc_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", listings)
    conn.executemany(
        "INSERT INTO variants(id, listing_id, variant_id, title, weight_g, last_seen, gone, price_cents, "
        "compare_at_cents, available) VALUES (?,?,?,?,?,?,?,?,?,?)", variants)
    conn.executemany(
        "INSERT INTO observations(variant_pk, observed_on, price_cents, compare_at_cents, available, gone) "
        "VALUES (?,?,?,?,?,?)", observations)
    conn.executemany(
        "INSERT INTO sales(listing_id, sold_on, price_cents, quantity, source, confidence) "
        "VALUES (?,?,?,1,?,?)", sales)
    conn.commit()
    return count


def test_bulk_export_with_marketplace_data_stays_fast_and_bulk(tmp_path):
    conn, days = build_big()
    n = add_marketplace_to_big(conn, days, every=3, sales_per_listing=3)
    statements = []
    conn.set_trace_callback(statements.append)
    start = time.perf_counter()
    stats = export.export_site(conn, tmp_path, TODAY)
    elapsed = time.perf_counter() - start
    conn.set_trace_callback(None)

    assert elapsed < 10, f"export took {elapsed:.1f}s"
    assert len([s for s in statements if s.lstrip().upper().startswith("SELECT")]) <= 12
    assert stats["sales_inferred"] == n * 3 and stats["sales_confirmed"] == 0
    index, hist = load(tmp_path)
    assert len(index["discs"]) == 1250
    with_mp = [h for h in hist.values() if "series_by_kind" in h]
    assert len(with_mp) == n == len([d for d in index["discs"] if "retail" in d])
    h = hist["innova-mold3-star"]
    assert len(h["sales"]) == 3 and set(h["series_by_kind"]) == {"retail", "marketplace"}
    assert [p["min"] for p in h["series_by_kind"]["marketplace"]["new"][:1]] == [10.03]
    assert "series_by_kind" not in hist["innova-mold1-star"]
