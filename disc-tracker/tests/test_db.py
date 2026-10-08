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
