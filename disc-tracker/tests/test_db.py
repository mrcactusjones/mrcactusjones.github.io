from disctracker import db
from disctracker.models import ParsedListing, RawProduct, RawVariant, disc_key, key_slug


def make_conn():
    conn = db.connect(":memory:")
    db.upsert_store(conn, {"id": "s", "name": "S", "base_url": "https://example.com/"})
    return conn


def prod(pid=1, price=1000, avail=True, title="Innova Star Destroyer", variants=1):
    return RawProduct(pid, f"h{pid}", title,
                      variants=[RawVariant(100 * pid + i, f"v{i}", price_cents=price, available=avail)
                                for i in range(variants)])


def obs(conn):
    return [tuple(r) for r in conn.execute(
        "SELECT observed_on, price_cents, available, gone FROM observations ORDER BY variant_pk, observed_on")]


def test_change_only_observations():
    c = make_conn()
    db.record_products(c, "s", "2026-01-01", [prod()])
    db.record_products(c, "s", "2026-01-02", [prod()])  # unchanged: no row
    db.record_products(c, "s", "2026-01-03", [prod(price=900)])
    assert obs(c) == [("2026-01-01", 1000, 1, 0), ("2026-01-03", 900, 1, 0)]


def test_gone_and_return():
    c = make_conn()
    db.record_products(c, "s", "2026-01-01", [prod(1), prod(2)])
    stats = db.record_products(c, "s", "2026-01-02", [prod(1)])
    assert stats["gone"] == 1
    assert c.execute("SELECT gone FROM listings WHERE product_id=2").fetchone()[0] == 1
    db.record_products(c, "s", "2026-01-03", [prod(1), prod(2)])
    assert c.execute("SELECT gone FROM listings WHERE product_id=2").fetchone()[0] == 0
    assert c.execute("SELECT gone FROM variants WHERE variant_id=200").fetchone()[0] == 0
    last = [r for r in obs(c)][-1]
    assert last == ("2026-01-03", 1000, 1, 0)


def test_title_change_invalidates_parse():
    c = make_conn()
    db.record_products(c, "s", "2026-01-01", [prod(title="A")])
    lid = c.execute("SELECT id FROM listings").fetchone()[0]
    db.save_parse(c, lid, ParsedListing(status="matched", manufacturer="Innova", mold="Destroyer"), 1)
    db.record_products(c, "s", "2026-01-02", [prod(title="A")])
    assert c.execute("SELECT parse_version FROM listings").fetchone()[0] == 1
    db.record_products(c, "s", "2026-01-03", [prod(title="B")])
    assert c.execute("SELECT parse_version FROM listings").fetchone()[0] == 0


def test_stores_are_isolated():
    c = make_conn()
    db.upsert_store(c, {"id": "t", "name": "T", "base_url": "https://t.example"})
    db.record_products(c, "s", "2026-01-01", [prod(1)])
    db.record_products(c, "t", "2026-01-01", [prod(2)])
    db.record_products(c, "s", "2026-01-02", [prod(1)])
    assert c.execute("SELECT gone FROM listings WHERE store_id='t'").fetchone()[0] == 0


def test_many_products_no_sqlite_variable_limit():
    c = make_conn()
    db.record_products(c, "s", "2026-01-01", [prod(i) for i in range(1, 3000)])
    db.record_products(c, "s", "2026-01-02", [prod(i) for i in range(1, 2990)])
    assert c.execute("SELECT COUNT(*) FROM listings WHERE gone=1").fetchone()[0] == 10


def test_disc_key_and_slug():
    p = ParsedListing(status="matched", manufacturer="Innova", mold="Destroyer", plastic="Star")
    assert disc_key(p) == "innova|destroyer|star||"
    assert key_slug("innova|destroyer|star||") == "innova-destroyer-star"
    assert disc_key(ParsedListing(status="review", manufacturer="Innova", mold="X")) is None


# --- marketplace (eBay) support ---------------------------------------------------------

def ebay_conn():
    c = db.connect(":memory:")
    db.upsert_store(c, {"id": "ebay", "name": "eBay", "base_url": "https://www.ebay.com", "kind": "marketplace"})
    return c


def item(pid, price=2500, query="q1", ends_at=""):
    p = prod(pid, price=price, title=f"Innova Star Destroyer {pid}")
    p.query_key, p.ends_at = query, ends_at
    return p


def test_incomplete_scrape_does_not_mark_gone():
    c = ebay_conn()
    db.record_products(c, "ebay", "2026-01-01", [item(1), item(2)], complete=False)
    db.record_products(c, "ebay", "2026-01-02", [item(1)], complete=False)
    assert c.execute("SELECT COUNT(*) FROM listings WHERE gone=1").fetchone()[0] == 0


def test_expire_missing_infers_low_confidence_sale():
    c = ebay_conn()
    db.record_products(c, "ebay", "2026-01-01", [item(1, 2500), item(2, 3000)], complete=False)
    db.record_products(c, "ebay", "2026-01-02", [item(1, 2500)], complete=False)
    stats = db.expire_missing(c, "ebay", "q1", "2026-01-02")
    assert stats == {"gone": 1, "inferred_sales": 1}
    sale = c.execute("SELECT sold_on, price_cents, source, confidence FROM sales").fetchone()
    assert tuple(sale) == ("2026-01-01", 3000, "inferred_disappeared", "low")
    assert c.execute("SELECT gone FROM listings WHERE product_id=2").fetchone()[0] == 1
    assert c.execute("SELECT gone FROM listings WHERE product_id=1").fetchone()[0] == 0
    last = c.execute("SELECT o.available, o.gone FROM observations o JOIN variants v ON v.id=o.variant_pk "
                     "WHERE v.variant_id=200 ORDER BY observed_on DESC").fetchone()
    assert tuple(last) == (0, 1)


def test_expire_ignores_other_queries_and_same_day_sightings():
    c = ebay_conn()
    db.record_products(c, "ebay", "2026-01-01", [item(1, query="q1"), item(2, query="q2")], complete=False)
    assert db.expire_missing(c, "ebay", "q1", "2026-01-01")["gone"] == 0  # seen today: not missing
    db.record_products(c, "ebay", "2026-01-02", [], complete=False)
    assert db.expire_missing(c, "ebay", "q1", "2026-01-02")["gone"] == 1  # only q1's listing
    assert c.execute("SELECT gone FROM listings WHERE product_id=2").fetchone()[0] == 0


def test_ended_listing_is_not_a_sale():
    c = ebay_conn()
    db.record_products(c, "ebay", "2026-01-01", [item(1, ends_at="2026-01-02T10:00:00.000Z")], complete=False)
    db.record_products(c, "ebay", "2026-01-03", [], complete=False)
    assert db.expire_missing(c, "ebay", "q1", "2026-01-03") == {"gone": 1, "inferred_sales": 0}
    assert c.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 0


def test_relisted_item_cancels_inferred_sale():
    c = ebay_conn()
    db.record_products(c, "ebay", "2026-01-01", [item(1)], complete=False)
    db.record_products(c, "ebay", "2026-01-02", [], complete=False)
    db.expire_missing(c, "ebay", "q1", "2026-01-02")
    assert c.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 1
    db.record_products(c, "ebay", "2026-01-03", [item(1)], complete=False)
    assert c.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 0
    assert c.execute("SELECT gone FROM listings").fetchone()[0] == 0


def test_query_rotation_orders_never_run_then_stalest():
    c = ebay_conn()
    qs = [("a", "A"), ("b", "B"), ("c", "C")]
    db.record_query_run(c, "a", "A", "2026-01-05", True, 3)
    db.record_query_run(c, "b", "B", "2026-01-02", True, 3)
    assert [k for k, _ in db.order_queries(c, qs)] == ["c", "b", "a"]


def test_migrates_old_database(tmp_path):
    path = tmp_path / "old.db"
    c = db.connect(path)
    for table, col in (("stores", "kind"), ("listings", "ends_at"), ("listings", "last_query")):
        c.execute(f"ALTER TABLE {table} DROP COLUMN {col}")  # simulate the first release's schema
    c.commit()
    c.close()
    c = db.connect(path)
    assert "kind" in {r["name"] for r in c.execute("PRAGMA table_info(stores)")}
    assert {"ends_at", "last_query"} <= {r["name"] for r in c.execute("PRAGMA table_info(listings)")}
