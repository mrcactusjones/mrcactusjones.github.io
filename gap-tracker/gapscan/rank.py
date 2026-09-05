"""Turn cached quotes into a ranking, and promote the top cards to the watchlist."""
from __future__ import annotations

from datetime import date

from .config import Config
from .econ import (Quote, days_since, evaluate, mix_from_population,
                   mix_from_sales)
from .scan import coverage
from .store import Store, age_days, iso, utcnow

# The window the split is judged over, matching the trend analytics.
SPLIT_WINDOW_DAYS = 90


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


def _gap_pairs(raw: list, psa9: list, days: int = 90,
               today: date | None = None) -> list[list]:
    """[[raw, psa9], ...] for every priced day in the window, oldest first.

    Not downsampled: the page recomputes the worst case from these, and a
    sampled series would quietly miss the dip that makes a card risky.

    `today` must match the one `summarise` used. The page derives its worst
    case from these pairs and Python derives its own from the floor series; a
    different anchor gives the two a different window, and they disagree about
    the number the tool exists to report.
    """
    from . import trends
    return [[round(r, 2), round(g, 2)]
            for _, r, g in trends.gap_inputs(raw, psa9, days=days, today=today)]


def _comps_splits(priced: list, cfg: Config) -> dict:
    """card_id -> CompsSplit for every card whose graded sales are two cards.

    Reads the stored PSA 9 sales rather than the quote, because a split is only
    visible across a series of sales -- the provider hands us one blended
    number that hides it.

    Silently returns nothing when there is no database, so the free-tier
    workflow is unaffected, exactly as `_attach_trends` already guards.
    """
    from . import db, trends
    if not db.PATH.exists():
        return {}
    splits = {}
    today = date.today()
    with db.session() as conn:
        for entry, _ in priced:
            # Real sales only. A snapshot is the provider's blended figure --
            # the very number a split is hiding inside -- and one lands in the
            # series on every run, so including them would let the detector
            # cut the clusters at a point that is not data, then stop firing
            # altogether once enough of them piled up.
            sales, _ = db.sales_series(conn, entry["id"], "psa9")
            # The recent window, not the whole series: a cluster median drawn
            # from sales a year old is not a price you can transact at today.
            recent = trends.window(sales, SPLIT_WINDOW_DAYS, today)
            split = trends.comps_split(
                [v for _, v in recent], cfg.thresholds.comps_split_spread,
                cfg.thresholds.comps_split_min_share,
                cfg.thresholds.comps_split_min_sample)
            if split is not None:
                splits[entry["id"]] = split
    return splits


def cheap_variant_price(quoted: float, split) -> float:
    """What a common copy of a two-printing card actually fetches.

    You buy a raw copy at the raw price, which is the common printing, so the
    cheap cluster is what you can count on -- not the blend, which is a number
    no copy sells for.

    Never upward: the cluster is a median of past sales while the quote is the
    provider's current figure, and taking the higher of the two would let a
    contamination warning inflate a floor. That is the opposite of the point.
    """
    return round(min(quoted, split.low), 2)


def _attach_trends(rows: list[dict], cfg: Config, splits: dict | None = None) -> int:
    """Fold price-history analytics onto the ranked rows.

    Silently does nothing when there is no database yet, so the free-tier
    workflow is unaffected.
    """
    from . import db, trends
    if not db.PATH.exists():
        return 0
    enriched = 0
    today = date.today()
    with db.session() as conn:
        for row in rows:
            # Raw history is already a daily market price, the same kind of
            # measurement as its snapshot, so it is read whole. The graded
            # series is sales, and mixing the blended snapshot into those
            # flattens every trend computed from them.
            raw = db.series(conn, row["id"], "raw")
            psa9, _ = db.sales_series(conn, row["id"], "psa9")
            split = (splits or {}).get(row["id"])
            if split is not None:
                # Keep only the cheap variant's sales. The headline floor is
                # priced from them, so the floor history, the worst case and
                # the sparkline have to be too -- otherwise the page and the
                # table describe two different cards.
                psa9 = [p for p in psa9 if p[1] <= split.boundary]
            if len(raw) < 2 and len(psa9) < 2:
                continue
            psa10, _ = db.sales_series(conn, row["id"], "psa10")
            floor = trends.gap_series(raw, psa9, cfg.econ.all_in, cfg.econ.net_proceeds)
            # One anchor for every window. Left to itself each series anchors
            # on its own last observation, so a raw series ending today and a
            # psa9 series ending at its last sale describe different spans --
            # and `divergence` subtracts one from the other.
            row.update(trends.summarise(raw, psa9, psa10, floor,
                                        cfg.thresholds.min_floor_profit,
                                        today=today))
            # No separate floor history: gap_points carries the same shape and
            # lets the page cost it under the user's own settings, so the
            # sparkline and the worst case can never disagree with the table.
            # Weekly (raw, psa9) pairs for the last 90 days, so the page can
            # recompute the worst-case floor under the user's own cost
            # assumptions instead of trusting a number baked at rank time.
            row["gap_points"] = _gap_pairs(raw, psa9, days=90, today=today)
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
    splits = _comps_splits(priced, cfg)

    for entry, quote in priced:
        cached = {"fetched_at": entry.get("_fetched_at")}
        # Two printings pooled into one graded price. You buy a raw copy at the
        # raw price, which is the common printing, so the cheap cluster is what
        # you can actually count on -- price the floor from that and say so.
        split = splits.get(entry["id"])
        split_reasons = []
        blended = quote.psa9
        if split is not None and quote.psa9 is not None:
            quote.psa9 = cheap_variant_price(quote.psa9, split)
            split_reasons.append(
                f"graded sales split in two: {split.low_count} near "
                f"${split.low:,.0f} and {split.high_count} near ${split.high:,.0f}; "
                f"priced off the cheaper, not the ${blended:,.0f} blend")
        # A real population report if we have one, otherwise the free proxy.
        mix = (mix_from_population(quote.population)
               or mix_from_sales(quote.psa_sales_mix, cfg.thresholds.min_mix_sample,
                                 cfg.thresholds.sales_mix_min_low))
        # Judged after repricing, against medians built before it. The test is
        # one-sided (only a multiple far *above* the set's norm is flagged), so
        # a repriced card can only draw fewer flags, never a spurious one --
        # and it already carries the split reason.
        multiple_reasons = _multiple_reasons(entry, quote, set_medians, cfg.thresholds)
        verdict = evaluate(quote, cfg.econ, cfg.thresholds, mix=mix,
                           extra_reasons=multiple_reasons + split_reasons)
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
            # What the provider reported, and the two cards behind it.
            "comps_split": split is not None,
            "psa9_blended": blended if split is not None else None,
            "comps_split_low": split.low if split else None,
            "comps_split_high": split.high if split else None,
            "comps_split_counts": ([split.low_count, split.high_count]
                                   if split else None),
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
            "upside_known": verdict.upside_known,
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

    with_trends = _attach_trends(rows, cfg, splits)

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
        "comps_split_cards": sum(1 for row in rows if row.get("comps_split")),
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
