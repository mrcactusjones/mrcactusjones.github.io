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
    assert set(index["stats"]) == {"listings", "matched", "review", "ignored", "unparsed", "discs", "stores"}
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


# --------------------------------------------------------------------------- sample data contract

INDEX_KEYS = {"key", "slug", "manufacturer", "mold", "plastic", "edition", "player", "disc_type", "new", "used", "last_seen"}
BLOCK_KEYS = {"min", "median", "stores_in_stock", "stores_listing", "change_7d", "change_30d"}
HISTORY_KEYS = {"key", "slug", "manufacturer", "mold", "plastic", "edition", "player", "disc_type", "series", "listings"}
POINT_KEYS = {"date", "min", "median", "stores_in_stock"}
LISTING_KEYS = {"store", "store_id", "title", "url", "condition", "weight_g", "price", "compare_at", "available", "last_seen"}
STORE_KEYS = {"id", "name", "base_url", "last_ok"}
TOP_KEYS = {"generated_at", "currency", "stores", "stats", "discs"}


def sample_index() -> dict:
    return json.loads((SAMPLE / "index.json").read_text(encoding="utf-8"))


def sample_history(slug: str) -> dict:
    return json.loads((SAMPLE / "history" / f"{slug}.json").read_text(encoding="utf-8"))


def test_sample_data_has_the_design_shape():
    index = sample_index()
    assert set(index) == TOP_KEYS
    assert all(set(s) == STORE_KEYS for s in index["stores"])
    files = {p.stem for p in (SAMPLE / "history").glob("*.json")}
    assert files == {d["slug"] for d in index["discs"]}
    for d in index["discs"]:
        assert set(d) == INDEX_KEYS, d["slug"]
        for c in ("new", "used"):
            assert d[c] is None or set(d[c]) == BLOCK_KEYS, (d["slug"], c)
        h = sample_history(d["slug"])
        assert set(h) == HISTORY_KEYS and set(h["series"]) == {"new", "used"}
        for field in ("key", "slug", "manufacturer", "mold", "plastic", "edition", "player", "disc_type"):
            assert h[field] == d[field], (d["slug"], field)
        for c in ("new", "used"):
            assert all(set(p) == POINT_KEYS for p in h["series"][c])
        assert all(set(x) == LISTING_KEYS for x in h["listings"])


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
    assert {d["mold"] for d in index["discs"]} == {"Destroyer", "Aviar", "Roc3"}
    delisted = [d for d in index["discs"] if d["new"] is None and d["used"] is None]
    assert [d["mold"] for d in delisted] == ["Aviar"], "the exporter keeps a fully delisted disc as an index entry"
    for d in index["discs"]:
        assert set(d) == INDEX_KEYS
        for c in ("new", "used"):
            assert d[c] is None or set(d[c]) == BLOCK_KEYS
        h = json.loads((exported / "history" / f"{d['slug']}.json").read_text(encoding="utf-8"))
        assert set(h) == HISTORY_KEYS
        assert all(set(p) == POINT_KEYS for c in h["series"] for p in h["series"][c])
        assert all(set(x) == LISTING_KEYS for x in h["listings"])
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
    assert len(browser_results) >= 12 and not failed, "\n".join(failed)
