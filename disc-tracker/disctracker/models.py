"""Shared data structures and helpers. Everything else imports from here."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field


@dataclass
class RawVariant:
    variant_id: int
    title: str = ""
    sku: str = ""
    price_cents: int = 0
    compare_at_cents: int | None = None
    available: bool = False
    grams: int | None = None


@dataclass
class RawProduct:
    """One product as returned by a store, normalised across platforms."""

    product_id: int
    handle: str
    title: str
    vendor: str = ""
    product_type: str = ""
    tags: list[str] = field(default_factory=list)
    url: str = ""
    variants: list[RawVariant] = field(default_factory=list)


@dataclass
class ParsedListing:
    """Result of identifying which disc a listing is. See DESIGN.md section 4."""

    status: str = "unparsed"  # matched | review | ignored | unparsed
    manufacturer: str = ""
    mold: str = ""
    plastic: str = ""
    edition: str = ""  # controlled vocabulary, e.g. "tour series", "first run"
    player: str = ""
    year: int | None = None
    condition: str = "new"  # new | used
    grade: float | None = None  # 1-10 "sleepy scale" when stated
    disc_type: str = ""  # Distance Driver, Putter, ...
    flags: list[str] = field(default_factory=list)  # oop, ink, dyed, prototype, ...
    confidence: float = 0.0  # 0..1


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return text


def disc_key(p: ParsedListing) -> str | None:
    """Canonical identity: manufacturer|mold|plastic|edition|player (lowercase).

    Only matched listings get a key. Empty parts are kept so the key can be
    split back into five fields.
    """
    if p.status != "matched" or not p.manufacturer or not p.mold:
        return None
    parts = [p.manufacturer, p.mold, p.plastic, p.edition, p.player]
    return "|".join(" ".join(x.lower().split()) for x in parts)


def key_slug(key: str) -> str:
    """URL/file-safe slug for a disc key, unique per key."""
    return slugify(" ".join(x for x in key.split("|") if x))
