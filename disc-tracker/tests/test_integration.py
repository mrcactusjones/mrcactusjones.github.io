"""End to end: fake Shopify store -> scrape -> parse -> export, through the real CLI."""
import json
from pathlib import Path

import httpx
import pytest

from disctracker import cli, shopify


def product(pid, title, price, available=True, vendor="Innova", ptype="Golf Discs", variants=None):
    return {
        "id": pid, "handle": title.lower().replace(" ", "-"), "title": title, "vendor": vendor,
        "product_type": ptype, "tags": "disc, golf",
        "variants": variants or [{"id": pid * 10, "title": "Default Title", "sku": f"S{pid}",
                                  "price": price, "compare_at_price": None, "available": available,
                                  "grams": 180}],
    }


CATALOG_DAY1 = [
    product(1, "Innova Champion Roc3", "17.99"),
    product(2, "Innova Star Destroyer", "19.99", variants=[
        {"id": 21, "title": "170-175g", "price": "19.99", "compare_at_price": None, "available": True, "grams": 180},
        {"id": 22, "title": "175g", "price": "19.99", "compare_at_price": None, "available": False, "grams": 180}]),
    product(3, "Discraft ESP Buzzz", "18.49", vendor="Discraft"),
    product(4, "Dynamic Discs Lucid Escape (Used 8/10) 172g", "12.00", vendor="Dynamic Discs"),
    product(5, "Disc Golf Backpack Pro", "149.00", vendor="Innova", ptype="Bags"),
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    state = {"catalog": CATALOG_DAY1}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        assert request.url.path == "/products.json"
        assert request.url.params["limit"] == "250"
        page = int(request.url.params["page"])
        return httpx.Response(200, json={"products": state["catalog"] if page == 1 else []})

    real = shopify.fetch_store
    monkeypatch.setattr(
        shopify, "fetch_store",
        lambda store, delay=1.0, **kw: real(store, client=httpx.Client(transport=httpx.MockTransport(handler)),
                                            delay=0))
    stores = tmp_path / "stores.json"
    stores.write_text(json.dumps({"stores": [{"id": "fake", "name": "Fake Discs",
                                               "base_url": "https://fake.example", "enabled": True}]}))
    argv = ["--db", str(tmp_path / "d.db"), "--stores", str(stores), "--out", str(tmp_path / "out")]
    return state, argv, tmp_path / "out"


def test_full_pipeline(env):
    state, argv, out = env
    assert cli.main(argv + ["run"]) == 0
    index = json.loads((out / "index.json").read_text())
    names = {(d["manufacturer"], d["mold"], d["plastic"]) for d in index["discs"]}
    assert ("Innova", "Roc3", "Champion") in names
    assert ("Innova", "Destroyer", "Star") in names
    assert ("Discraft", "Buzzz", "ESP") in names
    assert index["stats"]["ignored"] == 1  # the backpack
    destroyer = next(d for d in index["discs"] if d["mold"] == "Destroyer")
    assert destroyer["new"]["min"] == 19.99 and destroyer["new"]["stores_in_stock"] == 1
    used = [d for d in index["discs"] if d["used"]]
    assert used and used[0]["mold"] == "Escape"
    hist = json.loads((out / "history" / f"{destroyer['slug']}.json").read_text())
    assert hist["series"]["new"] and hist["listings"][0]["url"].startswith("https://fake.example/products/")


def test_price_drop_creates_second_observation(env):
    state, argv, out = env
    cli.main(argv + ["run"])
    state["catalog"] = [dict(p) for p in CATALOG_DAY1]
    state["catalog"][0] = product(1, "Innova Champion Roc3", "15.99")
    cli.main(argv + ["scrape"])
    import sqlite3
    c = sqlite3.connect(argv[1])
    n = c.execute("SELECT COUNT(*) FROM observations o JOIN variants v ON v.id=o.variant_pk "
                  "WHERE v.variant_id=10").fetchone()[0]
    # Same calendar day: the (variant, day) key means the later price replaces the earlier one.
    assert n == 1
    assert c.execute("SELECT price_cents FROM variants WHERE variant_id=10").fetchone()[0] == 1599


def test_sharp_drop_is_refused_not_recorded(env, capsys):
    state, argv, out = env
    state["catalog"] = [product(i, f"Innova Champion Roc3 {i}", "10.00") for i in range(1, 41)]
    assert cli.main(argv + ["scrape"]) == 0
    state["catalog"] = state["catalog"][:5]  # 40 -> 5 looks like a broken fetch
    assert cli.main(argv + ["scrape"]) == 1
    assert "refusing to record" in capsys.readouterr().err
    import sqlite3
    c = sqlite3.connect(argv[1])
    assert c.execute("SELECT COUNT(*) FROM listings WHERE gone=1").fetchone()[0] == 0


def test_removed_store_is_retired(env, tmp_path):
    state, argv, out = env
    cli.main(argv + ["scrape"])
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"stores": [{"id": "x", "name": "X", "base_url": "https://x.example"}]}))
    argv2 = argv[:2] + ["--stores", str(other)] + argv[4:]
    state["catalog"] = CATALOG_DAY1
    cli.main(argv2 + ["scrape"])
    import sqlite3
    c = sqlite3.connect(argv[1])
    assert c.execute("SELECT COUNT(*) FROM listings WHERE store_id='fake' AND gone=0").fetchone()[0] == 0


def test_export_and_parse_refuse_missing_db(tmp_path):
    for cmd in ("export", "parse"):
        with pytest.raises(SystemExit):
            cli.main(["--db", str(tmp_path / "nope.db"), "--out", str(tmp_path / "o"), cmd])
    assert not (tmp_path / "nope.db").exists()
