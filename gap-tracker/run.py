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
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gapscan import rank as rank_mod
from gapscan import scan as scan_mod
from gapscan.catalog import build as build_catalog
from gapscan.config import Config, FIXTURES, ROOT, SEEDS
from gapscan.providers.mock import MockProvider
from gapscan.store import Store


class _Tee:
    """Mirror writes to the console and a log file."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, text):
        for stream in self.streams:
            stream.write(text)
        return len(text)

    def flush(self):
        for stream in self.streams:
            stream.flush()


def start_logging(root: Path):
    """Tee stdout/stderr into data/logs/YYYY-MM-DD.log.

    Kept in Python rather than the shell wrappers so date formatting and
    directory creation work identically on Windows, macOS and Linux.
    """
    from datetime import datetime, timezone
    log_dir = root / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc)
    handle = open(log_dir / f"{stamp.date().isoformat()}.log", "a", encoding="utf-8")
    handle.write(f"\n=== {stamp.replace(microsecond=0).isoformat()} ===\n")
    sys.stdout = _Tee(sys.__stdout__, handle)
    sys.stderr = _Tee(sys.__stderr__, handle)
    return handle


def load_env_file(path: Path) -> None:
    """Read KEY=VALUE lines from a local .env, without overriding the real
    environment. Keeps the API key out of shell history and out of git."""
    if not path.exists():
        return
    # utf-8-sig: PowerShell and Notepad can prepend a BOM, which would
    # otherwise become part of the first key name.
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_seeds() -> dict:
    return json.loads((SEEDS / "community.json").read_text())


def get_provider(args, cfg: Config):
    if args.provider == "mock":
        return MockProvider(drift_seed=getattr(args, "drift", "") or "")
    from gapscan.providers.ppt import PPTProvider
    return PPTProvider(credits_per_card=cfg.budget.credits_per_card)


def cmd_catalog(args, cfg: Config, store: Store) -> int:
    fixture = FIXTURES / "catalog.json" if args.fixture else None
    only = set(args.sets.split(",")) if getattr(args, "sets", None) else None
    if fixture is None:
        keyed = "yes" if os.environ.get("POKEMONTCG_API_KEY") else "no (slower, expect 500s)"
        print(f"pokemontcg.io key: {keyed}")
    universe, meta = build_catalog(load_seeds(), cfg.thresholds, fixture=fixture,
                                   verify_sets=True, only=only)
    if not universe:
        print("No candidates matched. Check set ids and the raw price band.")
        return 1

    source = "fixture" if fixture else "api"
    existing = store.load_universe()
    # Never mix demo cards into a live universe, or vice versa.
    same_source = {k: v for k, v in existing.items() if v.get("source", "api") == source}
    # A partial build (--sets, or sets that failed) must leave the rest intact:
    # replace only the sets actually rebuilt this run.
    rebuilt = {v["set_id"] for v in universe.values()}
    merged = {k: v for k, v in same_source.items() if v.get("set_id") not in rebuilt}
    merged.update(universe)
    for card_id, entry in existing.items():
        if card_id in merged and entry.get("tier"):
            merged[card_id]["tier"] = entry["tier"]

    store.save_universe(merged, meta)
    kept = len(merged) - len(universe)
    print(f"universe: {len(merged)} candidates ({len(universe)} from this run"
          + (f", {kept} kept from earlier runs" if kept else "") + f") [{source}]")
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
    from gapscan.providers.ppt import PPTProvider, discover, extract_quote

    if args.discover:
        key = os.environ.get("PPT_API_KEY")
        if not key:
            print("PPT_API_KEY is not set (put it in .env).")
            return 1
        print("Trying candidate endpoints -- a 404 costs no credits.\n")
        for url, result in discover(key):
            print(f"  {result}\n    {url}\n")
        print("Set the working base in .env, e.g.:\n"
              "  PPT_API_BASE=https://www.pokemonpricetracker.com/api/v2")
        return 0

    from gapscan.providers.ppt import pick_match, results_of

    universe = store.load_universe()
    if not universe:
        print("No universe yet -- run `catalog` first.")
        return 1
    if args.card:
        card = universe.get(args.card)
        if card is None:
            print(f"{args.card} is not in the universe. Try one of: "
                  + ", ".join(list(universe)[:5]))
            return 1
    else:
        # Highest-priority card, so the probe uses one we actually care about.
        card = max(universe.values(), key=lambda e: e.get("priority", 0))
        print(f"(no --card given; using {card['id']})")

    from gapscan.providers.ppt import PPTError

    provider = PPTProvider(credits_per_card=cfg.budget.credits_per_card)
    query = args.search or provider.search_text(card)
    print(f"GET {provider.base}/cards?search={query}&limit=10")
    print(f"  looking for: {card.get('name')} ({card.get('set_name')} "
          f"#{card.get('number')})\n")
    try:
        blob = provider.raw_response(card, search=args.search)
    except PPTError as exc:
        print(f"Request failed: {exc}")
        print("\nTry a different search term:  run.py probe --search \"Kingdra\"")
        return 1

    results = results_of(blob)
    record, why = pick_match(results, card)
    print(f"--- {len(results)} result(s); {why} ---\n")
    print(json.dumps(record if record is not None else blob, indent=2)[:8000])
    if record is not None:
        print("\n--- keys on the matched record ---")
        print(", ".join(sorted(record)))
        print("\n--- what the extractor found ---")
        print(json.dumps(extract_quote(record).__dict__, indent=2))
    return 0


def cmd_reset(args, cfg: Config, store: Store) -> int:
    """Wipe cached prices and rankings -- e.g. to clear demo data before going live."""
    import shutil
    targets = [store.cards, store.history, store.root / "rankings.json",
               store.universe_path]
    present = [t for t in targets if t.exists()]
    if not present:
        print("Nothing to reset.")
        return 0
    if not args.yes:
        print("This will delete:")
        for t in present:
            print(f"  {t}")
        print("\nRe-run with --yes to confirm. Logs and .env are left alone.")
        return 1
    for t in present:
        shutil.rmtree(t) if t.is_dir() else t.unlink()
    print(f"Reset {len(present)} item(s). Next step: run.py catalog")
    return 0


def cmd_demo(args, cfg: Config, store: Store) -> int:
    """Fill the dashboard with fake but plausible data, including history.

    Everything is held in memory and written once at the end. Writing each
    simulated day to disk meant thousands of file operations, which is fast on
    Linux and painfully slow on Windows, where every file create is scanned.
    """
    args.fixture = True
    if cmd_catalog(args, cfg, store):
        return 1
    universe = store.load_universe()

    quotes: dict[str, dict] = {}
    history: dict[str, list[dict]] = {}
    stamp = store_now()
    print(f"simulating {args.days} days across {len(universe)} cards ", end="", flush=True)

    for day in range(args.days):
        provider = MockProvider(drift_seed=f"day{day}")
        for entry in universe.values():
            quotes[entry["id"]] = {
                "id": entry["id"], "fetched_at": stamp, "miss": False,
                "quote": provider.fetch(entry).__dict__, "provider": "mock"}

        rankings = rank_mod.build(universe, store, cfg, quotes=quotes, history=history)
        rank_mod.promote_watchlist(universe, rankings, cfg)

        days_ago = args.days - day - 1
        _write_backdated_snapshot(store, rankings, days_ago)
        date = _snapshot_date(days_ago)
        for row in rankings["rows"]:
            history.setdefault(row["id"], []).append(
                {"date": date, "floor_profit": row["floor_profit"]})
        print(".", end="", flush=True)
    print()

    for card_id, record in quotes.items():
        store.save_quote(card_id, record)
    store.save_universe(universe)
    rankings = rank_mod.build(universe, store, cfg, quotes=quotes, history=history)
    store.save_rankings(rankings)
    print(f"demo data ready: {len(rankings['rows'])} cards, {args.days} days of history")
    print("now run:  python3 run.py serve")
    return 0


def store_now() -> str:
    from gapscan.store import iso, utcnow
    return iso(utcnow())


def _snapshot_date(days_ago: int) -> str:
    from datetime import timedelta
    from gapscan.store import utcnow
    return (utcnow() - timedelta(days=days_ago)).date().isoformat()


def _write_backdated_snapshot(store: Store, rankings: dict, days_ago: int) -> None:
    from gapscan.store import _atomic_write
    day = _snapshot_date(days_ago)
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

    def add_log(p):
        p.add_argument("--log", action="store_true",
                       help="also append output to data/logs/<date>.log")

    p = sub.add_parser("catalog", help="rebuild the candidate universe (free)")
    p.add_argument("--fixture", action="store_true", help="use the offline fixture")
    p.add_argument("--sets", help="comma-separated set ids to rebuild, e.g. base5,gym1")
    add_log(p)
    p.set_defaults(func=cmd_catalog)

    p = sub.add_parser("scan", help="spend the day's credits on graded prices")
    add_provider(p)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--budget", type=int, help="override daily credits")
    add_log(p)
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("rank", help="re-rank, snapshot, promote the watchlist")
    p.add_argument("--top", type=int, default=15)
    add_log(p)
    p.set_defaults(func=cmd_rank)

    p = sub.add_parser("daily", help="catalog (if needed) + scan + rank")
    add_provider(p)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--budget", type=int)
    p.add_argument("--top", type=int, default=15)
    p.add_argument("--rebuild-catalog", action="store_true")
    p.add_argument("--fixture", action="store_true")
    p.add_argument("--sets", help="comma-separated set ids to rebuild")
    add_log(p)
    p.set_defaults(func=cmd_daily)

    p = sub.add_parser("probe", help="dump a raw provider response")
    p.add_argument("--card", help="universe card id, e.g. base1-4")
    p.add_argument("--discover", action="store_true",
                   help="try candidate API endpoints and report what answers")
    p.add_argument("--search", help="override the search text sent to the API")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("reset", help="delete cached prices/rankings (e.g. demo data)")
    p.add_argument("--yes", action="store_true", help="actually do it")
    p.set_defaults(func=cmd_reset)

    p = sub.add_parser("demo", help="populate with fake data end to end")
    p.add_argument("--days", type=int, default=21)
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("serve", help="serve the dashboard locally")
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(func=cmd_serve)

    args = parser.parse_args()
    here = Path(__file__).resolve().parent
    load_env_file(here / ".env")
    handle = start_logging(here) if getattr(args, "log", False) else None
    try:
        cfg = Config.load(args.config)
        return args.func(args, cfg, Store())
    finally:
        if handle is not None:
            sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
            handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
