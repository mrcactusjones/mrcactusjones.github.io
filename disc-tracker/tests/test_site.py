"""Tests for the static dashboard in site/ (DESIGN.md section 6).

Three layers:
* static source checks (untrusted strings never reach the DOM as HTML, CSP, pinned CDN, relative URLs);
* a contract check of the sample data the site is tested with, compared with what the real exporter writes;
* browser checks (tests/site_checks.js, Playwright + Chromium). They are skipped when node, playwright-core or a
  Chromium is missing, so CI without a browser still runs everything else. To run them by hand:
      node tests/site_checks.js
  (set PLAYWRIGHT_MODULE / CHROMIUM_PATH if they are not found by themselves).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest

from disctracker import db, export
from disctracker.models import ParsedListing, RawProduct, RawVariant, key_slug

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
SITE = ROOT / "site"
SAMPLE = TESTS / "fixtures" / "site_sample"
TODAY = "2026-10-08"

APP = (SITE / "app.js").read_text(encoding="utf-8")
HTML = (SITE / "index.html").read_text(encoding="utf-8")


def code_only(js: str) -> str:
    """app.js without block comments and whole-line // comments (they may mention what the code must not do)."""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return "\n".join(line for line in js.splitlines() if not line.lstrip().startswith("//"))


# --------------------------------------------------------------------------- static source checks

def test_app_never_turns_data_into_html():
    code = code_only(APP)
    for needle in (r"\.innerHTML", r"\.outerHTML", r"insertAdjacentHTML", r"document\.write", r"\beval\s*\(",
                   r"new\s+Function", r"\.srcdoc\s*=", r"setTimeout\s*\(\s*[\"'`]", r"DOMParser", r"createContextualFragment",
                   r"\.setAttribute\(\s*[\"']on", r"\.setAttribute\(\s*[\"']style"):
        assert not re.search(needle, code), needle


def test_index_html_has_no_inline_code_and_a_strict_csp():
    assert not re.search(r"\son[a-z]+\s*=", HTML, re.I), "inline event handler"
    assert not re.search(r"\sstyle\s*=", HTML, re.I), "inline style attribute"
    assert "javascript:" not in HTML.lower()
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", HTML, re.S | re.I)
    assert scripts and all("src=" in attrs and not body.strip() for attrs, body in scripts), "inline <script>"
    csp = re.search(r'http-equiv="Content-Security-Policy"\s+content="([^"]+)"', HTML).group(1)
    assert "default-src 'none'" in csp and "base-uri 'none'" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert re.search(r"script-src 'self' https://cdnjs\.cloudflare\.com(;|$)", csp)


def test_chart_js_comes_from_one_pinned_cdnjs_url():
    urls = re.findall(r"https://[^\"'\s)]+", code_only(APP))
    assert urls == ["https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"]


def test_data_is_fetched_with_relative_urls():
    paths = re.findall(r"getJSON\(\s*([\"'])(.*?)\1", APP)
    assert ("\"", "data/index.json") in paths
    assert all(not p.startswith("/") and "://" not in p for _, p in paths)
    assert 'getJSON("data/history/"' in APP


def test_committed_data_is_an_empty_state_in_the_exporter_shape():
    index = json.loads((SITE / "data" / "index.json").read_text(encoding="utf-8"))
    assert set(index) == {"generated_at", "currency", "stores", "stats", "discs"}
    assert index["discs"] == [] and index["stores"] == []
    # written by an older exporter until the next scheduled run: the sales totals are optional here
    base = {"listings", "matched", "review", "ignored", "unparsed", "discs", "stores"}
    assert base <= set(index["stats"]) <= base | {"sales_confirmed", "sales_inferred"}
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", index["generated_at"])


def test_slug_pattern_accepts_every_slug_the_exporter_makes():
    pattern = re.search(r"const SLUG_RE = /(.+)/;", APP).group(1)
    keys = [
        "innova|destroyer|star||", "innova|roc3|champion|tour series|ricky wysocki", "kastaplast|järn|k1||",
        "discraft|buzzz|esp||", 'innova|<img src=x onerror=alert(1)>|"><script>|tour series|<svg onload=1>',
        "a|" + "very long mold name " * 30 + "|plastic||", "|||", "x|日本語||", "mvp|ion|neutron|first run|",
    ]
    for key in keys:
        slug = key_slug(key)[:100].rstrip("-") or "disc"
        for candidate in (slug, slug + "-2"):
            assert re.fullmatch(pattern, candidate), (key, candidate)
    assert not re.fullmatch(pattern, "../../etc/passwd") and not re.fullmatch(pattern, "a b") and not re.fullmatch(pattern, "")


def test_inferred_sales_wording_is_the_contract_text():
    # DESIGN.md 10.4: an inferred row reads exactly like this and a confirmed one never shares it
    assert 'const INFERRED_LABEL = "Likely sold (inferred, low confidence - the seller may have delisted it)";' in APP
    assert 'const CONFIRMED_LABEL = "Sold (confirmed)";' in APP
    # a sale is confirmed only when the data says so, and a "disappeared" source can never be one
    assert 'confirmed: x.confidence === "confirmed" && x.source !== INFERRED_SOURCE' in APP
    assert len(re.findall(r"\bINFERRED_LABEL\b", code_only(APP))) == 2   # the definition and its one use


# --------------------------------------------------------------------------- sample data contract

INDEX_KEYS = {"key", "slug", "manufacturer", "mold", "plastic", "edition", "player", "disc_type", "new", "used",
              "sales_30d", "last_seen"}
INDEX_KEYS_WITH_MARKETPLACE = INDEX_KEYS | {"retail"}  # `retail`: the blocks of the retail stores alone (see export.py)
BLOCK_KEYS = {"min", "median", "stores_in_stock", "stores_listing", "change_7d", "change_30d"}
HISTORY_KEYS = {"key", "slug", "manufacturer", "mold", "plastic", "edition", "player", "disc_type", "series",
                "listings", "sales"}
HISTORY_KEYS_WITH_MARKETPLACE = HISTORY_KEYS | {"series_by_kind"}
POINT_KEYS = {"date", "min", "median", "stores_in_stock"}
LISTING_KEYS = {"store", "store_id", "store_kind", "title", "url", "condition", "weight_g", "price", "compare_at",
                "available", "last_seen"}
SALE_KEYS = {"date", "price", "condition", "source", "confidence", "store", "url"}
STORE_KEYS = {"id", "name", "base_url", "kind", "last_ok"}
STATS_KEYS = {"listings", "matched", "review", "ignored", "unparsed", "discs", "stores", "sales_confirmed",
              "sales_inferred"}
TOP_KEYS = {"generated_at", "currency", "stores", "stats", "discs"}
SALES_DAYS = 30


def sample_index() -> dict:
    return json.loads((SAMPLE / "index.json").read_text(encoding="utf-8"))


def sample_history(slug: str) -> dict:
    return json.loads((SAMPLE / "history" / f"{slug}.json").read_text(encoding="utf-8"))


def test_sample_data_has_the_design_shape():
    index = sample_index()
    assert set(index) == TOP_KEYS and set(index["stats"]) == STATS_KEYS
    assert all(set(s) == STORE_KEYS for s in index["stores"])
    files = {p.stem for p in (SAMPLE / "history").glob("*.json")}
    assert files == {d["slug"] for d in index["discs"]}
    for d in index["discs"]:
        h = sample_history(d["slug"])
        marketplace = "series_by_kind" in h
        assert set(d) == (INDEX_KEYS_WITH_MARKETPLACE if marketplace else INDEX_KEYS), d["slug"]
        assert set(h) == (HISTORY_KEYS_WITH_MARKETPLACE if marketplace else HISTORY_KEYS), d["slug"]
        for c in ("new", "used"):
            assert d[c] is None or set(d[c]) == BLOCK_KEYS, (d["slug"], c)
            if marketplace:
                assert d["retail"][c] is None or set(d["retail"][c]) == BLOCK_KEYS, (d["slug"], c)
        assert set(h["series"]) == {"new", "used"}
        for field in ("key", "slug", "manufacturer", "mold", "plastic", "edition", "player", "disc_type"):
            assert h[field] == d[field], (d["slug"], field)
        for c in ("new", "used"):
            assert all(set(p) == POINT_KEYS for p in h["series"][c])
        assert all(set(x) == LISTING_KEYS for x in h["listings"])
        assert all(set(x) == SALE_KEYS for x in h["sales"])
        if marketplace:
            assert set(h["series_by_kind"]) == {"retail", "marketplace"}
            for kind in h["series_by_kind"].values():
                assert set(kind) == {"new", "used"}
                assert all(set(p) == POINT_KEYS for c in kind for p in kind[c])


def test_sample_data_is_internally_consistent():
    index = sample_index()
    today = date.fromisoformat(index["generated_at"])
    store_ids = {s["id"] for s in index["stores"]}
    names = [tuple(d[k].casefold() for k in ("manufacturer", "mold", "plastic", "edition", "player")) for d in index["discs"]]
    assert names == sorted(names), "discs are sorted by manufacturer, mold, plastic"
    assert len({d["slug"] for d in index["discs"]}) == len(index["discs"])
    for d in index["discs"]:
        h = sample_history(d["slug"])
        prices = [x["price"] for x in h["listings"]]
        assert prices == sorted(prices), "listings are cheapest first"
        assert all(x["store_id"] in store_ids for x in h["listings"])
        for c in ("new", "used"):
            block, series = d[c], h["series"][c]
            dates = [p["date"] for p in series]
            assert dates == sorted(set(dates)) and all(x <= index["generated_at"] for x in dates)
            assert all(0 < p["min"] <= p["median"] and p["stores_in_stock"] >= 1 for p in series)
            listings = [x for x in h["listings"] if x["condition"] == c]
            assert bool(block) == bool(listings), (d["slug"], c, "a block exists exactly when there are live listings")
            if not block:
                continue
            in_stock = [x for x in listings if x["available"]]
            assert block["stores_in_stock"] == len({x["store_id"] for x in in_stock}), (d["slug"], c)
            assert block["stores_listing"] == len({x["store_id"] for x in listings}), (d["slug"], c)
            assert block["min"] == min(x["price"] for x in (in_stock or listings)), (d["slug"], c)
            assert block["min"] <= block["median"]
            for days in (7, 30):
                prior = [p for p in series if date.fromisoformat(p["date"]) <= today - timedelta(days=days)]
                expected = None
                if prior and in_stock:
                    expected = round((block["min"] - prior[-1]["min"]) / prior[-1]["min"], 4) or 0.0
                assert block[f"change_{days}d"] == expected, (d["slug"], c, days)
    assert any(not d["new"] and not d["used"] for d in index["discs"]), "the sample needs a fully delisted disc"
    assert any(d["new"] and not d["used"] for d in index["discs"]) and any(d["used"] and not d["new"] for d in index["discs"])


def test_sample_marketplace_data_is_consistent():
    index = sample_index()
    today = date.fromisoformat(index["generated_at"])
    kinds = {s["id"]: s["kind"] for s in index["stores"]}
    assert set(kinds.values()) == {"retail", "marketplace"}
    names = {s["name"] for s in index["stores"]}
    confirmed = inferred = 0
    for d in index["discs"]:
        h = sample_history(d["slug"])
        assert all(x["store_kind"] == kinds[x["store_id"]] for x in h["listings"]), d["slug"]
        sales = h["sales"]
        assert len(sales) <= 100 and [x["date"] for x in sales] == sorted((x["date"] for x in sales), reverse=True)
        for x in sales:
            assert x["store"] in names and x["price"] > 0 and x["condition"] in ("new", "used")
            if x["confidence"] == "confirmed":
                assert x["source"] != "inferred_disappeared"
            else:  # the one thing the page must be able to rely on: an unconfirmed sale says `low`
                assert x["confidence"] == "low"
        confirmed += sum(x["confidence"] == "confirmed" for x in sales)
        inferred += sum(x["confidence"] != "confirmed" for x in sales)
        # sales_30d is derived from the sales rows: sale dates from today - 30 days to today
        window = [x for x in sales if today - timedelta(days=SALES_DAYS) <= date.fromisoformat(x["date"]) <= today]
        for label, rows in (("confirmed", [x for x in window if x["confidence"] == "confirmed"]),
                            ("inferred", [x for x in window if x["confidence"] != "confirmed"])):
            got = d["sales_30d"][label]
            if not rows:
                assert got is None, (d["slug"], label)
                continue
            prices = sorted(round(x["price"] * 100) for x in rows)
            mid = len(prices) // 2
            median = prices[mid] if len(prices) % 2 else (prices[mid - 1] + prices[mid] + 1) // 2
            assert got == {"count": len(rows), "median": median / 100}, (d["slug"], label)
        if "series_by_kind" not in h:
            assert not any(x["store_kind"] == "marketplace" for x in h["listings"]) and not sales
            continue
        by = h["series_by_kind"]
        for c in ("new", "used"):
            # blended point = the two kinds together: same minimum, store counts add up
            r = {p["date"]: p for p in by["retail"][c]}
            m = {p["date"]: p for p in by["marketplace"][c]}
            for p in h["series"][c]:
                parts = [q[p["date"]] for q in (r, m) if p["date"] in q]
                assert parts, (d["slug"], c, p["date"])
                if len(parts) == 2:  # (a single kind is the blended point itself)
                    assert p["min"] == min(q["min"] for q in parts), (d["slug"], c, p["date"])
                    assert p["stores_in_stock"] == sum(q["stores_in_stock"] for q in parts)
                else:
                    assert p == parts[0], (d["slug"], c, p["date"])
            assert {p["date"] for p in h["series"][c]} == set(r) | set(m)
            # the retail block is the retail listings alone
            retail = [x for x in h["listings"] if x["condition"] == c and x["store_kind"] == "retail"]
            block = d["retail"][c]
            assert bool(block) == bool(retail), (d["slug"], c)
            if block:
                in_stock = [x for x in retail if x["available"]]
                assert block["min"] == min(x["price"] for x in (in_stock or retail))
                assert block["stores_in_stock"] == len({x["store_id"] for x in in_stock})
    assert (confirmed, inferred) == (index["stats"]["sales_confirmed"], index["stats"]["sales_inferred"])


def test_sample_covers_the_marketplace_cases_the_browser_checks_rely_on():
    index = sample_index()
    discs = {d["slug"]: d for d in index["discs"]}
    hist = {slug: sample_history(slug) for slug in discs}
    mixed = [s for s, h in hist.items() if "series_by_kind" in h]
    assert len(mixed) >= 4
    all_sales = [(s, x) for s, h in hist.items() for x in h["sales"]]
    assert any(x["confidence"] == "confirmed" for _, x in all_sales), "a confirmed sale"
    assert any("<" in x["store"] and x["confidence"] == "low" for _, x in all_sales), "a hostile store name in a sales row"
    assert any(x["url"] is None for _, x in all_sales), "a sale without a usable URL"
    today = date.fromisoformat(index["generated_at"])
    assert any(today - date.fromisoformat(x["date"]) > timedelta(days=SALES_DAYS) for _, x in all_sales), "an old sale"
    only_ebay = [s for s in mixed if not any(x["store_kind"] == "retail" for x in hist[s]["listings"])
                 and not hist[s]["series_by_kind"]["retail"]["new"] and not hist[s]["series_by_kind"]["retail"]["used"]]
    assert any(hist[s]["sales"] and all(x["confidence"] == "low" for x in hist[s]["sales"]) for s in only_ebay), \
        "an eBay-only disc whose sales are all inferred"
    assert any(not hist[s]["sales"] for s in mixed), "a marketplace disc without sales"
    assert any(discs[s]["new"] is None and discs[s]["used"] is None and hist[s]["sales"] for s in mixed), \
        "a marketplace disc that is gone but has a sale"
    assert any(d["retail"]["new"] is None and d["retail"]["used"] is None for d in discs.values() if "retail" in d)
    assert any(d["new"] and d["retail"] and d["retail"]["new"] and d["new"]["min"] < d["retail"]["new"]["min"]
               for d in discs.values() if "retail" in d), "a disc whose lowest price is a marketplace price"


# --------------------------------------------------------------------------- real exporter output

def _prod(pid: int, title: str, price: int, available: bool = True) -> RawProduct:
    return RawProduct(pid, f"h{pid}", title, vendor="Innova", url=f"https://shop.example/products/h{pid}",
                      variants=[RawVariant(pid * 10, "175g", price_cents=price, available=available)])


PARSES = {  # product id -> what the parser would say (the parser's lower-case edition vocabulary included)
    1: dict(mold="Destroyer", plastic="Star", disc_type="Distance Driver"),
    2: dict(mold="Aviar", plastic="DX", disc_type="Putter"),
    3: dict(mold="Roc3", plastic="Champion", disc_type="Midrange", condition="used"),
    4: dict(mold="Destroyer", plastic="Star", disc_type="Distance Driver", edition="tour series", player="Ricky Wysocki"),
    5: dict(status="review", mold="Rare", plastic=""),
    # eBay items (legacy item ids): a cheap Destroyer that vanished (an inferred sale), an eBay-only Wraith
    9001: dict(mold="Destroyer", plastic="Star", disc_type="Distance Driver"),
    9002: dict(mold="Wraith", plastic="Star", disc_type="Distance Driver"),
}


@pytest.fixture(scope="module")
def exported(tmp_path_factory) -> Path:
    """Output of the real exporter for a small world: a live disc, a disc every store delisted, a used-only disc, a
    lower-case edition and a review listing."""
    conn = db.connect(":memory:")
    for sid in ("alpha", "beta"):
        db.upsert_store(conn, {"id": sid, "name": f"{sid.title()} Discs", "base_url": f"https://{sid}.example", "currency": "USD"})
    for n in range(20, -1, -1):
        on = (date.fromisoformat(TODAY) - timedelta(days=n)).isoformat()
        for sid, shift in (("alpha", 0), ("beta", 100)):
            items = [_prod(1, "Innova Star Destroyer", (1799 if n > 10 else 1699) + shift),
                     _prod(3, "[Used] Innova Champion Roc3 9/10", 999 + shift),
                     _prod(4, "Innova Star Destroyer Tour Series Ricky Wysocki", 2499),
                     _prod(5, "Innova Rare Thing", 500)]
            if n > 15:
                items.append(_prod(2, "Innova DX Aviar", 800 + shift))   # delisted everywhere 15 days ago
            run = db.start_run(conn, sid, on, on + "T06:00:00+00:00")
            db.record_products(conn, sid, on, items)
            db.finish_run(conn, run, on + "T06:05:00+00:00", "ok", len(items), None)
    db.upsert_store(conn, {"id": "ebay", "name": "eBay", "base_url": "https://www.ebay.com", "currency": "USD",
                           "kind": "marketplace"})
    for n in range(20, -1, -1):
        on = (date.fromisoformat(TODAY) - timedelta(days=n)).isoformat()
        items = []
        for pid, cents, first, last in ((9001, 1499, 20, 8), (9002, 2100, 12, 0)):
            if last <= n <= first:
                items.append(RawProduct(pid, "", f"NEW Innova Star {PARSES[pid]['mold']} 175g Max Distance!!",
                                        product_type="New", tags=["condition:new"], url=f"https://www.ebay.com/itm/{pid}",
                                        variants=[RawVariant(pid, "", price_cents=cents, available=True)],
                                        ends_at="2030-01-01T00:00:00.000Z", query_key="innova|destroyer|star"))
        run = db.start_run(conn, "ebay", on, on + "T05:00:00+00:00")
        db.record_products(conn, "ebay", on, items, complete=False)
        db.record_query_run(conn, "innova|destroyer|star", "star destroyer innova", on, True, len(items))
        db.expire_missing(conn, "ebay", "innova|destroyer|star", on)  # 9001 vanishes on day 7: an inferred sale
        db.finish_run(conn, run, on + "T05:05:00+00:00", "ok", len(items), None)
    for (lid, pid) in conn.execute("SELECT id, product_id FROM listings").fetchall():
        spec = {"status": "matched", "manufacturer": "Innova", "condition": "new", "confidence": 0.9, **PARSES[pid]}
        db.save_parse(conn, lid, ParsedListing(**spec), 1)
    conn.commit()
    out = tmp_path_factory.mktemp("export") / "data"
    out.mkdir()
    export.export_site(conn, out, TODAY)
    return out


def test_exporter_output_has_the_shape_the_sample_documents(exported):
    index = json.loads((exported / "index.json").read_text(encoding="utf-8"))
    assert set(index) == TOP_KEYS and all(set(s) == STORE_KEYS for s in index["stores"])
    assert set(index["stats"]) == STATS_KEYS
    assert {s["id"]: s["kind"] for s in index["stores"]} == {"alpha": "retail", "beta": "retail", "ebay": "marketplace"}
    assert {d["mold"] for d in index["discs"]} == {"Destroyer", "Aviar", "Roc3", "Wraith"}
    delisted = [d for d in index["discs"] if d["new"] is None and d["used"] is None]
    assert [d["mold"] for d in delisted] == ["Aviar"], "the exporter keeps a fully delisted disc as an index entry"
    assert (index["stats"]["sales_confirmed"], index["stats"]["sales_inferred"]) == (0, 1)
    for d in index["discs"]:
        h = json.loads((exported / "history" / f"{d['slug']}.json").read_text(encoding="utf-8"))
        marketplace = d["mold"] in ("Destroyer", "Wraith") and not d["edition"]
        assert (set(d), set(h)) == ((INDEX_KEYS_WITH_MARKETPLACE, HISTORY_KEYS_WITH_MARKETPLACE) if marketplace
                                    else (INDEX_KEYS, HISTORY_KEYS)), d["slug"]
        for c in ("new", "used"):
            assert d[c] is None or set(d[c]) == BLOCK_KEYS
        assert all(set(p) == POINT_KEYS for c in h["series"] for p in h["series"][c])
        assert all(set(x) == LISTING_KEYS for x in h["listings"])
        assert all(set(x) == SALE_KEYS for x in h["sales"])
    assert any(d["edition"] == "tour series" for d in index["discs"]), "editions come out lower case"


# --------------------------------------------------------------------------- browser checks

@pytest.fixture(scope="module")
def browser_results(exported, tmp_path_factory):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    env = {**os.environ, "REAL_DIR": str(exported), "WORK_DIR": str(tmp_path_factory.mktemp("site-checks"))}
    proc = subprocess.run([node, str(TESTS / "site_checks.js")], capture_output=True, text=True, timeout=900, env=env, cwd=ROOT)
    if proc.returncode == 3:
        pytest.skip(proc.stdout.strip() or "browser checks cannot run here")
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")][-1]
    return json.loads(line)["results"]


def test_browser_checks(browser_results):
    failed = [f"{r['name']}\n    {r['detail']}" for r in browser_results if not r["ok"]]
    assert len(browser_results) >= 20 and not failed, "\n".join(failed)
