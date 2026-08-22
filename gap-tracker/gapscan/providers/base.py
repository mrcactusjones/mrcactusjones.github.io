"""Provider interface for graded price lookups."""
from __future__ import annotations

from typing import Protocol

from ..econ import Quote


class PriceProvider(Protocol):
    name: str
    credits_per_card: int

    def fetch(self, card: dict) -> Quote | None:
        """Return a Quote for one universe entry, or None if not found."""
        ...
