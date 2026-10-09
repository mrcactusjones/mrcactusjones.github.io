import json
import os
import re
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

from disctracker import db, export
from disctracker.models import ParsedListing, RawProduct, RawVariant

TODAY = "2026-10-08"
DESTROYER = "innova|destroyer|star||"
AVIAR = "innova|aviar|dx||"


# --------------------------------------------------------------------------- helpers

def day(offset: int) -> str:
    """ISO date `offset` days from TODAY."""
    return (date.fromisoformat(TODAY) + timedelta(days=offset)).isoformat()


def weight(title: str):
    return int(title[:-1]) if title.endswith("g") and title[:-1].isdigit() else None


def make_conn(*store_ids: str):
    conn = db.connect(":memory:")
    for sid in store_ids:
        db.upsert_store(conn, {"id": sid, "name": f"{sid.title()} Discs",
                               "base_url": f"https://{sid}.example/", "currency": "USD"})
    return conn


def prod(pid: int, *variants, title: str | None = None) -> RawProduct:
    """variants: (price_cents, available[, variant_title[, compare_at_cents]])"""
    vs = []
    for i, v in enumerate(variants):
        price, avail, *rest = v
        vs.append(RawVariant(pid * 100 + i, rest[0] if rest else "Default Title",
                             price_cents=price, available=avail,
                             compare_at_cents=rest[1] if len(rest) > 1 else None))
    return RawProduct(pid, f"h{pid}", title or f"Product {pid}", vendor="Innova",
                      url=f"https://shop.example/products/h{pid}", variants=vs)


def scrape(conn, store_id: str, on: str, products=(), status: str = "ok") -> None:
    run = db.start_run(conn, store_id, on, on + "T06:00:00+00:00")
    if status == "ok":
        db.record_products(conn, store_id, on, products, weight_parser=weight)
    db.finish_run(conn, run, on + "T06:05:00+00:00", status,
                  len(list(products)) if status == "ok" else 0, None if status == "ok" else "boom")


def parse(conn, store_id: str, pid: int, status: str = "matched", manufacturer: str = "Innova",
          mold: str = "Destroyer", plastic: str = "Star", edition: str = "", player: str = "",
          condition: str = "new", disc_type: str = "Distance Driver") -> None:
    lid = conn.execute("SELECT id FROM listings WHERE store_id=? AND product_id=?",
                       (store_id, pid)).fetchone()[0]
    db.save_parse(conn, lid, ParsedListing(
        status=status, manufacturer=manufacturer, mold=mold, plastic=plastic, edition=edition,
        player=player, condition=condition, disc_type=disc_type, confidence=0.9), 1)
    conn.commit()


def load(out: Path):
    index = json.loads((out / "index.json").read_text(encoding="utf-8"))
    hist = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in (out / "history").glob("*.json")}
    return index, hist


def disc_entry(index: dict, key: str) -> dict:
    return next(d for d in index["discs"] if d["key"] == key)


def pt(date_: str, lo: float, med: float, n: int) -> dict:
    return {"date": date_, "min": lo, "median": med, "stores_in_stock": n}


def one_disc(points: dict[str, tuple[int, bool]], condition: str = "new"):
    """A single store/listing/variant scraped on the given dates {date: (price_cents, available)}."""
    conn = make_conn("s")
    for i, (on, (price, avail)) in enumerate(sorted(points.items())):
        scrape(conn, "s", on, [prod(1, (price, avail))])
        if i == 0:
            parse(conn, "s", 1, condition=condition)
    return conn


# --------------------------------------------------------------------------- the big scenario
# Destroyer Star (new) over 2026-10-01..08, prices in cents, "x" = out of stock / absent:
#   day:      1     2     3     4     5     6     7     8
#   alpha v1  1799  1799  1799  1699  1699  1699  1699  1699
#   alpha v2  1799  1799  1799  1799  x     x     x     x       (sold out on day 5)
#   beta      1899  1899  OUT   1899  1899  1899  gone  gone    (beta outage on day 3, delisted day 7)
#   gamma     x     x     x     x     x     2000  2000  2000    (restocked on day 6)

DAYS = [f"2026-10-0{n}" for n in range(1, 9)]


def alpha(n: int):
    items = [
        prod(1, (1799 if n < 4 else 1699, True, "175g"), (1799, n < 5, "170g"),
             title="Innova Star Destroyer 175g"),
        prod(8, (500, True), title="Innova Rare Thing"),
        prod(9, (300, True), title="mystery"),
    ]
    if n >= 2:
        items.append(prod(3, (999, True), title="[Used] Star Destroyer 9/10"))
    if n in (4, 5):
        items.append(prod(4, (1499, True), title="[Used] Star Destroyer 7/10"))
    return items


def beta(n: int):
    items = [prod(2, (2500, True), title="Innova Tote Backpack")]
    if n <= 2 or 4 <= n <= 6:
        items.append(prod(1, (1899, True), title="INNOVA Star Destroyer"))
    if n <= 2:
        items.append(prod(5, (800, True), title="Innova DX Aviar"))
    if n <= 4:
        items.append(prod(6, (1200, False), title="Innova Star Wraith"))
    return items


def gamma(n: int):
    return [prod(1, (2000, n >= 6), title="Innova Destroyer Star")]


def build_world():
    conn = make_conn("alpha", "beta", "gamma", "delta")
    for n, on in enumerate(DAYS, start=1):
        scrape(conn, "alpha", on, alpha(n))
        if n == 3:
            scrape(conn, "beta", on, status="error")
        else:
            scrape(conn, "beta", on, beta(n))
        scrape(conn, "gamma", on, gamma(n))
    scrape(conn, "delta", DAYS[-1], status="error")  # never succeeded: last_ok is null
    parse(conn, "alpha", 1)
    parse(conn, "alpha", 3, condition="used")
    parse(conn, "alpha", 4, condition="used")
    parse(conn, "alpha", 8, status="review", mold="Rare", plastic="")
    parse(conn, "alpha", 9, status="unparsed", manufacturer="", mold="", plastic="")
    parse(conn, "beta", 1, manufacturer="INNOVA")
    parse(conn, "beta", 2, status="ignored", mold="", plastic="")
    parse(conn, "beta", 5, mold="Aviar", plastic="DX", disc_type="Putter")
    parse(conn, "beta", 6, mold="Wraith")
    parse(conn, "gamma", 1, disc_type="")
    return conn


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    conn = build_world()
    out = tmp_path_factory.mktemp("site-data")
    stats = export.export_site(conn, out, TODAY)
    index, hist = load(out)
    return conn, out, stats, index, hist


# --------------------------------------------------------------------------- index.json

def test_index_header_stores_and_stats(world):
    _, _, stats, index, _ = world
    assert index["generated_at"] == TODAY
    assert index["currency"] == "USD"
    assert index["stores"] == [
        {"id": "alpha", "name": "Alpha Discs", "base_url": "https://alpha.example", "kind": "retail",
         "last_ok": "2026-10-08"},
        {"id": "beta", "name": "Beta Discs", "base_url": "https://beta.example", "kind": "retail",
         "last_ok": "2026-10-08"},
        {"id": "delta", "name": "Delta Discs", "base_url": "https://delta.example", "kind": "retail",
         "last_ok": None},
        {"id": "gamma", "name": "Gamma Discs", "base_url": "https://gamma.example", "kind": "retail",
         "last_ok": "2026-10-08"},
    ]
    assert index["stats"] == {"listings": 10, "matched": 7, "review": 1, "ignored": 1,
                              "unparsed": 1, "discs": 2, "stores": 4,
                              "sales_confirmed": 0, "sales_inferred": 0}
    assert stats == index["stats"]


def test_review_ignored_unparsed_are_not_discs(world):
    _, out, _, index, hist = world
    assert [d["key"] for d in index["discs"]] == [AVIAR, DESTROYER]  # sorted by mold; Wraith skipped
    assert sorted(hist) == ["innova-aviar-dx", "innova-destroyer-star"]
    blob = (out / "index.json").read_text()
    for needle in ("Rare", "Backpack", "mystery"):
        assert needle not in blob
    assert index["stats"]["review"] == 1  # but they are counted


def test_destroyer_index_entry(world):
    _, _, _, index, _ = world
    d = disc_entry(index, DESTROYER)
    assert (d["slug"], d["manufacturer"], d["mold"], d["plastic"], d["edition"], d["player"]) == (
        "innova-destroyer-star", "Innova", "Destroyer", "Star", "", "")
    assert d["disc_type"] == "Distance Driver"  # most common non-empty value
    assert d["last_seen"] == "2026-10-08"
    # live: alpha v1 16.99 + gamma 20.00 in stock (alpha v2 is sold out, beta delisted)
    assert d["new"] == {"min": 16.99, "median": 18.5, "stores_in_stock": 2, "stores_listing": 2,
                        "change_7d": -0.0556, "change_30d": None}
    assert d["used"] == {"min": 9.99, "median": 9.99, "stores_in_stock": 1, "stores_listing": 1,
                         "change_7d": None, "change_30d": None}
    # retail-only disc: no marketplace data, so no `retail` blocks; no sales either
    assert d["sales_30d"] == {"confirmed": None, "inferred": None} and "retail" not in d


def test_disc_with_only_gone_listings_is_still_exported(world):
    _, _, _, index, hist = world
    d = disc_entry(index, AVIAR)
    assert d["new"] is None and d["used"] is None
    assert d["disc_type"] == "Putter"
    assert d["last_seen"] == "2026-10-02"
    h = hist["innova-aviar-dx"]
    assert h["listings"] == []
    # beta's outage day (10-03) is not forward-filled; delisted from 10-04 on
    assert h["series"] == {"new": [pt("2026-10-01", 8.0, 8.0, 1), pt("2026-10-02", 8.0, 8.0, 1)],
                           "used": []}


def test_disc_never_in_stock_and_gone_is_not_exported(world):
    _, out, _, index, _ = world
    assert all(d["mold"] != "Wraith" for d in index["discs"])
    assert not (out / "history" / "innova-wraith-star.json").exists()


# --------------------------------------------------------------------------- history series

def test_new_series_median_min_outage_oos_and_delist(world):
    _, _, _, _, hist = world
    assert hist["innova-destroyer-star"]["series"]["new"] == [
        pt("2026-10-01", 17.99, 17.99, 2),  # 1799 1799 1899
        pt("2026-10-02", 17.99, 17.99, 2),  # unchanged day still gets a point (forward-fill)
        pt("2026-10-03", 17.99, 17.99, 1),  # beta outage: only alpha counted, not forward-filled
        pt("2026-10-04", 16.99, 17.99, 2),  # alpha v1 drops to 1699; beta back via forward-fill
        pt("2026-10-05", 16.99, 17.99, 2),  # v2 sold out: 1699 1899 -> median 1799
        pt("2026-10-06", 16.99, 18.99, 3),  # gamma restocks: odd count, median is the middle
        pt("2026-10-07", 16.99, 18.5, 2),   # beta delisted: 1699 2000 -> 1849.5 rounds half up
        pt("2026-10-08", 16.99, 18.5, 2),
    ]


def test_used_series_is_separate_and_starts_when_listed(world):
    _, _, _, _, hist = world
    assert hist["innova-destroyer-star"]["series"]["used"] == [
        pt("2026-10-02", 9.99, 9.99, 1),   # nothing used existed on 10-01 -> no point
        pt("2026-10-03", 9.99, 9.99, 1),
        pt("2026-10-04", 9.99, 12.49, 1),  # second used copy appears
        pt("2026-10-05", 9.99, 12.49, 1),
        pt("2026-10-06", 9.99, 9.99, 1),   # ...and sells (listing gone)
        pt("2026-10-07", 9.99, 9.99, 1),
        pt("2026-10-08", 9.99, 9.99, 1),
    ]


def test_history_listings_are_live_variants_cheapest_first(world):
    _, _, _, _, hist = world
    rows = hist["innova-destroyer-star"]["listings"]
    assert [(r["store_id"], r["condition"], r["price"], r["available"], r["weight_g"]) for r in rows] == [
        ("alpha", "used", 9.99, True, None),
        ("alpha", "new", 16.99, True, 175),
        ("alpha", "new", 17.99, False, 170),  # sold out but still listed
        ("gamma", "new", 20.0, True, None),
    ]
    first = rows[1]
    assert first["store"] == "Alpha Discs"
    assert first["title"] == "Innova Star Destroyer 175g"
    assert first["url"] == "https://shop.example/products/h1"
    assert first["compare_at"] is None
    assert first["last_seen"] == "2026-10-08"
    assert set(first) == {"store", "store_id", "store_kind", "title", "url", "condition", "weight_g",
                          "price", "compare_at", "available", "last_seen"}
    assert first["store_kind"] == "retail"


def test_history_identity_fields(world):
    _, _, _, _, hist = world
    h = hist["innova-destroyer-star"]
    assert {k: h[k] for k in ("key", "slug", "manufacturer", "mold", "plastic", "edition",
                              "player", "disc_type")} == {
        "key": DESTROYER, "slug": "innova-destroyer-star", "manufacturer": "Innova",
        "mold": "Destroyer", "plastic": "Star", "edition": "", "player": "",
        "disc_type": "Distance Driver"}
    # a retail-only disc has no marketplace data, so no series_by_kind; sales is always present
    assert set(h) == {"key", "slug", "manufacturer", "mold", "plastic", "edition", "player",
                      "disc_type", "series", "listings", "sales"}
    assert h["sales"] == []


# --------------------------------------------------------------------------- math

@pytest.mark.parametrize("prices,expected", [
    ([500], (500, 500)),
    ([100, 300], (100, 200)),
    ([1699, 2000], (1699, 1850)),      # 1849.5 -> half up
    ([100, 101], (100, 101)),          # 100.5 -> 101
    ([100, 200, 900], (100, 200)),
    ([100, 200, 300, 900], (100, 250)),
])
def test_min_median(prices, expected):
    assert export._min_median(prices) == expected


def test_three_variants_two_stores_math(tmp_path):
    conn = make_conn("a", "b")
    scrape(conn, "a", TODAY, [prod(1, (2000, True, "175g", 2500), (3000, True, "170g"))])
    scrape(conn, "b", TODAY, [prod(1, (1000, True))])
    parse(conn, "a", 1)
    parse(conn, "b", 1)
    export.export_site(conn, tmp_path, TODAY)
    index, hist = load(tmp_path)
    block = disc_entry(index, DESTROYER)["new"]
    assert (block["min"], block["median"], block["stores_in_stock"], block["stores_listing"]) == (
        10.0, 20.0, 2, 2)
    assert hist["innova-destroyer-star"]["series"]["new"] == [pt(TODAY, 10.0, 20.0, 2)]
    compare = {r["price"]: r["compare_at"] for r in hist["innova-destroyer-star"]["listings"]}
    assert compare == {10.0: None, 20.0: 25.0, 30.0: None}


def test_delisted_variant_of_a_live_listing(tmp_path):
    conn = make_conn("s")
    scrape(conn, "s", day(-1), [prod(1, (1000, True, "175g"), (3000, True, "170g"))])
    scrape(conn, "s", day(0), [prod(1, (1000, True, "175g"))])  # the 170g option was removed
    parse(conn, "s", 1)
    export.export_site(conn, tmp_path, TODAY)
    index, hist = load(tmp_path)
    h = hist["innova-destroyer-star"]
    assert [r["weight_g"] for r in h["listings"]] == [175]
    assert h["series"]["new"] == [pt(day(-1), 10.0, 20.0, 1), pt(day(0), 10.0, 10.0, 1)]
    assert disc_entry(index, DESTROYER)["new"]["median"] == 10.0


def test_delisted_variant_price_is_not_a_last_listed_price(tmp_path):
    conn = make_conn("s")
    scrape(conn, "s", day(-1), [prod(1, (2000, False, "175g"), (1000, False, "170g"))])
    scrape(conn, "s", day(0), [prod(1, (2000, False, "175g"))])
    parse(conn, "s", 1)
    export.export_site(conn, tmp_path, TODAY)
    block = disc_entry(load(tmp_path)[0], DESTROYER)["new"]
    assert (block["min"], block["median"], block["stores_in_stock"]) == (20.0, 20.0, 0)


def test_status_is_checked_even_if_disc_key_is_stale(tmp_path):
    conn = one_disc({day(0): (1000, True)})
    conn.execute("UPDATE listings SET status = 'review'")  # disc_key left behind
    conn.commit()
    stats = export.export_site(conn, tmp_path, TODAY)
    assert stats["discs"] == 0 and stats["review"] == 1
    assert load(tmp_path)[1] == {}


def test_all_out_of_stock_uses_last_listed_prices(tmp_path):
    conn = one_disc({day(-10): (2000, True), day(0): (2100, False)})
    export.export_site(conn, tmp_path, TODAY)
    index, hist = load(tmp_path)
    assert disc_entry(index, DESTROYER)["new"] == {
        "min": 21.0, "median": 21.0, "stores_in_stock": 0, "stores_listing": 1,
        "change_7d": None, "change_30d": None}
    # the out-of-stock day is simply absent from the series (no zero points)
    assert hist["innova-destroyer-star"]["series"]["new"] == [pt(day(-10), 20.0, 20.0, 1)]


# --------------------------------------------------------------------------- change_7d / change_30d

@pytest.mark.parametrize("start", [31, 30, 29, 8, 7, 6, 1, 0])
def test_change_window_boundaries(tmp_path, start):
    # series starts `start` days ago at $20, today $15 -> -25% when it reaches back far enough
    conn = one_disc({day(-start): (2000, True), day(0): (1500, True)})
    export.export_site(conn, tmp_path, TODAY)
    block = disc_entry(load(tmp_path)[0], DESTROYER)["new"]
    assert block["change_7d"] == (-0.25 if start >= 7 else None)
    assert block["change_30d"] == (-0.25 if start >= 30 else None)


def test_change_uses_nearest_point_at_or_before_target(tmp_path):
    # no point exactly on today-7 / today-30: fall back to the closest earlier one
    conn = one_disc({day(-37): (2000, True), day(-18): (1000, True), day(0): (1500, True)})
    export.export_site(conn, tmp_path, TODAY)
    block = disc_entry(load(tmp_path)[0], DESTROYER)["new"]
    assert block["change_7d"] == 0.5     # vs day -18 ($10)
    assert block["change_30d"] == -0.25  # vs day -37 ($20)


def test_change_zero_is_plain_zero(tmp_path):
    conn = one_disc({day(-8): (1500, True), day(0): (1500, True)})
    export.export_site(conn, tmp_path, TODAY)
    change = disc_entry(load(tmp_path)[0], DESTROYER)["new"]["change_7d"]
    assert change == 0.0 and str(change) == "0.0"


def test_change_only_for_series_of_same_condition(tmp_path):
    conn = one_disc({day(-10): (2000, True), day(0): (1000, True)}, condition="used")
    export.export_site(conn, tmp_path, TODAY)
    entry = disc_entry(load(tmp_path)[0], DESTROYER)
    assert entry["new"] is None
    assert entry["used"]["change_7d"] == -0.5


# --------------------------------------------------------------------------- outages and runs

def test_error_run_does_not_hide_an_ok_run_on_the_same_day(tmp_path):
    conn = make_conn("s")
    scrape(conn, "s", day(-2), [prod(1, (1000, True))])
    scrape(conn, "s", day(-1), status="error")
    scrape(conn, "s", day(-1), [prod(1, (1000, True))])  # retry succeeded
    parse(conn, "s", 1)
    export.export_site(conn, tmp_path, TODAY)
    series = load(tmp_path)[1]["innova-destroyer-star"]["series"]["new"]
    assert [p["date"] for p in series] == [day(-2), day(-1)]


def test_outage_day_is_missing_for_a_single_store_disc(tmp_path):
    conn = make_conn("s")
    scrape(conn, "s", day(-3), [prod(1, (1000, True))])
    scrape(conn, "s", day(-2), status="error")
    scrape(conn, "s", day(-1), [prod(1, (1000, True))])
    parse(conn, "s", 1)
    export.export_site(conn, tmp_path, TODAY)
    series = load(tmp_path)[1]["innova-destroyer-star"]["series"]["new"]
    assert [p["date"] for p in series] == [day(-3), day(-1)]


def test_other_stores_ok_run_does_not_revive_a_down_store(tmp_path):
    conn = make_conn("a", "b")
    scrape(conn, "a", day(-1), [prod(1, (1000, True))])
    scrape(conn, "a", day(0), status="error")  # a is down today ...
    scrape(conn, "b", day(0), [prod(2, (5000, True), title="other")])  # ... b scraped fine
    parse(conn, "a", 1)
    export.export_site(conn, tmp_path, TODAY)
    series = load(tmp_path)[1]["innova-destroyer-star"]["series"]["new"]
    assert [p["date"] for p in series] == [day(-1)]


def test_observations_before_first_scrape_date_apply_on_first_ok_date(tmp_path):
    conn = make_conn("s")
    scrape(conn, "s", day(-5), [prod(1, (1000, True))])
    conn.execute("DELETE FROM runs")  # lose the run record of the first day; keep observations
    scrape(conn, "s", day(-4), [prod(1, (1000, True))])
    parse(conn, "s", 1)
    export.export_site(conn, tmp_path, TODAY)
    series = load(tmp_path)[1]["innova-destroyer-star"]["series"]["new"]
    assert series == [pt(day(-4), 10.0, 10.0, 1)]


# --------------------------------------------------------------------------- identity / names / slugs

def test_display_name_prefers_most_common_casing(tmp_path):
    conn = make_conn("a", "b", "c")
    for sid, maker in (("a", "INNOVA"), ("b", "Innova"), ("c", "innova")):
        scrape(conn, sid, TODAY, [prod(1, (1000, True))])
        parse(conn, sid, 1, manufacturer=maker)
    export.export_site(conn, tmp_path, TODAY)
    # three different casings tie 1-1-1 -> mixed case beats all-upper / all-lower
    assert load(tmp_path)[0]["discs"][0]["manufacturer"] == "Innova"


def test_edition_and_player_in_key_and_slug(tmp_path):
    conn = make_conn("s")
    scrape(conn, "s", TODAY, [prod(1, (3000, True)), prod(2, (1500, True))])
    parse(conn, "s", 1, edition="tour series", player="Ricky Wysocki")
    parse(conn, "s", 2)
    export.export_site(conn, tmp_path, TODAY)
    index, hist = load(tmp_path)
    assert [d["key"] for d in index["discs"]] == [
        DESTROYER, "innova|destroyer|star|tour series|ricky wysocki"]
    tour = index["discs"][1]
    assert (tour["edition"], tour["player"], tour["slug"]) == (
        "tour series", "Ricky Wysocki", "innova-destroyer-star-tour-series-ricky-wysocki")
    assert sorted(hist) == ["innova-destroyer-star", "innova-destroyer-star-tour-series-ricky-wysocki"]


def test_discs_sorted_by_manufacturer_mold_plastic(tmp_path):
    conn = make_conn("s")
    rows = [("Innova", "Roc", "Star"), ("Discraft", "Buzzz", "ESP"), ("Innova", "Roc", "DX"),
            ("Innova", "aviar", "DX"), ("discraft", "Zone", "Z")]
    scrape(conn, "s", TODAY, [prod(i, (1000, True)) for i in range(1, len(rows) + 1)])
    for i, (m, mo, pl) in enumerate(rows, start=1):
        parse(conn, "s", i, manufacturer=m, mold=mo, plastic=pl)
    export.export_site(conn, tmp_path, TODAY)
    assert [(d["manufacturer"], d["mold"], d["plastic"]) for d in load(tmp_path)[0]["discs"]] == [
        ("Discraft", "Buzzz", "ESP"), ("discraft", "Zone", "Z"), ("Innova", "aviar", "DX"),
        ("Innova", "Roc", "DX"), ("Innova", "Roc", "Star")]


def test_slug_collision_guard(tmp_path, monkeypatch):
    conn = make_conn("s")
    scrape(conn, "s", TODAY, [prod(1, (1000, True)), prod(2, (2000, True)), prod(3, (3000, True))])
    parse(conn, "s", 1, mold="Aviar")
    parse(conn, "s", 2, mold="Buzzz")
    parse(conn, "s", 3, mold="Crush")
    forced = {"innova|aviar|star||": "same", "innova|buzzz|star||": "same",
              "innova|crush|star||": "same-2"}  # crush legitimately owns "same-2"
    monkeypatch.setattr(export, "key_slug", lambda key: forced[key])
    export.export_site(conn, tmp_path, TODAY)
    index, hist = load(tmp_path)
    slugs = {d["key"]: d["slug"] for d in index["discs"]}
    assert slugs == {"innova|aviar|star||": "same", "innova|buzzz|star||": "same-3",
                     "innova|crush|star||": "same-2"}
    assert sorted(hist) == ["same", "same-2", "same-3"]
    assert {h["key"] for h in hist.values()} == set(forced)  # no history file overwritten


def test_non_ascii_text_round_trips(tmp_path):
    conn = make_conn("s")
    scrape(conn, "s", TODAY, [prod(1, (1000, True), title="Innova Star Destroyer ☃ café")])
    parse(conn, "s", 1)
    export.export_site(conn, tmp_path, TODAY)
    row = load(tmp_path)[1]["innova-destroyer-star"]["listings"][0]
    assert row["title"] == "Innova Star Destroyer ☃ café"


def test_missing_url_falls_back_to_handle(tmp_path):
    conn = make_conn("s")
    p = prod(1, (1000, True))
    p.url = ""
    scrape(conn, "s", TODAY, [p])
    parse(conn, "s", 1)
    export.export_site(conn, tmp_path, TODAY)
    assert load(tmp_path)[1]["innova-destroyer-star"]["listings"][0]["url"] == "https://s.example/products/h1"


# --------------------------------------------------------------------------- adversarial review regressions

def test_overlong_slug_is_clipped_not_fatal(tmp_path):
    # A scraped title can leave a very long player name; the file name would exceed the
    # filesystem limit (255 bytes) and abort the whole export.
    conn = make_conn("s")
    scrape(conn, "s", TODAY, [prod(1, (1000, True)), prod(2, (1100, True)), prod(3, (1200, True))])
    parse(conn, "s", 1, edition="tour series", player="Ricky " + "Wysocki " * 40)
    parse(conn, "s", 2, edition="tour series", player="Ricky " + "Wysocki " * 39 + "Jr")  # same clipped prefix
    parse(conn, "s", 3)
    export.export_site(conn, tmp_path, TODAY)
    index, hist = load(tmp_path)
    slugs = [d["slug"] for d in index["discs"]]
    assert len(slugs) == 3 and len(set(slugs)) == 3
    for slug in slugs:
        assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug)  # what the site accepts
        assert len(slug) <= 110
    assert "innova-destroyer-star" in slugs  # short slugs are untouched
    assert sorted(hist) == sorted(slugs)
    assert not list(tmp_path.rglob("*.tmp"))


def test_failed_export_does_not_orphan_the_published_index(tmp_path, monkeypatch):
    # Stale history files must only disappear once the new index has replaced the old one;
    # otherwise a crash in between leaves index.json pointing at deleted history files.
    conn = make_conn("s")
    scrape(conn, "s", TODAY, [prod(1, (1000, True)), prod(2, (1100, True))])
    parse(conn, "s", 1)
    parse(conn, "s", 2, mold="Aviar")
    export.export_site(conn, tmp_path, TODAY)
    published = json.loads((tmp_path / "index.json").read_text())
    assert len(published["discs"]) == 2

    parse(conn, "s", 2, status="ignored")  # Aviar leaves the export
    real = os.replace

    def boom(src, dst):
        if Path(dst).name == "index.json":
            raise OSError("disk full")
        real(src, dst)

    monkeypatch.setattr(export.os, "replace", boom)
    with pytest.raises(OSError):
        export.export_site(conn, tmp_path, TODAY)
    still_published = json.loads((tmp_path / "index.json").read_text())
    assert still_published == published
    for d in still_published["discs"]:
        assert (tmp_path / "history" / f"{d['slug']}.json").is_file(), d["slug"]


@pytest.mark.parametrize("bad", [
    "javascript:alert(1)", "  JavaScript:alert(1)", "java\nscript:alert(1)", "data:text/html,<b>x</b>",
    "//evil.example/x", "ftp://s.example/x", "https://user:pw@evil.example/x", "https:///nohost", "/relative"])
def test_non_http_urls_are_never_exported(tmp_path, bad):
    conn = make_conn("s")
    scrape(conn, "s", TODAY, [prod(1, (1000, True))])
    parse(conn, "s", 1)
    conn.execute("UPDATE listings SET url = ?", (bad,))
    conn.commit()
    export.export_site(conn, tmp_path, TODAY)
    row = load(tmp_path)[1]["innova-destroyer-star"]["listings"][0]
    assert row["url"] == "https://s.example/products/h1"  # rebuilt from the trusted store base_url + handle

    conn.execute("UPDATE listings SET handle = NULL")
    conn.commit()
    export.export_site(conn, tmp_path, TODAY)
    assert load(tmp_path)[1]["innova-destroyer-star"]["listings"][0]["url"] is None


def test_http_urls_are_kept(tmp_path):
    conn = make_conn("s")
    scrape(conn, "s", TODAY, [prod(1, (1000, True))])
    parse(conn, "s", 1)
    conn.execute("UPDATE listings SET url = 'HTTP://Shop.Example/products/x?variant=1#top'")
    conn.commit()
    export.export_site(conn, tmp_path, TODAY)
    assert load(tmp_path)[1]["innova-destroyer-star"]["listings"][0]["url"] == \
        "HTTP://Shop.Example/products/x?variant=1#top"


def test_unpriced_and_zero_priced_variants_are_not_live(tmp_path):
    # price NULL can only come from a damaged DB, 0 from a placeholder product ("call for price")
    conn = make_conn("s")
    scrape(conn, "s", day(-1), [prod(1, (1500, True, "175g"), (1600, True, "170g"), (1700, True, "172g"))])
    scrape(conn, "s", day(0), [prod(1, (1500, True, "175g"), (0, True, "170g"), (1700, True, "172g"))])
    parse(conn, "s", 1)
    conn.execute("UPDATE variants SET price_cents = NULL WHERE variant_id = 102")
    conn.commit()
    export.export_site(conn, tmp_path, TODAY)
    index, hist = load(tmp_path)
    h = hist["innova-destroyer-star"]
    assert [(r["weight_g"], r["price"]) for r in h["listings"]] == [(175, 15.0)]
    block = disc_entry(index, DESTROYER)["new"]
    assert (block["min"], block["median"], block["stores_in_stock"], block["stores_listing"]) == (
        15.0, 15.0, 1, 1)
    # the $0.00 day never becomes a series point (min would be 0 and a change could not be computed)
    assert [(p["date"], p["min"]) for p in h["series"]["new"]] == [(day(-1), 15.0), (day(0), 15.0)]
    assert block["change_7d"] is None


# --------------------------------------------------------------------------- output files

def test_empty_database(tmp_path):
    stats = export.export_site(db.connect(":memory:"), tmp_path / "out", TODAY)
    assert stats == {"listings": 0, "matched": 0, "review": 0, "ignored": 0, "unparsed": 0,
                     "discs": 0, "stores": 0, "sales_confirmed": 0, "sales_inferred": 0}
    index, hist = load(tmp_path / "out")
    assert index == {"generated_at": TODAY, "currency": "USD", "stores": [], "stats": stats, "discs": []}
    assert hist == {}


def test_out_dir_may_be_a_string_and_is_created(tmp_path):
    export.export_site(make_conn("s"), str(tmp_path / "a" / "b"), TODAY)
    assert (tmp_path / "a" / "b" / "index.json").is_file()
    assert (tmp_path / "a" / "b" / "history").is_dir()


def test_files_are_compact_sorted_json(world):
    _, out, _, _, _ = world
    files = [out / "index.json", *sorted((out / "history").glob("*.json"))]
    assert len(files) == 3
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert text == json.dumps(json.loads(text), separators=(",", ":"), sort_keys=True)


def test_deterministic_output(world, tmp_path):
    conn, out, _, _, _ = world
    again = tmp_path / "again"
    export.export_site(conn, again, TODAY)
    export.export_site(conn, out, TODAY)  # re-export over the existing files
    names = sorted(p.relative_to(out) for p in out.rglob("*") if p.is_file())
    assert names == sorted(p.relative_to(again) for p in again.rglob("*") if p.is_file())
    for name in names:
        assert (out / name).read_bytes() == (again / name).read_bytes(), name


def test_stale_history_files_are_deleted(tmp_path):
    conn = one_disc({day(0): (1000, True)})
    hist_dir = tmp_path / "history"
    hist_dir.mkdir()
    (hist_dir / "old-disc.json").write_text("{}")
    (hist_dir / "half.json.tmp").write_text("{")
    (hist_dir / ".gitkeep").write_text("")
    (hist_dir / "notes.txt").write_text("keep me")
    (hist_dir / "subdir").mkdir()
    export.export_site(conn, tmp_path, TODAY)
    assert sorted(p.name for p in hist_dir.iterdir()) == [
        ".gitkeep", "innova-destroyer-star.json", "notes.txt", "subdir"]

    # the disc is re-parsed as something else -> its file disappears on the next export
    parse(conn, "s", 1, status="ignored")
    export.export_site(conn, tmp_path, TODAY)
    assert sorted(p.name for p in hist_dir.iterdir()) == [".gitkeep", "notes.txt", "subdir"]
    assert load(tmp_path)[0]["discs"] == []


def test_every_write_is_a_temp_file_then_os_replace(tmp_path, monkeypatch):
    conn = one_disc({day(-1): (1000, True), day(0): (1000, True)})
    real = os.replace
    calls = []

    def spy(src, dst):
        src, dst = Path(src), Path(dst)
        assert src.parent == dst.parent and src.name == dst.name + ".tmp"
        assert src.exists() and (not dst.exists() or dst.read_text() != src.read_text())
        calls.append(dst.name)
        real(src, dst)

    monkeypatch.setattr(export.os, "replace", spy)
    export.export_site(conn, tmp_path, TODAY)
    assert sorted(calls) == ["index.json", "innova-destroyer-star.json"]
    assert calls[-1] == "index.json"  # the index lands last
    assert not list(tmp_path.rglob("*.tmp"))


def test_failed_write_leaves_previous_file_and_no_temp(tmp_path, monkeypatch):
    conn = one_disc({day(0): (1000, True)})
    export.export_site(conn, tmp_path, TODAY)
    before = (tmp_path / "index.json").read_bytes()
    scrape(conn, "s", day(1), [prod(1, (900, True))])  # new data, so the index would change
    real = os.replace

    def boom(src, dst):
        if Path(dst).name == "index.json":
            raise OSError("disk full")
        real(src, dst)

    monkeypatch.setattr(export.os, "replace", boom)
    with pytest.raises(OSError):
        export.export_site(conn, tmp_path, TODAY)
    assert (tmp_path / "index.json").read_bytes() == before
    assert not list(tmp_path.rglob("*.tmp"))


# --------------------------------------------------------------------------- performance

def build_big(n_listings: int = 5000, variants_per: int = 4, n_days: int = 365, n_discs: int = 1250):
    """One store, n_listings*variants_per variants, change-only observations over n_days.

    Rows are bulk-inserted (record_products over 365 days would dominate the runtime);
    the shape matches what record_products writes.
    """
    conn = make_conn("big")
    days = [day(-(n_days - 1 - i)) for i in range(n_days)]
    n_variants = n_listings * variants_per
    conn.executemany(
        "INSERT INTO runs(store_id, observed_on, started_at, finished_at, status, n_products) "
        "VALUES ('big', ?, ?, ?, 'ok', ?)", [(d, d, d, n_listings) for d in days])
    listings, variants, observations = [], [], []
    for i in range(n_listings):
        k = i % n_discs
        condition = "used" if k % 5 == 0 else "new"
        listings.append((i + 1, "big", i + 1, f"h{i}", f"Innova Star Mold{k}", f"https://big.example/p/h{i}",
                         days[0], days[-1], "matched", "Innova", f"Mold{k}", "Star", condition,
                         "Putter", f"innova|mold{k}|star||"))
        for j in range(variants_per):
            vpk = i * variants_per + j + 1
            base = 1000 + (vpk % 50) * 10
            # (offset, price, available): a price change, a sell-out and a restock (odd variants
            # stay in stock, so every disc has a point on every date)
            steps = [(0, base, 1), (50 + vpk % 10, base - 100, 1),
                     (120 + vpk % 10, base - 100, 1 if vpk % 2 else 0), (200 + vpk % 10, base - 100, 1),
                     (300 + vpk % 10, base + 200, 1)]
            for off, price, avail in steps:
                observations.append((vpk, days[off], price, None, avail, 0))
            _, price, avail = steps[-1]
            variants.append((vpk, i + 1, vpk, f"{170 + j}g", 170 + j, days[-1], 0, price, None, avail))
    conn.executemany(
        "INSERT INTO listings(id, store_id, product_id, handle, title, url, first_seen, last_seen, "
        "status, manufacturer, mold, plastic, condition, disc_type, disc_key) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", listings)
    conn.executemany(
        "INSERT INTO variants(id, listing_id, variant_id, title, weight_g, last_seen, gone, "
        "price_cents, compare_at_cents, available) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(v[0], v[1], v[2], v[3], v[4], v[5], v[6], v[7], v[8], v[9]) for v in variants])
    conn.executemany(
        "INSERT INTO observations(variant_pk, observed_on, price_cents, compare_at_cents, "
        "available, gone) VALUES (?,?,?,?,?,?)", observations)
    conn.commit()
    assert len(variants) == n_variants
    return conn, days


def test_bulk_export_is_fast_and_does_not_query_per_variant(tmp_path):
    conn, days = build_big()
    statements = []
    conn.set_trace_callback(statements.append)
    start = time.perf_counter()
    stats = export.export_site(conn, tmp_path, TODAY)
    elapsed = time.perf_counter() - start
    conn.set_trace_callback(None)

    assert elapsed < 10, f"export took {elapsed:.1f}s"
    # a fixed handful of bulk reads, independent of variants x dates
    assert len([s for s in statements if s.lstrip().upper().startswith("SELECT")]) <= 12
    assert stats["discs"] == 1250 and stats["listings"] == 5000 and stats["matched"] == 5000

    index, hist = load(tmp_path)
    assert len(index["discs"]) == 1250 and len(hist) == 1250
    series = hist["innova-mold1-star"]["series"]
    assert [p["date"] for p in series["new"]] == days and series["used"] == []
    assert len(hist["innova-mold0-star"]["series"]["used"]) == 365
    assert all(p["stores_in_stock"] == 1 for p in series["new"])
    # price move at offset 50-59 shows up exactly on its date (spot check one disc)
    prices = {p["date"]: p["min"] for p in series["new"]}
    assert prices[days[0]] != prices[days[70]] and prices[days[0]] != prices[days[320]]
