"""On-disk cache and history.

Credits are the scarce resource and disk is free, so nothing fetched is ever
thrown away: every quote is cached with a timestamp and re-read until its TTL
expires.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator

from .config import DATA


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def age_days(stamp: str | None) -> float:
    if not stamp:
        return float("inf")
    try:
        then = datetime.fromisoformat(stamp)
    except ValueError:
        return float("inf")
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (utcnow() - then).total_seconds() / 86400.0


def _atomic_write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp.replace(path)


class Store:
    def __init__(self, root: Path | None = None):
        self.root = root or DATA
        self.cards = self.root / "cards"
        self.history = self.root / "history"

    # ---- universe (the candidate list) -------------------------------
    @property
    def universe_path(self) -> Path:
        return self.root / "universe.json"

    def load_universe(self) -> dict:
        if not self.universe_path.exists():
            return {}
        return json.loads(self.universe_path.read_text()).get("cards", {})

    def save_universe(self, cards: dict, meta: dict | None = None) -> None:
        _atomic_write(self.universe_path,
                      {"generated_at": iso(utcnow()), "meta": meta or {}, "cards": cards})

    # ---- per-card quote cache ----------------------------------------
    def _card_path(self, card_id: str) -> Path:
        safe = card_id.replace("/", "_")
        return self.cards / f"{safe}.json"

    def load_quote(self, card_id: str) -> dict | None:
        path = self._card_path(card_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return None  # corrupt cache entry: treat as a miss and refetch

    def save_quote(self, card_id: str, record: dict) -> None:
        _atomic_write(self._card_path(card_id), record)

    def all_quotes(self) -> Iterator[tuple[str, dict]]:
        if not self.cards.exists():
            return
        for path in sorted(self.cards.glob("*.json")):
            try:
                yield path.stem, json.loads(path.read_text())
            except json.JSONDecodeError:
                continue

    # ---- outputs ------------------------------------------------------
    def save_rankings(self, payload: dict) -> Path:
        out = self.root / "rankings.json"
        _atomic_write(out, payload)
        return out

    def save_snapshot(self, payload: dict) -> Path:
        """One dated snapshot per day; re-running the same day overwrites it.

        Filed under the *local* date, matching the `date.today()` that every
        trend window in `rank` is anchored on. Filed by UTC these disagreed:
        west of Greenwich an evening run lands on tomorrow's UTC date, so an
        evening run and the next morning's overwrite each other, one local day
        straddles two files, and the dates `diff` prints are days you never
        think in.
        """
        day = date.today().isoformat()
        out = self.history / f"{day}.json"
        slim = [
            {k: row[k] for k in ("id", "floor_profit", "upside_profit", "verdict",
                                 "raw", "psa9", "psa10") if k in row}
            for row in payload.get("rows", [])
        ]
        _atomic_write(out, {"date": day, "rows": slim})
        return out

    def snapshot_dates(self) -> list[str]:
        """Every day a ranking was snapshotted, oldest first."""
        if not self.history.exists():
            return []
        return sorted(path.stem for path in self.history.glob("*.json"))

    def load_snapshot(self, day: str) -> dict[str, dict]:
        """One day's ranking, card id -> its row.

        `load_history` keeps only the floor, which is all the streak needs.
        A day-over-day comparison needs the verdict too, so it reads the file.
        """
        path = self.history / f"{day}.json"
        if not path.exists():
            return {}
        try:
            blob = json.loads(path.read_text())
        except json.JSONDecodeError:
            return {}
        return {row["id"]: row for row in blob.get("rows", []) if row.get("id")}

    def load_history(self) -> dict[str, list[dict]]:
        """card_id -> [{date, floor_profit, ...}] oldest first."""
        series: dict[str, list[dict]] = {}
        if not self.history.exists():
            return series
        for path in sorted(self.history.glob("*.json")):
            try:
                blob = json.loads(path.read_text())
            except json.JSONDecodeError:
                continue
            for row in blob.get("rows", []):
                cid = row.get("id")
                if cid is None:
                    continue
                series.setdefault(cid, []).append(
                    {"date": blob.get("date"), "floor_profit": row.get("floor_profit")})
        return series
