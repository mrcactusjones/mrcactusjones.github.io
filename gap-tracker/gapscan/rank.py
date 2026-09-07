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


def _split_for(conn, card_id: str, grade: str, cfg: Config, today: date):
    """(CompsSplit or None, sales seen in the window) for one card and grade."""
    from . import db, trends
    # Real sales only. A snapshot is the provider's blended figure -- the very
    # number a split is hiding inside -- and one lands in the series on every
    # run, so including them would let the detector cut the clusters at a point
    # that is not data, then stop firing altogether once enough of them piled up.
    sales, _ = db.sales_series(conn, card_id, grade)
    # The recent window, not the whole series: a cluster median drawn from sales
    # a year old is not a price you can transact at today.
    recent = trends.window(sales, SPLIT_WINDOW_DAYS, today)
    # A thin window is not evidence of one card, it is absence of evidence --
    # and sparse expensive cards are where a pooled price does the most damage.
    # Celebi had four sales in ninety days and sixteen in total, and the check
    # simply never ran.
    basis = recent if len(recent) >= cfg.thresholds.comps_split_min_sample else sales
    split = trends.comps_split(
        [v for _, v in basis], cfg.thresholds.comps_split_spread,
        cfg.thresholds.comps_split_min_share,
        cfg.thresholds.comps_split_min_sample,
        cfg.thresholds.comps_split_tail_spread)
    return split, len(recent)


def _comps_splits(priced: list, cfg: Config, observed: dict | None = None) -> dict:
    """card_id -> {"psa9": CompsSplit|None, "psa10": CompsSplit|None}.

    Reads the stored sales rather than the quote, because a split is only
    visible across a series of sales -- the provider hands us one blended
    number that hides it.

    Both grades, because PPT reads both out of the same eBay titles and a title
    carries no printing. If the 9s are two printings pooled, the 10s are too,
    and repricing only the 9 leaves the upside quoting a blend against a
    cheap-variant cost -- overstating the profit and understating the gem rate
    a card needs, on exactly the cards already known to be contaminated.

    Each grade is cut on its own series. The 9's boundary is an absolute dollar
    figure drawn from 9s, and 10s sit above it, so reusing it would file every
    10 under "dear".

    Silently returns nothing when there is no database, so the free-tier
    workflow is unaffected, exactly as `_attach_trends` already guards.
    """
    from . import db
    if not db.PATH.exists():
        return {}
    splits = {}
    observed = {} if observed is None else observed
    today = date.today()
    with db.session() as conn:
        for entry, _ in priced:
            nine, seen = _split_for(conn, entry["id"], "psa9", cfg, today)
            ten, _ = _split_for(conn, entry["id"], "psa10", cfg, today)
            if nine is not None or ten is not None:
                splits[entry["id"]] = {"psa9": nine, "psa10": ten}
            observed[entry["id"]] = seen
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


def upside_price(quoted: float | None, split, psa9: float | None) -> tuple:
    """The PSA 10 price to judge an upside on, and whether it is usable at all.

    Same rule as the floor: when the 10s are two printings pooled, take the
    cheap cluster, and never upward. You buy one raw copy of the common
    printing; if it grades a 10 it is a 10 of *that* printing.

    Returns (price, unusable). `unusable` means the cheap cluster of 10s came
    in below the PSA 9 price. A 10 is never worth less than a 9 of the same
    printing, so that says the two grades were cut across different
    populations -- most likely the 9s are pooled too and the detector missed
    them. Neither number can price an upside then, and the honest answer is
    that we do not know it, not a number picked from the two.
    """
    if split is None or quoted is None:
        return quoted, False
    priced = cheap_variant_price(quoted, split)
    if psa9 is not None and priced < psa9:
        return None, True
    return priced, False


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
            pair = (splits or {}).get(row["id"]) or {}
            split = pair.get("psa9")
            if split is not None:
                # Keep only the cheap variant's sales. The headline floor is
                # priced from them, so the floor history, the worst case and
                # the sparkline have to be too -- otherwise the page and the
                # table describe two different cards.
                psa9 = [p for p in psa9 if p[1] <= split.boundary]
            if len(raw) < 2 and len(psa9) < 2:
                continue
            psa10, _ = db.sales_series(conn, row["id"], "psa10")
            # Same cut on the 10s, for the same reason: psa10_30d and
            # divergence_30d otherwise average two populations and call the
            # result a trend.
            if pair.get("psa10") is not None:
                psa10 = [p for p in psa10 if p[1] <= pair["psa10"].boundary]
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
    observed_sales: dict[str, int] = {}
    splits = _comps_splits(priced, cfg, observed_sales)

    for entry, quote in priced:
        cached = {"fetched_at": entry.get("_fetched_at")}
        # Two printings pooled into one graded price. You buy a raw copy at the
        # raw price, which is the common printing, so the cheap cluster is what
        # you can actually count on -- price the floor from that and say so.
        # What we can see, against what the provider claims.
        quote.observed_sales_9 = observed_sales.get(entry["id"])
        pair = splits.get(entry["id"]) or {}
        split, split10 = pair.get("psa9"), pair.get("psa10")
        split_reasons = []
        blended = quote.psa9
        blended10 = quote.psa10
        if split is not None and quote.psa9 is not None:
            quote.psa9 = cheap_variant_price(quote.psa9, split)
            split_reasons.append(
                f"graded sales split in two: {split.low_count} near "
                f"${split.low:,.0f} and {split.high_count} near ${split.high:,.0f}; "
                f"priced off the cheaper, not the ${blended:,.0f} blend")
        # The 10 gets the same treatment, and deliberately adds no reason: the
        # ranking is floor-at-9, and a pooled 10 says nothing about whether the
        # 9 is clean. Label the upside, do not demote the floor -- the same call
        # already made for a card with no PSA 10 comps at all.
        quote.psa10, upside_unusable = upside_price(quote.psa10, split10, quote.psa9)
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
            "observed_sales_9": quote.observed_sales_9,
            "psa9_outlier": quote.psa9_outlier,
            # The page cannot compute this: it needs the whole set.
            "multiple_outlier": bool(multiple_reasons),
            "variant_spread": quote.variant_spread,
            # What the provider reported, and the two cards behind it.
            # `comps_split` stays the PSA 9's -- the page and `diff` read it as
            # the reason a floor was repriced, and that is still what it means.
            # The 10's split is reported alongside under its own names.
            "comps_split": split is not None,
            "psa9_blended": blended if split is not None else None,
            "comps_split_low": split.low if split else None,
            "comps_split_high": split.high if split else None,
            "comps_split_counts": ([split.low_count, split.high_count]
                                   if split else None),
            "comps_split_10": split10 is not None,
            "psa10_blended": blended10 if split10 is not None else None,
            "comps_split_10_low": split10.low if split10 else None,
            "comps_split_10_high": split10.high if split10 else None,
            "comps_split_10_counts": ([split10.low_count, split10.high_count]
                                      if split10 else None),
            # The cheap cluster of 10s came in under the PSA 9 price, so there
            # is no upside we can stand behind. Not a missing comp -- a
            # contradictory one, and worth saying differently.
            "upside_unusable": upside_unusable,
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
            "fee_headroom": verdict.fee_headroom,
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
        # Python's own answers at values no card in the data reaches, so the
        # page's copy of the fee model is checked across the whole table --
        # including above the top tier, where it withholds confidence.
        "fee_reference": [
            {"declared": v,
             "fee": round(cfg.econ.fee_for(v), 6),
             "above_range": cfg.econ.above_modelled_range(v),
             "headroom": (None if (h := cfg.econ.tier_headroom(v)) is None
                          else round(h, 9))}
            for v in (0.0, 100.0, 499.0, 500.0, 1499.0, 1500.0, 2499.0, 2500.0,
                      4999.0, 5000.0, 9999.0, 10000.0, 24999.0, 25000.0, 80000.0)
        ],
        "coverage": coverage(universe, store, cfg, quotes=quotes),
        "verdict_counts": counts,
        "trend_coverage": with_trends,
        "stale_variant_data": stale_variants,
        "comps_split_cards": sum(1 for row in rows if row.get("comps_split")),
        "comps_split_10_cards": sum(1 for row in rows if row.get("comps_split_10")),
        "upside_unusable_cards": sum(1 for row in rows if row.get("upside_unusable")),
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
