"""Turn cached quotes into a ranking, and promote the top cards to the watchlist."""
from __future__ import annotations

from .config import Config
from .econ import (Quote, days_since, evaluate, mix_from_population,
                   mix_from_sales)
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


def _set_multiples(priced: list, min_sample: int) -> dict[str, float]:
    """Median PSA 9 / raw multiple per set.

    A card whose multiple is wildly out of step with the rest of its set is
    usually carrying another card's comps -- the graded sales are matched from
    listing titles, and a set like Aquapolis holds several cards of the same
    name. Needs enough cards in the set for a median to mean anything.
    """
    import statistics
    by_set: dict[str, list[float]] = {}
    for entry, quote in priced:
        if quote.raw and quote.psa9 and quote.raw > 0:
            by_set.setdefault(entry.get("set_name") or "?", []).append(quote.psa9 / quote.raw)
    return {name: statistics.median(values)
            for name, values in by_set.items() if len(values) >= min_sample}


def _multiple_reasons(entry: dict, quote: Quote, medians: dict,
                      thresholds) -> list[str]:
    """Flag a graded/raw multiple far out of step with the card's set."""
    median = medians.get(entry.get("set_name") or "?")
    if not median or not quote.raw or not quote.psa9 or quote.raw <= 0:
        return []
    multiple = quote.psa9 / quote.raw
    if multiple > median * thresholds.set_multiple_factor:
        return [f"PSA 9 is {multiple:.1f}x raw, against {median:.1f}x "
                f"typical for {entry.get('set_name')}"]
    return []


def _gap_pairs(raw: list, psa9: list, days: int = 90) -> list[list]:
    """[[raw, psa9], ...] for every priced day in the window, oldest first.

    Not downsampled: the page recomputes the worst case from these, and a
    sampled series would quietly miss the dip that makes a card risky.
    """
    from . import trends
    return [[round(r, 2), round(g, 2)]
            for _, r, g in trends.gap_inputs(raw, psa9, days=days)]


def _attach_trends(rows: list[dict], cfg: Config) -> int:
    """Fold price-history analytics onto the ranked rows.

    Silently does nothing when there is no database yet, so the free-tier
    workflow is unaffected.
    """
    from . import db, trends
    if not db.PATH.exists():
        return 0
    enriched = 0
    with db.session() as conn:
        for row in rows:
            raw = db.series(conn, row["id"], "raw")
            psa9 = db.series(conn, row["id"], "psa9")
            if len(raw) < 2 and len(psa9) < 2:
                continue
            psa10 = db.series(conn, row["id"], "psa10")
            floor = trends.gap_series(raw, psa9, cfg.econ.all_in, cfg.econ.net_proceeds)
            row.update(trends.summarise(raw, psa9, psa10, floor,
                                        cfg.thresholds.min_floor_profit))
            # No separate floor history: gap_points carries the same shape and
            # lets the page cost it under the user's own settings, so the
            # sparkline and the worst case can never disagree with the table.
            # Weekly (raw, psa9) pairs for the last 90 days, so the page can
            # recompute the worst-case floor under the user's own cost
            # assumptions instead of trusting a number baked at rank time.
            row["gap_points"] = _gap_pairs(raw, psa9, days=90)
            enriched += 1
    return enriched


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

    # Read every quote once: the set-level comparison below needs the whole set
    # before any single card can be judged against it.
    priced: list[tuple[dict, Quote]] = []
    for entry in universe.values():
        cached = quotes[entry["id"]] if quotes is not None else store.load_quote(entry["id"])
        if not cached or cached.get("miss") or not cached.get("quote"):
            continue
        priced.append((dict(entry, _fetched_at=cached.get("fetched_at")),
                       Quote(**cached["quote"])))

    set_medians = _set_multiples(priced, cfg.thresholds.min_set_sample)

    for entry, quote in priced:
        cached = {"fetched_at": entry.get("_fetched_at")}
        # A real population report if we have one, otherwise the free proxy.
        mix = (mix_from_population(quote.population)
               or mix_from_sales(quote.psa_sales_mix, cfg.thresholds.min_mix_sample,
                                 cfg.thresholds.sales_mix_min_low))
        multiple_reasons = _multiple_reasons(entry, quote, set_medians, cfg.thresholds)
        verdict = evaluate(quote, cfg.econ, cfg.thresholds, mix=mix,
                           extra_reasons=multiple_reasons)
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
            "psa9_outlier": quote.psa9_outlier,
            # The page cannot compute this: it needs the whole set.
            "multiple_outlier": bool(multiple_reasons),
            "variant_spread": quote.variant_spread,
            "printings": quote.printings,
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
            "ev_profit": verdict.ev_profit,
            "sales_per_month": verdict.sales_per_month,
            "months_to_sell": verdict.months_to_sell,
            "capital_months": verdict.capital_months,
            "floor_per_month": verdict.floor_per_month,
            "gem_rate": verdict.gem_rate,
            "mix_source": verdict.mix_source,
            "mix_sample": verdict.mix_sample,
            "psa8": quote.psa8,
            # Full precision: the page recomputes EV from these, and rounding
            # here put it a few cents out on high-priced cards.
            "p10": mix.p10 if mix else None,
            "p9": mix.p9 if mix else None,
            "p_low": mix.p_low if mix else None,
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

    # Cards cached before variant detection existed carry no printings at all,
    # so the check silently cannot fire on them. Say so rather than leaving it
    # to be inferred from an unchanged ranking.
    stale_variants = sum(1 for _, quote in priced if quote.printings is None)

    with_trends = _attach_trends(rows, cfg)

    # Scored last: conviction reads the trend fields, so it has to run after
    # the history is folded in.
    from .scoring import score as conviction_score
    for row in rows:
        result = conviction_score(row, cfg.scoring)
        row["conviction"] = result["conviction"]
        row["conviction_parts"] = result["parts"]
        row["conviction_coverage"] = result["coverage"]

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1

    return {
        "generated_at": iso(utcnow()),
        "config": cfg.to_dict(),
        "coverage": coverage(universe, store, cfg, quotes=quotes),
        "verdict_counts": counts,
        "trend_coverage": with_trends,
        "stale_variant_data": stale_variants,
        "scoring": {"weights": cfg.scoring.weights,
                    "roi_full": cfg.scoring.roi_full,
                    "depth_full": cfg.scoring.depth_full,
                    "liquidity_full": cfg.scoring.liquidity_full,
                    "direction_span": cfg.scoring.direction_span,
                    "max_sale_age_days": cfg.scoring.max_sale_age_days,
                    "unconfident_multiplier": cfg.scoring.unconfident_multiplier},
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
