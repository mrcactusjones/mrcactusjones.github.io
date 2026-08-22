#!/usr/bin/env python3
"""gap-tracker CLI.

    python3 run.py demo                 # populate everything with fake data
    python3 run.py serve                # open the dashboard
    python3 run.py catalog              # rebuild the candidate universe (free)
    python3 run.py scan --provider ppt  # spend the day's credits
    python3 run.py rank                 # re-rank + snapshot + promote watchlist
    python3 run.py daily --provider ppt # what cron should call
    python3 run.py probe --card base1-4 # dump a raw provider response
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gapscan import rank as rank_mod
from gapscan import scan as scan_mod
from gapscan.catalog import build as build_catalog
from gapscan.config import Config, FIXTURES, ROOT, SEEDS
from gapscan.providers.mock import MockProvider
from gapscan.store import Store


def load_seeds() -> dict:
    return json.loads((SEEDS / "community.json").read_text())


def get_provider(args, cfg: Config):
    if args.provider == "mock":
        return MockProvider(drift_seed=getattr(args, "drift", "") or "")
    from gapscan.providers.ppt import PPTProvider
    return PPTProvider(credits_per_card=cfg.budget.credits_per_card)


def cmd_catalog(args, cfg: Config, store: Store) -> int:
    fixture = FIXTURES / "catalog.json" if args.fixture else None
    universe, meta = build_catalog(load_seeds(), cfg.thresholds, fixture=fixture,
                                   verify_sets=True)
    if not universe:
        print("No candidates matched. Check set ids and the raw price band.")
        return 1
    # Preserve watchlist tiers across rebuilds.
    for card_id, entry in store.load_universe().items():
        if card_id in universe and entry.get("tier"):
            universe[card_id]["tier"] = entry["tier"]
    store.save_universe(universe, meta)
    print(f"universe: {len(universe)} candidates from {meta['sets_requested']} sets "
          f"({meta['source']})")
    print(f"  filtered out: {meta['skipped']}")
    return 0


def cmd_scan(args, cfg: Config, store: Store) -> int:
    universe = store.load_universe()
    if not universe:
        print("No universe yet -- run `catalog` first.")
        return 1
    if args.budget:
        cfg.budget.daily_credits = args.budget
    scan_mod.run(universe, store, cfg, get_provider(args, cfg), dry_run=args.dry_run)
    return 0


def cmd_rank(args, cfg: Config, store: Store) -> int:
    universe = store.load_universe()
    if not universe:
        print("No universe yet -- run `catalog` first.")
        return 1
    rankings = rank_mod.build(universe, store, cfg)
    if not rankings["rows"]:
        print("Nothing priced yet -- run `scan` first.")
        return 1
    store.save_rankings(rankings)
    store.save_snapshot(rankings)
    moved = rank_mod.promote_watchlist(universe, rankings, cfg)
    store.save_universe(universe)

    cov = rankings["coverage"]
    print(f"ranked {len(rankings['rows'])} cards | coverage {cov['scanned']}/"
          f"{cov['universe']} (oldest {cov['oldest_scan_days']}d, "
          f"~{cov['days_to_full_coverage']}d to full) | watchlist changes: {moved}")
    print(f"verdicts: {rankings['verdict_counts']}")
    for row in rankings["rows"][:args.top]:
        flag = " " if row["confident"] else "?"
        print(f" {flag} ${row['floor_profit']:>8.2f} floor  "
              f"${row['upside_profit']:>8.2f} at 10  "
              f"{row['name']} ({row['set_name']} {row['number']})")
    return 0


def cmd_daily(args, cfg: Config, store: Store) -> int:
    if args.rebuild_catalog or not store.load_universe():
        if cmd_catalog(args, cfg, store):
            return 1
    if cmd_scan(args, cfg, store):
        return 1
    return cmd_rank(args, cfg, store)


def cmd_probe(args, cfg: Config, store: Store) -> int:
    universe = store.load_universe()
    card = universe.get(args.card)
    if card is None:
        print(f"{args.card} is not in the universe. Known ids look like 'base1-4'.")
        return 1
    from gapscan.providers.ppt import PPTProvider
    from gapscan.providers.ppt import extract_quote
    provider = PPTProvider(credits_per_card=cfg.budget.credits_per_card)
    blob = provider.raw_response(card)
    print(json.dumps(blob, indent=2)[:8000])
    print("\n--- what the extractor found ---")
    print(json.dumps(extract_quote(blob).__dict__, indent=2))
    return 0


def cmd_demo(args, cfg: Config, store: Store) -> int:
    """Fill the dashboard with fake but plausible data, including history."""
    args.fixture = True
    if cmd_catalog(args, cfg, store):
        return 1
    universe = store.load_universe()
    cfg.budget.daily_credits = len(universe) * cfg.budget.credits_per_card
    for day in range(args.days):
        provider = MockProvider(drift_seed=f"day{day}")
        for entry in universe.values():
            quote = provider.fetch(entry)
            store.save_quote(entry["id"], {
                "id": entry["id"], "fetched_at": store_now(), "miss": False,
                "quote": quote.__dict__, "provider": "mock"})
        rankings = rank_mod.build(universe, store, cfg)
        rank_mod.promote_watchlist(universe, rankings, cfg)
        # Backdate each pass so the history series has more than one point.
        _write_backdated_snapshot(store, rankings, args.days - day - 1)
    store.save_universe(universe)
    rankings = rank_mod.build(universe, store, cfg)
    store.save_rankings(rankings)
    print(f"demo data ready: {len(rankings['rows'])} cards, {args.days} days of history")
    print("now run:  python3 run.py serve")
    return 0


def store_now() -> str:
    from gapscan.store import iso, utcnow
    return iso(utcnow())


def _write_backdated_snapshot(store: Store, rankings: dict, days_ago: int) -> None:
    from datetime import timedelta
    from gapscan.store import utcnow, _atomic_write
    day = (utcnow() - timedelta(days=days_ago)).date().isoformat()
    slim = [{k: r[k] for k in ("id", "floor_profit", "upside_profit", "verdict",
                               "raw", "psa9", "psa10") if k in r}
            for r in rankings["rows"]]
    _atomic_write(store.history / f"{day}.json", {"date": day, "rows": slim})


def cmd_serve(args, cfg: Config, store: Store) -> int:
    import http.server
    import socketserver
    import os
    os.chdir(ROOT)
    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("127.0.0.1", args.port), handler) as httpd:
        print(f"dashboard: http://127.0.0.1:{args.port}/index.html   (ctrl-c to stop)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="run.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_provider(p):
        p.add_argument("--provider", choices=("mock", "ppt"), default="mock")
        p.add_argument("--drift", default="", help="mock only: vary the fake prices")

    p = sub.add_parser("catalog", help="rebuild the candidate universe (free)")
    p.add_argument("--fixture", action="store_true", help="use the offline fixture")
    p.set_defaults(func=cmd_catalog)

    p = sub.add_parser("scan", help="spend the day's credits on graded prices")
    add_provider(p)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--budget", type=int, help="override daily credits")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("rank", help="re-rank, snapshot, promote the watchlist")
    p.add_argument("--top", type=int, default=15)
    p.set_defaults(func=cmd_rank)

    p = sub.add_parser("daily", help="catalog (if needed) + scan + rank")
    add_provider(p)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--budget", type=int)
    p.add_argument("--top", type=int, default=15)
    p.add_argument("--rebuild-catalog", action="store_true")
    p.add_argument("--fixture", action="store_true")
    p.set_defaults(func=cmd_daily)

    p = sub.add_parser("probe", help="dump a raw provider response")
    p.add_argument("--card", required=True)
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("demo", help="populate with fake data end to end")
    p.add_argument("--days", type=int, default=21)
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("serve", help="serve the dashboard locally")
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(func=cmd_serve)

    args = parser.parse_args()
    cfg = Config.load(args.config)
    return args.func(args, cfg, Store())


if __name__ == "__main__":
    raise SystemExit(main())
