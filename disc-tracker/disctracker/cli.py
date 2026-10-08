"""Command line entry point: python -m disctracker <command>."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import db

ROOT = Path(__file__).resolve().parent.parent


def _now() -> datetime:
    return datetime.now(timezone.utc)


def load_stores(path: Path, only: str | None = None) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    stores = [s for s in data["stores"] if s.get("enabled", True)]
    if only:
        stores = [s for s in stores if s["id"] == only]
        if not stores:
            raise SystemExit(f"unknown or disabled store: {only}")
    return stores


def _open_existing(path: Path):
    """Open a database that must already exist (a typo must not create an empty one)."""
    if not Path(path).is_file():
        raise SystemExit(f"database not found: {path} (run `scrape` first)")
    return db.connect(path)


# A scrape that returns far fewer products than last time is almost certainly a broken
# fetch (a proxy capping the page size, a maintenance page), not a real delisting.
MAX_DROP = 0.5
MIN_PREVIOUS_FOR_DROP_CHECK = 20


def cmd_check_stores(args) -> int:
    from . import shopify

    bad = 0
    for s in load_stores(args.stores):
        try:
            res = shopify.check_store(s)
        except Exception as exc:
            res = {"ok": False, "detail": f"unexpected error: {exc}"}
        print(f"{'OK  ' if res['ok'] else 'FAIL'} {s['id']}: {res['detail']}")
        bad += 0 if res["ok"] else 1
    return 1 if bad else 0


def cmd_scrape(args) -> int:
    from . import parser, shopify

    stores = load_stores(args.stores, args.store)
    conn = db.connect(args.db)
    ok = 0
    if not args.store:
        # Stores removed/disabled in stores.json would otherwise stay "in stock" forever.
        today = _now().strftime("%Y-%m-%d")
        enabled = {s["id"] for s in stores}
        for sid in db.known_store_ids(conn):
            if sid not in enabled:
                db.record_products(conn, sid, today, [])
                print(f"retired {sid}: no longer enabled, listings marked gone")
    for s in stores:
        db.upsert_store(conn, s)
        now = _now()
        run_id = db.start_run(conn, s["id"], now.strftime("%Y-%m-%d"), now.isoformat(timespec="seconds"))
        try:
            products = shopify.fetch_store(s, delay=args.delay)
            if not products:  # an empty catalogue would mark every listing gone
                raise shopify.StoreError("store returned zero products; refusing to record")
            prev = db.last_ok_count(conn, s["id"])
            if (prev is not None and prev >= MIN_PREVIOUS_FOR_DROP_CHECK
                    and len(products) < prev * (1 - MAX_DROP)):
                raise shopify.StoreError(
                    f"product count fell from {prev} to {len(products)}; refusing to record "
                    "(looks like an incomplete fetch)")
            stats = db.record_products(conn, s["id"], now.strftime("%Y-%m-%d"), products,
                                       weight_parser=parser.parse_weight)
            db.finish_run(conn, run_id, _now().isoformat(timespec="seconds"), "ok", len(products))
            print(f"ok   {s['id']}: {stats}")
            ok += 1
        except Exception as exc:  # one bad store must not stop the rest
            db.finish_run(conn, run_id, _now().isoformat(timespec="seconds"), "error", 0, str(exc)[:500])
            print(f"FAIL {s['id']}: {exc}", file=sys.stderr)
    return 0 if ok else 1


def cmd_parse(args) -> int:
    from . import parser

    conn = _open_existing(args.db) if args.cmd == "parse" else db.connect(args.db)
    rows = db.listings_needing_parse(conn, parser.PARSER_VERSION, force=args.all)
    counts: dict[str, int] = {}
    with conn:
        for r in rows:
            try:
                p = parser.parse_listing(r["title"], r["vendor"] or "", r["product_type"] or "",
                                         json.loads(r["tags"] or "[]"))
            except Exception as exc:  # one bad title must not block the rest
                print(f"parse failed for listing {r['id']}: {exc}", file=sys.stderr)
                continue
            db.save_parse(conn, r["id"], p, parser.PARSER_VERSION)
            counts[p.status] = counts.get(p.status, 0) + 1
    print(f"parsed {sum(counts.values())}/{len(rows)} listings: {counts}")
    return 0


def cmd_export(args) -> int:
    from . import export

    conn = _open_existing(args.db) if args.cmd == "export" else db.connect(args.db)
    stats = export.export_site(conn, args.out, _now().strftime("%Y-%m-%d"))
    print(f"exported: {stats}")
    return 0


def cmd_run(args) -> int:
    rc = cmd_scrape(args)
    cmd_parse(args)
    cmd_export(args)
    return rc


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="disctracker")
    ap.add_argument("--db", type=Path, default=ROOT / "data" / "discs.db")
    ap.add_argument("--stores", type=Path, default=ROOT / "stores.json")
    ap.add_argument("--out", type=Path, default=ROOT / "site" / "data")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check-stores").set_defaults(fn=cmd_check_stores)
    for name, fn in (("scrape", cmd_scrape), ("run", cmd_run)):
        sp = sub.add_parser(name)
        sp.add_argument("--store")
        sp.add_argument("--delay", type=float, default=1.0)
        sp.add_argument("--all", action="store_true", help="(run) re-parse every listing")
        sp.set_defaults(fn=fn)
    sp = sub.add_parser("parse")
    sp.add_argument("--all", action="store_true")
    sp.set_defaults(fn=cmd_parse)
    sub.add_parser("export").set_defaults(fn=cmd_export)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
