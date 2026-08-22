"""Turn cached quotes into a ranking, and promote the top cards to the watchlist."""
from __future__ import annotations

from .config import Config
from .econ import Quote, days_since, evaluate
from .scan import coverage
from .store import Store, age_days, iso, utcnow


def _streak(series: list[dict], threshold: float) -> int:
    """Consecutive days, counting back from today, clearing the threshold.

    A one-day gap is usually a stale comp or a single odd sale; a gap that
    survives weeks is the thing worth acting on.
    """
    count = 0
    for point in reversed(series):
        value = point.get("floor_profit")
        if value is None or value < threshold:
            break
        count += 1
    return count


def build(universe: dict, store: Store, cfg: Config,
          quotes: dict | None = None, history: dict | None = None) -> dict:
    """Rank every priced card.

    `quotes` and `history` let a caller supply data it already holds, so a
    repeated build (the demo replaying many days) doesn't re-read the whole
    cache directory each time.
    """
    if history is None:
        history = store.load_history()
    rows = []

    for entry in universe.values():
        cached = quotes[entry["id"]] if quotes is not None else store.load_quote(entry["id"])
        if not cached or cached.get("miss") or not cached.get("quote"):
            continue
        quote = Quote(**cached["quote"])
        verdict = evaluate(quote, cfg.econ, cfg.thresholds)
        if verdict is None:
            continue

        series = history.get(entry["id"], [])
        rows.append({
            "id": entry["id"],
            "name": entry.get("name"),
            "number": entry.get("number"),
            "set_name": entry.get("set_name"),
            "set_id": entry.get("set_id"),
            "rarity": entry.get("rarity"),
            "image": entry.get("image"),
            "seed_reason": entry.get("seed_reason"),
            "raw": quote.raw,
            "psa9": quote.psa9,
            "psa10": quote.psa10,
            "sales_9": quote.sales_9,
            "sales_10": quote.sales_10,
            "cgc9": quote.cgc9,
            "cgc10": quote.cgc10,
            "psa9_confidence": quote.psa9_confidence,
            "psa9_sale_age_days": (round(age, 1)
                                   if (age := days_since(quote.psa9_last_sale)) is not None
                                   else None),
            "psa10_sale_age_days": (round(age10, 1)
                                    if (age10 := days_since(quote.psa10_last_sale)) is not None
                                    else None),
            "verdict": verdict.verdict,
            "all_in": verdict.all_in,
            "floor_profit": verdict.floor_profit,
            "floor_roi": verdict.floor_roi,
            "upside_profit": verdict.upside_profit,
            "upside_roi": verdict.upside_roi,
            "breakeven_p10": verdict.breakeven_p10,
            "confident": verdict.confident,
            "reasons": verdict.reasons,
            "scanned_days_ago": round(age_days(cached.get("fetched_at")), 1),
            "days_tracked": len(series),
            "floor_streak": _streak(series, cfg.thresholds.min_floor_profit),
            "history": [p.get("floor_profit") for p in series][-60:],
        })

    # Confident cards first, then by what clears at a 9 -- the question the
    # whole tool exists to answer.
    rows.sort(key=lambda r: (r["confident"], r["floor_profit"]), reverse=True)

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1

    return {
        "generated_at": iso(utcnow()),
        "config": cfg.to_dict(),
        "coverage": coverage(universe, store, cfg, quotes=quotes),
        "verdict_counts": counts,
        "rows": rows,
    }


def promote_watchlist(universe: dict, rankings: dict, cfg: Config) -> int:
    """Mark the current leaders as watchlist so they get the weekly refresh."""
    size = cfg.budget.watchlist_size
    leaders = [r["id"] for r in rankings["rows"][:size]]
    leader_set = set(leaders)
    changed = 0
    for card_id, entry in universe.items():
        was = entry.get("tier", "candidate")
        if card_id in leader_set:
            now = "watchlist"
        elif was == "watchlist":
            now = "candidate"  # dropped out of the top N; back to slow rotation
        else:
            now = was
        if now != was:
            entry["tier"] = now
            changed += 1
    return changed
