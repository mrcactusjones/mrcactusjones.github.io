"""Trend analytics over price history.

These are the questions a time series can answer that a snapshot cannot: has
this gap actually held, is it widening or closing, and is the movement real or
just noise. Pure functions over (date, price) series so they are testable
without a database.
"""
from __future__ import annotations

import statistics
from datetime import date, datetime, timedelta
from typing import Optional, Sequence

Point = tuple[str, float]


def _as_date(stamp: str) -> date:
    return datetime.fromisoformat(str(stamp)[:10]).date()


def window(points: Sequence[Point], days: int, today: date | None = None) -> list[Point]:
    """The last `days` of a series, oldest first."""
    if not points:
        return []
    today = today or _as_date(points[-1][0])
    cutoff = today - timedelta(days=days)
    return [p for p in points if _as_date(p[0]) >= cutoff]


def _edge(values: Sequence[float], size: int = 3) -> float:
    """Median of the first/last few points, so one odd sale can't set the trend."""
    return statistics.median(values[:size]) if values else 0.0


def change_pct(points: Sequence[Point], days: int = 30,
               today: date | None = None) -> Optional[float]:
    """Percent change across the window, comparing ends by median.

    Needs at least four points: with two, "trend" is just the noise between
    two sales.
    """
    recent = window(points, days, today)
    if len(recent) < 4:
        return None
    values = [v for _, v in recent]
    start, end = _edge(values), _edge(list(reversed(values)))
    if start <= 0:
        return None
    return (end - start) / start


def volatility(points: Sequence[Point], days: int = 90,
               today: date | None = None) -> Optional[float]:
    """Standard deviation of day-over-day percent moves."""
    recent = window(points, days, today)
    if len(recent) < 5:
        return None
    values = [v for _, v in recent]
    moves = [(b - a) / a for a, b in zip(values, values[1:]) if a > 0]
    if len(moves) < 4:
        return None
    return statistics.pstdev(moves)


def gap_series(raw: Sequence[Point], graded: Sequence[Point],
               all_in_fn, net_fn) -> list[Point]:
    """Floor profit per day, on days where both a raw and a graded price exist.

    Prices are only carried forward from the most recent earlier observation --
    never interpolated backwards, which would invent a gap before the market
    showed one.
    """
    raw_by_date = dict(raw)
    out: list[Point] = []
    last_raw: float | None = None
    for stamp in sorted({d for d, _ in raw} | {d for d, _ in graded}):
        if stamp in raw_by_date:
            last_raw = raw_by_date[stamp]
        graded_now = dict(graded).get(stamp)
        if last_raw is None or graded_now is None:
            continue
        out.append((stamp, net_fn(graded_now) - all_in_fn(last_raw)))
    return out


def held_days(points: Sequence[Point], threshold: float, days: int = 90,
              today: date | None = None) -> int:
    """Observations in the window at or above the threshold.

    Counts observations, not calendar days: with a price every third day, 31
    of 31 observations clearing the bar is a full window, and reporting "31
    days" would understate it. Pair it with `observations` for the denominator.
    """
    return sum(1 for _, v in window(points, days, today) if v >= threshold)


def observations(points: Sequence[Point], days: int = 90,
                 today: date | None = None) -> int:
    """How many observations exist in the window at all."""
    return len(window(points, days, today))


def current_streak(points: Sequence[Point], threshold: float) -> int:
    """Calendar days spanned by the unbroken run at the end of the series.

    Measured in days rather than observations, so an irregularly sampled
    series cannot report a longer streak than actually elapsed.
    """
    run: list[Point] = []
    for point in reversed(list(points)):
        if point[1] < threshold:
            break
        run.append(point)
    if not run:
        return 0
    first, last = _as_date(run[-1][0]), _as_date(run[0][0])
    return (last - first).days + 1


def divergence(raw: Sequence[Point], graded: Sequence[Point], days: int = 30,
               today: date | None = None) -> Optional[float]:
    """Graded momentum minus raw momentum.

    Positive means the graded price is pulling away from raw -- the gap is
    widening on its own, rather than because raw got cheap. Negative means raw
    is catching up and the trade is closing.
    """
    graded_move = change_pct(graded, days, today)
    raw_move = change_pct(raw, days, today)
    if graded_move is None or raw_move is None:
        return None
    return graded_move - raw_move


def summarise(raw: Sequence[Point], psa9: Sequence[Point], psa10: Sequence[Point],
              floor: Sequence[Point], threshold: float,
              today: date | None = None) -> dict:
    """Everything the dashboard shows for one card, in one pass."""
    return {
        "raw_30d": change_pct(raw, 30, today),
        "psa9_30d": change_pct(psa9, 30, today),
        "psa9_90d": change_pct(psa9, 90, today),
        "psa10_30d": change_pct(psa10, 30, today),
        "divergence_30d": divergence(raw, psa9, 30, today),
        "psa9_volatility": volatility(psa9, 90, today),
        "floor_days_held_90d": held_days(floor, threshold, 90, today),
        "floor_observations_90d": observations(floor, 90, today),
        "floor_streak": current_streak(floor, threshold),
        "floor_points": len(floor),
        "history_days": len({d for d, _ in raw} | {d for d, _ in psa9}),
    }
