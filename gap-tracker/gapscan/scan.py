"""Budget-aware rolling scanner.

The free tier gives ~50 cards/day of graded data. That is not enough to both
refresh a watchlist and discover new cards every day, so the two jobs share one
budget under an explicit split: the watchlist takes at most `watchlist_share`
of the credits, discovery gets the rest, and anything the watchlist doesn't
claim rolls over to discovery.
"""
from __future__ import annotations

from dataclasses import asdict

from .config import Config
from .store import Store, age_days, iso, utcnow


def _ttl_for(entry: dict, budget) -> float:
    return {
        "watchlist": budget.watchlist_ttl_days,
        "rejected": budget.rejected_ttl_days,
    }.get(entry.get("tier", "candidate"), budget.candidate_ttl_days)


def build_queue(universe: dict, store: Store, cfg: Config) -> tuple[list[dict], list[dict]]:
    """Split due cards into (watchlist_due, discovery_due), each best-first."""
    watchlist, discovery = [], []
    for entry in universe.values():
        cached = store.load_quote(entry["id"])
        age = age_days((cached or {}).get("fetched_at"))
        if age < _ttl_for(entry, cfg.budget):
            continue
        row = dict(entry, _age=age, _seen=cached is not None)
        (watchlist if entry.get("tier") == "watchlist" else discovery).append(row)

    # Watchlist: stalest first, so nothing in the top 50 goes quietly cold.
    watchlist.sort(key=lambda r: -r["_age"])
    # Discovery: never-scanned first (highest seed priority wins), then stalest.
    discovery.sort(key=lambda r: (r["_seen"], -r.get("priority", 0), -r["_age"]))
    return watchlist, discovery


def run(universe: dict, store: Store, cfg: Config, provider, dry_run: bool = False) -> dict:
    budget = cfg.budget
    per_card = provider.credits_per_card or budget.credits_per_card
    total_cards = budget.daily_credits // per_card if per_card else len(universe)

    watch_due, disc_due = build_queue(universe, store, cfg)
    watch_cap = int(total_cards * budget.watchlist_share)
    watch_batch = watch_due[:watch_cap]
    # Unclaimed watchlist budget rolls into discovery rather than going to waste.
    disc_batch = disc_due[:total_cards - len(watch_batch)]
    batch = watch_batch + disc_batch

    print(f"budget: {budget.daily_credits} credits / {per_card} per card "
          f"= {total_cards} cards")
    print(f"  due: {len(watch_due)} watchlist, {len(disc_due)} discovery")
    print(f"  scanning: {len(watch_batch)} watchlist + {len(disc_batch)} discovery")
    if dry_run:
        for row in batch:
            print(f"    would fetch {row['id']:<16} {row.get('name')} "
                  f"({row.get('set_name')})")
        return {"scanned": 0, "dry_run": True, "planned": len(batch)}

    scanned = misses = 0
    for row in batch:
        quote = provider.fetch(row)
        if quote is None:
            misses += 1
            # Record the miss so a card with no graded market isn't retried daily.
            store.save_quote(row["id"], {"id": row["id"], "fetched_at": iso(utcnow()),
                                         "quote": None, "miss": True,
                                         "provider": provider.name})
        else:
            store.save_quote(row["id"], {"id": row["id"], "fetched_at": iso(utcnow()),
                                         "quote": asdict(quote), "miss": False,
                                         "provider": provider.name})
        scanned += 1

    credits = scanned * per_card
    print(f"  done: {scanned} cards ({credits} credits), {misses} with no graded data")
    return {"scanned": scanned, "misses": misses, "credits_spent": credits,
            "watchlist": len(watch_batch), "discovery": len(disc_batch)}


def coverage(universe: dict, store: Store, cfg: Config) -> dict:
    seen = stale = 0
    oldest = 0.0
    for entry in universe.values():
        cached = store.load_quote(entry["id"])
        if not cached:
            continue
        seen += 1
        age = age_days(cached.get("fetched_at"))
        oldest = max(oldest, age if age != float("inf") else 0)
        if age >= _ttl_for(entry, cfg.budget):
            stale += 1
    per_card = cfg.budget.credits_per_card or 1
    per_day = max(cfg.budget.daily_credits // per_card, 1)
    remaining = len(universe) - seen
    return {
        "universe": len(universe),
        "scanned": seen,
        "stale": stale,
        "oldest_scan_days": round(oldest, 1),
        "days_to_full_coverage": round(remaining / per_day, 1) if remaining else 0,
    }
