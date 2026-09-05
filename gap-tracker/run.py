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
from gapscan import trends
from gapscan.catalog import build as build_catalog
from gapscan.catalog import merge_universe
from gapscan.catalog import sweep_targets
from gapscan.providers.ppt import PAGE_MAX
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
    merged = merge_universe(existing, universe, source)
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
    stale = rankings.get("stale_variant_data", 0)
    if stale:
        print(f"note: {stale} card(s) were fetched before printing data was "
              f"captured, so the variant check cannot run on them yet.\n"
              f"      re-run `backfill` to populate it.")
    for row in rankings["rows"][:args.top]:
        flag = " " if row["confident"] else "?"
        # With no PSA 10 comps the model refuses to invent upside and leaves it
        # equal to the floor. Printing that number twice reads as "no upside at
        # a 10", which is a claim about the market rather than about our data.
        at10 = (f"${row['upside_profit']:>8.2f} at 10" if row.get("upside_known", True)
                else f"{'--':>9} at 10")
        print(f" {flag} {row.get('conviction', 0):>5.0f}  ${row['floor_profit']:>8.2f} floor  "
              f"{at10}  {row['name']} ({row['set_name']} {row['number']})")
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
    query = args.search or card.get("name") or provider.search_text(card)
    print(f"GET {provider.base}/cards?search={query}"
          + (f"&set={card.get('set_name')}" if card.get("set_name") else "")
          + f"&limit={provider.search_limit}"
          + (f"&includeHistory=true&days={args.history}" if args.history else ""))
    print(f"  looking for: {card.get('name')} ({card.get('set_name')} "
          f"#{card.get('number')})\n")
    try:
        # Same set filter fetch() uses -- without it the probe is not
        # reproducing the scanner, and a search for "Alakazam Base" comes
        # back with Base Set 2's copy.
        blob = provider.raw_response(
            card, search=args.search or card.get("name"),
            filters={"set": card["set_name"]} if card.get("set_name") else None,
            history_days=args.history)
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
        print(json.dumps(graded, indent=2)[:2500] if graded
              else "none returned (PSA prices need includeEbay=true)")

        if args.history:
            from gapscan.providers.ppt import parse_history
            print(f"\n--- raw price history (asked for {args.history} days) ---")
            print(json.dumps(record.get("priceHistory"), indent=2)[:2000])
            print("\n--- graded price history ---")
            print(json.dumps((graded or {}).get("priceHistory"), indent=2)[:2000])
            found = parse_history(record)
            print("\n--- what the history parser found ---")
            print({grade: len(points) for grade, points in found.items()} or
                  "nothing: the response carried no usable history")
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
    targets = sweep_targets(universe, load_seeds().get("sets", []), wanted)
    if not targets:
        print("No matching sets in the universe.")
        return 1

    budget = args.budget or cfg.budget.daily_credits
    if args.limit > PAGE_MAX:
        print(f"note: the server returns at most {PAGE_MAX} cards a page but bills "
              f"the limit asked for, so --limit {args.limit} would pay for "
              f"{args.limit - PAGE_MAX} undelivered cards each time. Using {PAGE_MAX}.")
        args.limit = PAGE_MAX
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
    pinned = sum(1 for t in targets if t.set_id)
    print(f"{len(targets)} set(s) ({pinned} by set id, {len(targets) - pinned} by "
          f"name), {args.limit} cards/page, {args.days} days of history")
    print(f"budget {budget} credits; each page costs {per_page}\n")

    cards = points = discovered = repinned = 0
    stop = False
    with db.session() as conn:
        for target in targets:
            if stop:
                break
            offset = 0
            while True:
                if provider.credits_used + per_page > budget:
                    print(f"  budget reached ({provider.credits_used}/{budget})")
                    stop = True
                    break
                try:
                    records, _ = provider.fetch_batch(
                        target.set_name, set_id=target.set_id,
                        days=args.days, limit=args.limit, offset=offset,
                        min_price=None if args.all_prices else cfg.thresholds.raw_price_min,
                        max_price=None if args.all_prices else cfg.thresholds.raw_price_max)
                except OutOfCredits as exc:
                    print(f"  stopping: {exc}")
                    stop = True
                    break
                except PPTError as exc:
                    print(f"  ! {target.label} @{offset}: {exc}")
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
                            "seed_reason": f"found sweeping {target.label}",
                            "image": None, "tier": "candidate", "source": "sweep",
                        }
                        discovered += 1
                    # Pin PPT's own set id so the next sweep can address this
                    # set exactly instead of guessing at its name.
                    repinned += ingest.pin_set(universe[card_id], record)
                    # Keep the JSON cache in step so ranking works unchanged.
                    quote = ingest.extract_quote(record)
                    if quote.psa9 is not None or quote.psa10 is not None:
                        store.save_quote(card_id, {
                            "id": card_id, "fetched_at": store_now(), "miss": False,
                            "quote": quote.__dict__, "provider": provider.name})
                print(f"  {target.label} @{offset}: {len(records)} cards, "
                      f"{provider.credits_used} credits used")
                # Advance by what the server actually returned. Using the
                # requested limit stopped every set after one page, because the
                # server caps a page below what we asked for.
                offset += len(records)
                if len(records) < min(args.limit, PAGE_MAX):
                    break
        stats = db.stats(conn)

    store.save_universe(universe)
    print(f"\nstored {cards} card(s), {points} price points "
          f"({provider.credits_used} credits)")
    if discovered:
        print(f"universe grew by {discovered} card(s) found while sweeping")
    if repinned:
        print(f"pinned {repinned} card(s) to PPT's own set id; the next sweep "
              f"addresses those sets exactly instead of by name")
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

    scored = trends.by_durability(scored)

    def pct(value):
        return f"{value * 100:+.0f}%" if value is not None else "     -"

    print(f"{'held':>7} {'streak':>7} {'floor':>10} {'psa9':>7} {'/n':<4}"
          f"{'diverge':>8}  card")
    for row in scored[:args.top]:
        # Both numbers: "13" alone reads as thin coverage when it can be every
        # observation there is. A gap point needs a raw and a graded price on
        # the same day, and graded sales are sparse.
        held = f"{row['floor_days_held_90d']}/{row.get('floor_observations_90d', '?')}"
        # Graded sales are too sparse for a 30-day trend on most cards; the
        # 90-day window is the one that usually has enough points to mean
        # anything, so prefer it and fall back.
        move = row.get("psa9_90d")
        if move is None:
            move = row.get("psa9_30d")
        # How many PSA 9 prices that percentage rests on. Without it a trend
        # from four sales and one from forty look equally solid.
        comps = row.get("psa9_observations_90d")
        # The floor is the whole test, so an underwater one is marked in the
        # row rather than left for the reader to notice the minus sign.
        mark = " " if row.get("floor_profit", 0) > 0 else "v"
        print(f"{mark}{held:>6} {row.get('floor_streak', 0):>6}d ${row['floor_profit']:>9.2f} "
              f"{pct(move):>7} {'' if comps is None else f'/{comps}':<4}"
              f"{pct(row.get('divergence_30d')):>8}  "
              f"{row['name']} ({row['set_name']} {row['number']})")

    sunk = sum(1 for r in scored[:args.top] if r.get("floor_profit", 0) <= 0)
    if sunk:
        print(f"\n{sunk} row(s) marked 'v' are under water today; a long hold "
              f"there describes a collapse, not an opportunity.")
    thin = sum(1 for r in scored if r.get("psa9_90d") is None)
    if thin:
        print(f"\n{thin} of {len(scored)} cards have too few graded sales for a "
              f"price trend; '-' means unavailable, not flat.")
    return 0


def cmd_series(args, cfg: Config, store: Store) -> int:
    """Print the stored price points behind a trend. Spends no credits.

    Every trend figure is derived from these rows, and until now nothing could
    show them -- so a percentage that moved between runs could not be traced
    to the data or to the maths.
    """
    from gapscan import db

    with db.session() as conn:
        rows = db.series_detail(conn, args.card, args.grade)
        if not rows:
            have = db.grades_for(conn, args.card)
            print(f"No {args.grade} points for {args.card}.")
            print(f"  grades stored: {', '.join(have) if have else 'none'}"
                  if have else "  that card has no stored prices at all")
            return 1

    points = [(r["date"], r["price"]) for r in rows]
    recent = trends.window(points, args.days)
    keep = {d for d, _ in recent}

    print(f"{args.card} {args.grade}: {len(rows)} point(s) stored, "
          f"{len(recent)} in the last {args.days} days\n")
    print(f"{'date':<12}{'price':>10}  {'sales':>5}  {'origin':<9} in window")
    for row in rows:
        inside = "yes" if row["date"] in keep else ""
        sales = "" if row["sales"] is None else str(row["sales"])
        print(f"{row['date']:<12}{row['price']:>10.2f}  {sales:>5}  "
              f"{(row['origin'] or ''):<9} {inside}")

    print()
    for days in sorted({30, 90, args.days}):
        move = trends.change_pct(points, days)
        n = trends.observations(points, days)
        shown = f"{move * 100:+.1f}%" if move is not None else "n/a"
        print(f"  {days:>3}d change {shown:>8}  from {n} point(s)"
              + ("" if n >= 4 else "  (under the 4-point minimum)"))

    vol = trends.volatility(points, args.days)
    if vol is not None:
        print(f"  volatility  {vol * 100:>7.1f}%  sale-to-sale")

    # The question this command exists to answer: is this one card's prices,
    # or two cards' sales sharing a title-parsed grade?
    split = trends.comps_split([v for _, v in trends.window(points, args.days)],
                               cfg.thresholds.comps_split_spread,
                               cfg.thresholds.comps_split_min_share,
                               cfg.thresholds.comps_split_min_sample)
    if split is None:
        print("  reads as one card's sales")
    else:
        print(f"\n  TWO CARDS POOLED (spread {split.spread:.2f}x, "
              f"cut at ${split.boundary:,.2f})")
        print(f"    {split.low_count} sale(s) near ${split.low:,.2f}  <- the floor "
              f"is priced from these")
        print(f"    {split.high_count} sale(s) near ${split.high:,.2f}")
    return 0


def cmd_population(args, cfg: Config, store: Store) -> int:
    """Check whether this plan can reach population data (2 credits to find out)."""
    from gapscan.providers.ppt import (PopulationUnavailable, PPTProvider,
                                       OutOfCredits)

    universe = store.load_universe()
    # scan.py records a miss as {"quote": None}, so .get("quote", {}) is None.
    priced = [(cid, rec) for cid, rec in store.all_quotes()
              if (rec.get("quote") or {}).get("tcgplayer_id")]
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
    print("\nPopulation data is reachable. Gem rates will use it in preference "
          "to the sales-mix estimate.")
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
    p.add_argument("--history", type=int, metavar="DAYS",
                   help="also request price history and show what came back")
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
    p.add_argument("--limit", type=int, default=25,
                   help=f"cards per page (server returns at most {PAGE_MAX})")
    p.add_argument("--sets", help="comma-separated set names, default all")
    p.add_argument("--budget", type=int, help="credit ceiling for this run")
    p.add_argument("--all-prices", action="store_true",
                   help="don't filter to the raw price band server-side")
    add_log(p)
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("trends", help="rank by how long the gap has held")
    p.add_argument("--top", type=int, default=20)
    p.set_defaults(func=cmd_trends)

    p = sub.add_parser("series", help="print the stored prices behind a trend")
    p.add_argument("--card", required=True, help="card id, e.g. base2-12")
    p.add_argument("--grade", default="psa9", help="raw, psa8, psa9, psa10, cgc9...")
    p.add_argument("--days", type=int, default=90, help="window to summarise")
    p.set_defaults(func=cmd_series)

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
