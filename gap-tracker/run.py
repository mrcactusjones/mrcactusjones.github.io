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

from gapscan import ingest
from gapscan import rank as rank_mod
from gapscan import watchlist as watchlist_mod
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
    return PPTProvider(credits_per_card=cfg.budget.credits_per_call,
                       search_limit=cfg.budget.search_limit,
                       include_graded=cfg.budget.include_graded)


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
    # Hand-picked cards are never touched by a catalog rebuild.
    same_source = {k: v for k, v in existing.items()
                   if v.get("source", "api") == source or v.get("source") == "manual"}
    # A partial build (--sets, or sets that failed) must leave the rest intact:
    # replace only the sets actually rebuilt this run.
    rebuilt = {v["set_id"] for v in universe.values()}
    merged = {k: v for k, v in same_source.items() if v.get("set_id") not in rebuilt}
    merged.update(universe)
    for card_id, entry in existing.items():
        if card_id in merged and entry.get("tier"):
            merged[card_id]["tier"] = entry["tier"]

    watch_blob = watchlist_mod.load()
    watchlist_mod.save(watch_blob)          # persist any migrated resolutions
    merged.update(watchlist_mod.to_universe(watch_blob))
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
        print(f" {flag} {row.get('conviction', 0):>5.0f}  ${row['floor_profit']:>8.2f} floor  "
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

    provider = PPTProvider(credits_per_card=cfg.budget.credits_per_call,
                           search_limit=cfg.budget.search_limit)
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
        graded = record.get("ebay")
        print("\n--- graded block ---")
        print(json.dumps(graded, indent=2)[:3000] if graded
              else "none returned (PSA prices need includeEbay=true)")
        print("\n--- what the extractor found ---")
        print(json.dumps(extract_quote(record).__dict__, indent=2))
    return 0


def cmd_filters(args, cfg: Config, store: Store) -> int:
    """Find a parameter that fetches exactly one known card.

    Billing is per card returned, so each attempt uses limit=1 and no graded
    block: one credit, win or lose.
    """
    from gapscan.providers.ppt import (FILTER_CANDIDATES, PPTError, PPTProvider,
                                       credits_from_error, results_of)

    universe = store.load_universe()
    card = universe.get(args.card) if args.card else None
    if card is None:
        live = [e for e in universe.values() if e.get("source") != "fixture"]
        if not live:
            print("No live universe -- run `catalog` first.")
            return 1
        card = max(live, key=lambda e: e.get("priority", 0))
    print(f"Target: {card['id']}  {card.get('name')} "
          f"({card.get('set_name')} #{card.get('number')})")
    print("Each attempt costs 1 credit (limit=1, no graded block).\n")

    provider = PPTProvider(credits_per_card=1, search_limit=1, include_graded=False)
    winners = []
    for name, build in FILTER_CANDIDATES:
        value = build(card)
        if not value:
            continue
        try:
            # search="" really means empty now, so a hit is the filter's doing.
            blob = provider.raw_response(card, search="", graded=False,
                                         filters={name: value})
            provider.credits_used += 1
        except PPTError as exc:
            if exc.code == 429:
                print(f"  out of credits -- {credits_from_error(exc.detail) or exc.detail}")
                break
            print(f"  {name}={value}: HTTP {exc.code}")
            continue

        records = results_of(blob)
        if not records:
            print(f"  {name}={value}: 0 results")
            continue
        top = records[0]
        got = top.get("externalCatalogId")
        exact = str(got) == str(card["id"])
        flag = "HIT " if exact else "    "
        print(f"  {flag}{name}={value}: {len(records)} result(s), "
              f"top={top.get('setName')} #{top.get('cardNumber')} id={got}")
        if exact:
            winners.append(name)

    print()
    if winners:
        print(f"Exact-lookup parameter(s): {', '.join(winners)}")
        print(f"Put it in .env as  PPT_LOOKUP_PARAM={winners[0]}")
    else:
        print("No exact-lookup parameter found. Falling back to search+verify.")
    return 0


def cmd_watchlist(args, cfg: Config, store: Store) -> int:
    """Resolve hand-picked cards to exact provider records, then track them."""
    blob = watchlist_mod.load()
    entries = blob.get("cards", [])
    if not entries:
        print(f"No entries in {watchlist_mod.PATH}")
        return 0

    if not args.resolve:
        for entry in entries:
            mark = "ok " if watchlist_mod.is_resolved(entry) else "?? "
            where = entry.get("set_name") or f"unresolved (hint: {entry.get('set_hint')})"
            print(f"  {mark}{entry['name']} #{entry['number']} -- {where}")
        # Persist anything migrated out of the tracked file, so reverting that
        # file can't take the resolutions with it.
        watchlist_mod.save(blob)
        done = sum(1 for e in entries if watchlist_mod.is_resolved(e))
        print(f"\n{done}/{len(entries)} resolved; stored in {watchlist_mod.LOCAL}")
        if done < len(entries):
            print("Resolve the rest with --resolve.")
        return 0

    from gapscan.providers.ppt import PPTError, PPTProvider

    provider = PPTProvider(credits_per_card=cfg.budget.credits_per_call,
                           search_limit=cfg.budget.search_limit)
    pending = [e for e in entries if not watchlist_mod.is_resolved(e) or args.force]
    print(f"Resolving {len(pending)} entr(ies), 1 credit each (no price data).\n")

    for entry in pending:
        query = f"{entry['name']} {entry.get('set_hint', '')}".strip() if args.use_hint \
            else entry["name"]
        try:
            blob_resp = provider.raw_response({"name": entry["name"]}, search=query,
                                              graded=False)
        except PPTError as exc:
            from gapscan.providers.ppt import credits_from_error
            if exc.code == 429:
                print(f"  {entry['name']} #{entry['number']}: out of credits -- "
                      f"{credits_from_error(exc.detail) or exc.detail}")
                print("  stopping; the rest keep their place in the list.")
                break
            print(f"  {entry['name']} #{entry['number']}: request failed -- {exc}")
            continue

        records = watchlist_mod.results_of(blob_resp)
        hits = watchlist_mod.candidates_for(entry, records)
        label = f"{entry['name']} #{entry['number']} ({entry.get('set_hint')})"

        if len(hits) == 1:
            watchlist_mod.apply_resolution(entry, hits[0])
            print(f"  OK  {label}\n        -> {watchlist_mod.summarise(hits[0])}")
        elif not hits:
            print(f"  --  {label}: no result with that number "
                  f"among {len(records)} hit(s)")
            for record in records[:5]:
                print(f"        saw: {watchlist_mod.summarise(record)}")
        else:
            print(f"  ??  {label}: {len(hits)} candidates -- pick one and put its "
                  f"set name in watchlist.json")
            for record in hits[:8]:
                print(f"        {watchlist_mod.summarise(record)}")

    watchlist_mod.save(blob)
    done = sum(1 for e in entries if watchlist_mod.is_resolved(e))
    print(f"\n{done}/{len(entries)} resolved -> {watchlist_mod.LOCAL}")
    if done:
        print("Next: run.py catalog   (folds them into the universe)")
    return 0


def cmd_backfill(args, cfg: Config, store: Store) -> int:
    """Sweep whole sets, storing months of price history in the database.

    This is what the paid tier buys: history arrives in one pass instead of
    accruing a day at a time.
    """
    from gapscan import db, ingest
    from gapscan.providers.ppt import OutOfCredits, PPTError

    universe = store.load_universe()
    if not universe:
        print("No universe yet -- run `catalog` first.")
        return 1

    wanted = [s.strip() for s in args.sets.split(",")] if args.sets else None
    sets: list[str] = []
    for entry in sorted(universe.values(), key=lambda e: -e.get("priority", 0)):
        name = entry.get("set_name")
        if name and name not in sets and (not wanted or name in wanted):
            sets.append(name)
    if not sets:
        print("No matching sets in the universe.")
        return 1

    budget = args.budget or cfg.budget.daily_credits
    per_page = args.limit * 3
    if per_page > budget:
        # Silently storing nothing is the worst possible outcome here.
        print(f"One page of {args.limit} cards costs {per_page} credits, but the "
              f"budget is {budget}. Nothing would be fetched.\n")
        print("Either lower the page size:")
        print(f"  run.py backfill --provider ppt --limit {max(1, budget // 3)}")
        print("or raise the budget for the paid tier, in config.json next to run.py:")
        print('  { "budget": { "daily_credits": 20000 } }')
        return 1
    provider = get_provider(args, cfg)
    print(f"{len(sets)} set(s), {args.limit} cards/page, {args.days} days of history")
    print(f"budget {budget} credits; each page costs {per_page}\n")

    cards = points = discovered = 0
    with db.session() as conn:
        for set_name in sets:
            offset = 0
            while True:
                if provider.credits_used + per_page > budget:
                    print(f"  budget reached ({provider.credits_used}/{budget})")
                    set_name = None
                    break
                try:
                    records, _ = provider.fetch_batch(
                        set_name, days=args.days, limit=args.limit, offset=offset,
                        min_price=None if args.all_prices else cfg.thresholds.raw_price_min,
                        max_price=None if args.all_prices else cfg.thresholds.raw_price_max)
                except OutOfCredits as exc:
                    print(f"  stopping: {exc}")
                    set_name = None
                    break
                except PPTError as exc:
                    print(f"  ! {set_name} @{offset}: {exc}")
                    break
                if not records:
                    break

                for record in records:
                    card_id, added = ingest.ingest_record(conn, record, universe)
                    cards += 1
                    points += added
                    # A set sweep returns cards the seed list never chose. They
                    # are paid for either way, so fold them into the universe
                    # rather than leaving them invisible to ranking.
                    if card_id not in universe:
                        entry = ingest.card_from_record(record, universe)
                        universe[card_id] = {
                            "id": card_id, "name": entry["name"],
                            "number": entry["number"], "set_id": "sweep",
                            "set_name": entry["set_name"], "rarity": entry["rarity"],
                            "raw_hint": None, "priority": 0,
                            "seed_reason": f"found sweeping {set_name}",
                            "image": None, "tier": "candidate", "source": "sweep",
                        }
                        discovered += 1
                    # Keep the JSON cache in step so ranking works unchanged.
                    quote = ingest.extract_quote(record)
                    if quote.psa9 is not None or quote.psa10 is not None:
                        store.save_quote(card_id, {
                            "id": card_id, "fetched_at": store_now(), "miss": False,
                            "quote": quote.__dict__, "provider": provider.name})
                print(f"  {set_name} @{offset}: {len(records)} cards, "
                      f"{provider.credits_used} credits used")
                if len(records) < args.limit:
                    break
                offset += args.limit
            if set_name is None:
                break
        stats = db.stats(conn)

    store.save_universe(universe)
    print(f"\nstored {cards} card(s), {points} price points "
          f"({provider.credits_used} credits)")
    if discovered:
        print(f"universe grew by {discovered} card(s) found while sweeping")
    print(f"database: {stats['cards']} cards, {stats['price_points']} points, "
          f"{stats['earliest']} to {stats['latest']}")
    print("Next: run.py rank")
    return 0


def cmd_trends(args, cfg: Config, store: Store) -> int:
    """Rank by how well a gap has held, not just how big it is today."""
    import json as _json
    path = store.root / "rankings.json"
    if not path.exists():
        print("No rankings yet -- run `rank` first.")
        return 1
    rows = _json.loads(path.read_text()).get("rows", [])
    scored = [r for r in rows if r.get("floor_days_held_90d") is not None]
    if not scored:
        print("No trend data yet -- run `backfill` to load price history.")
        return 1

    scored.sort(key=lambda r: (r.get("floor_days_held_90d", 0),
                               r.get("floor_profit", 0)), reverse=True)
    print(f"{'held':>5} {'streak':>7} {'floor':>10} {'psa9 30d':>9} "
          f"{'diverge':>8}  card")
    for row in scored[:args.top]:
        def pct(value):
            return f"{value * 100:+.0f}%" if value is not None else "    -"
        print(f"{row['floor_days_held_90d']:>5} {row.get('floor_streak', 0):>7} "
              f"${row['floor_profit']:>9.2f} {pct(row.get('psa9_30d')):>9} "
              f"{pct(row.get('divergence_30d')):>8}  "
              f"{row['name']} ({row['set_name']} {row['number']})")
    return 0


def cmd_population(args, cfg: Config, store: Store) -> int:
    """Check whether this plan can reach population data (2 credits to find out)."""
    from gapscan.providers.ppt import (PopulationUnavailable, PPTProvider,
                                       OutOfCredits)

    universe = store.load_universe()
    priced = [(cid, rec) for cid, rec in store.all_quotes()
              if rec.get("quote", {}).get("tcgplayer_id")]
    if not priced:
        print("No scanned card has a tcgPlayerId yet -- run `daily` first.")
        return 1
    card_id, record = priced[0]
    tcg_id = record["quote"]["tcgplayer_id"]
    name = (universe.get(card_id) or {}).get("name", card_id)
    print(f"Asking for population of {name} (tcgPlayerId {tcg_id}) -- costs 2 credits.\n")

    provider = PPTProvider(credits_per_card=2, search_limit=1)
    try:
        pop = provider.fetch_population(tcg_id)
    except PopulationUnavailable as exc:
        print("Not available on this plan (that is the expected answer on free/API "
              f"tiers):\n  {exc}")
        print("\nGem rates will keep coming from the graded-sales mix instead.")
        return 0
    except OutOfCredits as exc:
        print(f"Out of credits: {exc}")
        return 1

    if not pop:
        print("Reachable, but no population recorded for this card.")
        return 0
    print(f"Available. grades={pop['grades']} total={pop['total']} "
          f"gem_rate={pop['gem_rate']:.1%}")
    print("\nSet  population: true  under \"budget\" in config.json to use it.")
    return 0


def cmd_status(args, cfg: Config, store: Store) -> int:
    """Where the project stands. Spends no credits."""
    import json as _json

    print("keys")
    for name, label in (("PPT_API_KEY", "pokemonpricetracker"),
                        ("POKEMONTCG_API_KEY", "pokemontcg.io")):
        value = os.environ.get(name)
        shown = f"...{value[-4:]}" if value else "MISSING"
        print(f"  {label:<22} {shown}")

    universe = store.load_universe()
    if not universe:
        print("\nNo universe yet. Next: run.py catalog")
        return 0

    sources = {}
    tiers = {}
    for entry in universe.values():
        sources[entry.get("source", "?")] = sources.get(entry.get("source", "?"), 0) + 1
        tiers[entry.get("tier", "candidate")] = tiers.get(entry.get("tier", "candidate"), 0) + 1
    cov = scan_mod.coverage(universe, store, cfg)
    per_day = max(cfg.budget.daily_credits // (cfg.budget.credits_per_card or 1), 1)

    print(f"\nuniverse   {len(universe)} cards  {sources}")
    if sources.get("fixture"):
        print("           WARNING: demo data. Clear it with `run.py reset --yes`, "
              "then `run.py catalog`.")
    print(f"tiers      {tiers}")
    print(f"coverage   {cov['scanned']}/{cov['universe']} scanned, {cov['stale']} stale, "
          f"oldest {cov['oldest_scan_days']}d")
    print(f"           ~{cov['days_to_full_coverage']} more day(s) at {per_day} cards/day")

    newest, misses = None, 0
    for _, record in store.all_quotes():
        if record.get("miss"):
            misses += 1
        stamp = record.get("fetched_at")
        if stamp and (newest is None or stamp > newest):
            newest = stamp
    if newest:
        print(f"last scan  {newest}  ({misses} card(s) with no graded data)")
    else:
        print("last scan  never -- next: run.py daily --provider ppt --log")

    snapshots = sorted(store.history.glob("*.json")) if store.history.exists() else []
    print(f"history    {len(snapshots)} day(s)"
          + (f", {snapshots[0].stem} to {snapshots[-1].stem}" if snapshots else ""))

    rankings = store.root / "rankings.json"
    if rankings.exists():
        blob = _json.loads(rankings.read_text())
        counts = blob.get("verdict_counts", {})
        confident = sum(1 for r in blob.get("rows", []) if r.get("confident"))
        print(f"rankings   {len(blob.get('rows', []))} priced, {confident} confident, {counts}")
        best = [r for r in blob.get("rows", []) if r.get("confident")][:3]
        for row in best:
            print(f"           ${row['floor_profit']:>8.2f} floor  {row['name']} "
                  f"({row['set_name']} {row['number']})")
    else:
        print("rankings   none yet")
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

    p = sub.add_parser("filters", help="find an exact-lookup param (1 credit per try)")
    p.add_argument("--card", help="universe card id to test against")
    p.set_defaults(func=cmd_filters)

    p = sub.add_parser("watchlist", help="hand-picked cards; --resolve to pin them")
    p.add_argument("--resolve", action="store_true", help="look each entry up (1 credit each)")
    p.add_argument("--force", action="store_true", help="re-resolve already-resolved entries")
    p.add_argument("--use-hint", action="store_true",
                   help="include set_hint in the search text")
    p.set_defaults(func=cmd_watchlist)

    p = sub.add_parser("backfill", help="sweep sets and store price history")
    add_provider(p)
    p.add_argument("--days", type=int, default=180, help="history depth to request")
    p.add_argument("--limit", type=int, default=100, help="cards per page (max 100)")
    p.add_argument("--sets", help="comma-separated set names, default all")
    p.add_argument("--budget", type=int, help="credit ceiling for this run")
    p.add_argument("--all-prices", action="store_true",
                   help="don't filter to the raw price band server-side")
    add_log(p)
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("trends", help="rank by how long the gap has held")
    p.add_argument("--top", type=int, default=20)
    p.set_defaults(func=cmd_trends)

    p = sub.add_parser("population", help="test population access (2 credits)")
    p.set_defaults(func=cmd_population)

    p = sub.add_parser("status", help="where things stand; spends no credits")
    p.set_defaults(func=cmd_status)

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
