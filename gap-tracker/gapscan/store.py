"""On-disk cache and history.

Credits are the scarce resource and disk is free, so nothing fetched is ever
thrown away: every quote is cached with a timestamp and re-read until its TTL
expires.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
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
        """One dated snapshot per day; re-running the same day overwrites it."""
        day = utcnow().date().isoformat()
        out = self.history / f"{day}.json"
        slim = [
            {k: row[k] for k in ("id", "floor_profit", "upside_profit", "verdict",
                                 "raw", "psa9", "psa10") if k in row}
            for row in payload.get("rows", [])
        ]
        _atomic_write(out, {"date": day, "rows": slim})
        return out

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
