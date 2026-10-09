"""Listing title -> ParsedListing (DESIGN.md section 4).

Pure and deterministic: all vocabulary lives in ``data/*.json`` and is loaded and
indexed once at import time; ``parse_listing`` does no I/O.

A title is read in this order:
  1. decode stray HTML entities, fold accents / odd unicode / dash look-alikes, collapse
     blanks, and split into alphanumeric tokens;
  2. non-disc products (bags, baskets, apparel, cards, dog toys, minis...) and multi-disc
     listings (sets, packs, lots, pairs, "3x", "pick your disc"...) -> ``ignored``;
  3. condition, grade and flags; weights, years, grades and flight numbers are located and
     masked so they can never be mistaken for a plastic, a mold or a quantity;
  4. manufacturer: ``vendor`` first, then brand names found in the title;
  5. edition (and the player named next to a Tour/Team/Signature marker);
  6. mold: exact match on the space-insensitive joined form ("Roc 3" == "Roc3"),
     then a conservative rapidfuzz fallback; plastic: longest match in the
     manufacturer's plastics.

The central design rule is that a wrong ``matched`` is worse than a ``review``:
anything ambiguous, conflicting, only fuzzily matched, or that looks like a
sibling mold we do not know ("Roc 4", "Zone GT") is demoted to ``review``.

eBay titles (DESIGN.md section 10.5) are seller-written keyword soup. Extra rules keep them safe:
a lot, pair, bundle or "pick your disc" listing (``_multi_disc``) is never a single-disc price, and the
condition is read from the title, the eBay condition text and the ``condition:new`` / ``condition:used``
tag together (``_condition``: used wins over new). Listings that look like one disc but may be another are
``review``, not ``matched``: a second disc joined by "+" / "&" / "and" (``_joined_stranger``, ``_pick_exact``),
a longer sibling mold whose parts are apart (``_sibling_apart``: "SS Buzzz", "Buzzz Z SS"), a plastic with a
qualifier we do not list (``_qualified_plastic``: "Lucid Chameleon"), a listing that only resembles a disc
("compare to", "knockoff") and a title that talks about bidding.
"""
from __future__ import annotations

import html
import json
import os
import re
import unicodedata
from bisect import bisect_left
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Iterable

from rapidfuzz import fuzz, process

from .models import ParsedListing

PARSER_VERSION = 4  # bump whenever rules or anything under data/ change

DATA_DIR = Path(__file__).resolve().parent / "data"
MOLD_TYPES = ("Distance Driver", "Fairway Driver", "Midrange", "Putter", "Approach")

_FUZZY_MIN_LEN = 5       # shorter strings are too easy to confuse ("Roc" ~ "Rock")
_FUZZY_REVIEW = 85       # rapidfuzz ratio needed for a mold-ish candidate
_FUZZY_MATCHED = 90      # ratio needed before a fuzzy hit may be `matched`
_MIN_TAIL = 4            # shortest mold tail ("classic" of Aviar Classic) judged by a near-miss test
_TAIL_SCORE = 80         # rapidfuzz ratio of the next word against such a tail
_FOREIGN_MOLD_MIN_KEY = 5  # a second mold of another brand only counts when its name is this long ("Pure" may be a word)
_INFER_MIN_KEY = 5       # shortest mold key trusted without a brand or plastic to back it
_YEAR_MIN, _YEAR_MAX = 1990, 2035
_MAX_TITLE_CHARS = 2000   # real titles are < 200; this only bounds work on hostile input
_MAX_WORD_CHARS = 24      # longest word accepted as a fuzzy query or an unknown-mold guess
_MAX_TAGS = 50


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------

_TRANS = str.maketrans({
    "ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "ß": "ss", "đ": "d", "Đ": "D",
    "ł": "l", "Ł": "L", "œ": "oe", "Œ": "OE", "’": "'", "‘": "'",
    "“": '"', "”": '"', "–": "-", "—": "-", " ": " ",
    "​": "", "‌": "", "‍": "", "﻿": "",
})
# Every dash-like character is a plain hyphen; a wave dash is a tilde. (NFKD already
# folds the small and fullwidth hyphens, but not the Unicode hyphen / figure dash / minus.)
_TRANS.update({ord(c): "-" for c in "\u2010\u2011\u2012\u2015\u2212"})
_TRANS[0x301C] = "~"
# Sellers write "3x" with a multiplication sign, which NFKD leaves alone: read it as the word x
# (padded, so "Innova×Discraft" stays two words and "3×" becomes "3 x")
_TRANS.update({ord(c): " x " for c in "\u00d7\u2715\u2716\u2a2f"})
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
_WS_RE = re.compile(r"\s+")


def _fold(text: str) -> str:
    """ASCII-fold: strip accents, expand ligatures/fullwidth forms, drop invisibles."""
    if text.isascii():
        return text
    text = unicodedata.normalize("NFKD", text).translate(_TRANS)
    return "".join(c for c in text if not unicodedata.combining(c))


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(_fold(text).lower())


def _key(text: str) -> str:
    """Space/punctuation-insensitive identity: "Roc 3" == "Roc3" == "ROC-3"."""
    return "".join(_tokens(text))


class _Doc:
    """A folded title with token positions (so the original casing can be recovered)."""

    __slots__ = ("text", "lower_text", "raw", "low", "starts", "ends", "n")

    def __init__(self, text: str):
        self.text = _WS_RE.sub(" ", _fold(text))  # one blank per run: no pattern can scan a long gap
        self.lower_text = self.text.lower()
        matches = list(_TOKEN_RE.finditer(self.text))
        self.raw = [m.group() for m in matches]
        self.low = [r.lower() for r in self.raw]
        self.starts = [m.start() for m in matches]
        self.ends = [m.end() for m in matches]
        self.n = len(self.raw)

    def sep_after(self, i: int) -> str:
        """The characters between token i and token i+1 (e.g. " ", "'", "-")."""
        if i + 1 >= self.n:
            return ""
        return self.text[self.ends[i]:self.starts[i + 1]]


def _scan(low: list[str], masked: list[bool], index: dict, max_n: int) -> list[tuple[int, int, str]]:
    """Every (start, end, key) where low[start:end] joined equals a key of ``index``.

    Joining without spaces makes "Roc 3", "Roc3" and "Tee Bird" all hit; masked
    tokens (weights, brands, ...) are never part of a hit.
    """
    out: list[tuple[int, int, str]] = []
    n = len(low)
    for i in range(n):
        if masked[i]:
            continue
        key = ""
        for j in range(i, min(n, i + max_n)):
            if masked[j]:
                break
            key += low[j]
            if key in index:
                out.append((i, j + 1, key))
    return out


def _maximal(hits: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    """Keep the longest hit of any overlapping group (drops "Zone" inside "Zone OS")."""
    if len(hits) < 2:
        return hits
    taken = bytearray(max(h[1] for h in hits))
    keep: list[tuple[int, int, str]] = []
    for h in sorted(hits, key=lambda h: (-(h[1] - h[0]), -len(h[2]), h[0])):
        if not any(taken[h[0]:h[1]]):
            taken[h[0]:h[1]] = b"\x01" * (h[1] - h[0])
            keep.append(h)
    return sorted(keep)


def _trim_plastic_tail(hits: list[tuple[int, int, str]], low: list[str]) -> list[tuple[int, int, str]]:
    """Give a plastic word back when it only glued two spellings together: "Buzz Z" joins to
    "buzzz", but it is the Buzz mold in Z plastic. Only when the shorter hit is the same mold."""
    out = []
    for i, j, key in hits:
        if j - i >= 2 and low[j - 1] in _PLASTIC_ALL:
            short = "".join(low[i:j - 1])
            if short in _MOLD_IDX and _MOLD_IDX[short] == _MOLD_IDX[key]:
                out.append((i, j - 1, short))
                continue
        out.append((i, j, key))
    return out


def _mask(masked: list[bool], i: int, j: int) -> None:
    for k in range(i, j):
        masked[k] = True


def _mask_chars(doc: _Doc, masked: list[bool], start: int, end: int) -> None:
    """Mask every token overlapping the character range [start, end)."""
    for k in range(doc.n):
        if doc.starts[k] < end and start < doc.ends[k]:
            masked[k] = True


# --------------------------------------------------------------------------
# parse_weight
# --------------------------------------------------------------------------

_UNIT = r"(?:g|gr|gm|gms|gram|grams)"
_RANGE_SEP = r"(?:-|~|to|/)"
_W_SINGLE = re.compile(rf"(?<![\d.])(\d{{2,3}})(?:\.\d+)?\s*{_UNIT}(?![a-z])", re.I)
# "170-175g", "95-110g", "170g/175g", "172 to 173". The ends are \d{2,3}; a pair with no unit
# only counts when both look like weights, so a grade such as "10/10" is not a range.
_W_RANGE = re.compile(
    rf"(?<![\d.])(\d{{2,3}})(?:\.\d+)?(\s*{_UNIT}(?![a-z]))?\s*{_RANGE_SEP}\s*(\d{{2,3}})(?:\.\d+)?(\s*{_UNIT}(?![a-z]))?(?![\d])",
    re.I,
)
_W_BARE = re.compile(r"^\s*(\d{3})\s*$")
_MAX_WEIGHT_TEXT = 300  # a variant title is a few words; longer input is cut, not scanned


def _has_weight_range(s: str) -> bool:
    for m in _W_RANGE.finditer(s):
        three_a, three_b = len(m.group(1)) == 3, len(m.group(3)) == 3
        # both ends weight-sized, a unit on the far end ("95-110g"), or a unit up front and a
        # weight-sized far end ("170g-175"); "175g / 10/10" (weight, then a grade) is not a range
        if (three_a and three_b) or m.group(4) or (m.group(2) and three_b):
            return True
    return False


def parse_weight(text: str) -> int | None:
    """"173g" -> 173; a range ("170-175g"), no weight, or an implausible one -> None."""
    if not text or not isinstance(text, str):
        return None
    s = _fold(_WS_RE.sub(" ", text)[:_MAX_WEIGHT_TEXT])
    if _has_weight_range(s):
        return None
    found = {int(m.group(1)) for m in _W_SINGLE.finditer(s)}
    if not found:
        m = _W_BARE.match(s)  # variants are often just "173"
        if m:
            found = {int(m.group(1))}
    if len(found) != 1:
        return None
    w = found.pop()
    return w if 100 <= w <= 200 else None


# A token right after a mold name that signals a *different* mold we may not know
# ("Zone GT", "Roc 4"); seeing one demotes the match to `review`.
_VARIANT_SUFFIXES = frozenset("ss os gt sl xl max plus lite v2 v3 v4 ii iii iv supersoft".split())
_VARIANT_LETTERS = frozenset("xz")

# --------------------------------------------------------------------------
# Vocabulary: load data/*.json once and build the lookup indexes
# --------------------------------------------------------------------------

def _load(name: str) -> dict:
    with (DATA_DIR / name).open(encoding="utf-8") as f:
        return json.load(f)


@dataclass(frozen=True)
class _Mold:
    mfr: str
    name: str
    type: str
    key: str


@dataclass(frozen=True)
class _Plastic:
    name: str
    ntok: int
    glow: bool = False  # the line is glow-in-the-dark by itself, so a "Glow" in the title adds nothing


def _put_unique(index: dict, key: str, value, what: str) -> None:
    if not key:
        raise ValueError(f"{what}: empty key")
    if key in index and index[key] != value:
        raise ValueError(f"{what}: {key!r} is ambiguous ({index[key]!r} vs {value!r})")
    index[key] = value


def _max_tokens(texts: Iterable[str]) -> int:
    return max((len(_tokens(t)) for t in texts), default=1)


def _build_manufacturers():
    names: list[str] = []
    title_idx: dict[str, str] = {}
    vendor_idx: dict[str, str] = {}
    surface: list[str] = []
    words: set[str] = set()
    for m in _load("manufacturers.json")["manufacturers"]:
        name = m["name"]
        names.append(name)
        for alias in [name, *m.get("aliases", [])]:
            _put_unique(title_idx, _key(alias), name, "manufacturers")
            _put_unique(vendor_idx, _key(alias), name, "manufacturers")
            surface.append(alias)
            words.update(_tokens(alias))
        for alias in m.get("vendor_aliases", []):
            _put_unique(vendor_idx, _key(alias), name, "manufacturers(vendor)")
            surface.append(alias)
    return names, title_idx, vendor_idx, _max_tokens(surface), frozenset(words)


def _build_molds(manufacturers: list[str]):
    index: dict[str, list[_Mold]] = {}
    seen: set[tuple[str, str]] = set()
    surface: list[str] = []
    words: set[str] = set()
    for e in _load("molds.json")["molds"]:
        mfr, name, mtype = e["manufacturer"], e["mold"], e.get("type", "")
        if mfr not in manufacturers:
            raise ValueError(f"molds.json: unknown manufacturer {mfr!r}")
        if mtype != "" and mtype not in MOLD_TYPES:
            raise ValueError(f"molds.json: bad type {mtype!r} for {mfr} {name}")
        mold = _Mold(mfr, name, mtype, _key(name))
        if (mfr, mold.key) in seen:
            raise ValueError(f"molds.json: duplicate {mfr} {name}")
        seen.add((mfr, mold.key))
        for alias in [name, *e.get("aliases", [])]:
            bucket = index.setdefault(_key(alias), [])
            if any(m.mfr == mfr and m.name != name for m in bucket):
                raise ValueError(f"molds.json: alias {alias!r} collides within {mfr}")
            if mold not in bucket:
                bucket.append(mold)
            surface.append(alias)
            words.update(_tokens(alias))
    return {k: tuple(v) for k, v in index.items()}, _max_tokens(surface), frozenset(words)


def _build_plastics(manufacturers: list[str]):
    data = _load("plastics.json")

    def make(entries: list[dict], what: str) -> dict[str, _Plastic]:
        idx: dict[str, _Plastic] = {}
        for e in entries:
            for alias in [e["name"], *e.get("aliases", [])]:
                _put_unique(idx, _key(alias), _Plastic(e["name"], len(_tokens(alias)), bool(e.get("glow"))), what)
        return idx

    generic = make(data.get("generic", []), "plastics generic")
    per_mfr: dict[str | None, dict[str, _Plastic]] = {None: dict(generic)}
    own: dict[str, dict[str, _Plastic]] = {}
    everything: dict[str, bool] = dict.fromkeys(generic, True)
    surface: list[str] = []
    for e in data.get("generic", []):
        surface += [e["name"], *e.get("aliases", [])]
    for mfr, entries in data["manufacturers"].items():
        if mfr not in manufacturers:
            raise ValueError(f"plastics.json: unknown manufacturer {mfr!r}")
        own[mfr] = make(entries, f"plastics {mfr}")
        everything.update(dict.fromkeys(own[mfr], True))
        per_mfr[mfr] = {**generic, **own[mfr]}  # a manufacturer's own name wins over a generic one
        for e in entries:
            surface += [e["name"], *e.get("aliases", [])]
    return per_mfr, own, everything, _max_tokens(surface), frozenset(w for s in surface for w in _tokens(s))


def _build_editions():
    index: dict[str, tuple[int, str, bool]] = {}
    surface: list[str] = []
    for rank, e in enumerate(_load("editions.json")["editions"]):
        for alias in [e["name"], *e.get("aliases", [])]:
            _put_unique(index, _key(alias), (rank, e["name"], bool(e.get("player_marker"))), "editions")
            surface.append(alias)
    return index, _max_tokens(surface), frozenset(w for s in surface for w in _tokens(s))


_MFR_NAMES, _TITLE_MFR, _VENDOR_MFR, _MFR_MAXN, _MFR_WORDS = _build_manufacturers()
_MOLD_IDX, _MOLD_MAXN, _MOLD_WORDS = _build_molds(_MFR_NAMES)
_PLASTIC_IDX, _PLASTIC_OWN, _PLASTIC_ALL, _PLASTIC_MAXN, _PLASTIC_WORDS = _build_plastics(_MFR_NAMES)
_EDITION_IDX, _EDITION_MAXN, _EDITION_WORDS = _build_editions()


def _build_fuzzy() -> dict[str | None, dict[str, list[str]]]:
    """Mold keys the fuzzy fallback may compare against, bucketed by manufacturer
    and first letter so a lookup touches a handful of strings, not the catalogue.

    "Zone OS", "Nuke SS", "Challenger OS" are left out: "zones" is one edit from
    "zoneos" but is the plural of Zone, not a Zone OS. A typo in such a name still
    reaches its base mold ("Challengr SS"), which the trailing SS then demotes to review.
    """
    by_mfr: dict[str | None, set[str]] = {None: set()}
    for key, molds in _MOLD_IDX.items():
        if len(key) < _FUZZY_MIN_LEN or not key[0].isalpha():
            continue
        if all(len(toks) > 1 and toks[-1] in _VARIANT_SUFFIXES for toks in (_tokens(m.name) for m in molds)):
            continue
        by_mfr[None].add(key)
        for m in molds:
            by_mfr.setdefault(m.mfr, set()).add(key)
    out: dict[str | None, dict[str, list[str]]] = {}
    for mfr, keys in by_mfr.items():
        buckets: dict[str, list[str]] = {}
        for k in sorted(keys):
            buckets.setdefault(k[0], []).append(k)
        out[mfr] = buckets
    return out


_FUZZY_BUCKETS = _build_fuzzy()


def _build_extensions() -> dict[tuple[str, str], tuple[str, ...]]:
    """(manufacturer, mold key) -> the tails of that maker's longer molds that start with it
    ("aviar" -> "classic", "driver"), so a near-miss second word ("Aviar Classc") is noticed.
    Tails under four letters ("3", "x3", "os", "pro") are too short for a near-miss test."""
    keys: dict[str, set[str]] = {}
    for key, molds in _MOLD_IDX.items():
        for m in molds:
            keys.setdefault(m.mfr, set()).add(key)
    out: dict[tuple[str, str], tuple[str, ...]] = {}
    for mfr, ks in keys.items():
        for k in ks:
            tails = tuple(sorted({o[len(k):] for o in ks if len(o) >= len(k) + _MIN_TAIL and o.startswith(k)}))
            if tails:
                out[(mfr, k)] = tails
    return out


_MOLD_EXTENSIONS = _build_extensions()


# --------------------------------------------------------------------------
# Rules that live in code (documented in DESIGN.md section 4)
# --------------------------------------------------------------------------

# Whole words that mark a non-disc product when they appear in a title.
_IGNORE_TITLE_WORDS = frozenset("""
bag bags backpack backpacks tote totes basket baskets target targets cart carts
towel towels shirt shirts tshirt hoodie hoodies sweatshirt sweatshirts crewneck jacket jackets
beanie beanies hat hats visor visors snapback headband bandana socks sock shorts pants joggers
jersey jerseys polo leggings
sticker stickers decal decals patch patches lanyard keychain mug mugs tumbler koozie cooler
bottle bottles umbrella poster posters scorecard scorebook retriever net
giftcard giftcards mini minis marker markers ultimate ultrastar dog
bundle bundles combo mystery pack packs pk set sets
cap caps sweater sweaters sweatpants pullover sleeve sleeves wristband wristbands sweatband gaiter gaiters
glove gloves sunglasses shoe shoes belt belts pin pins banner banners coin coins pen pens pencil pencils
book books ebook case cases cover covers holder holders rack racks stand stands chalk
lot pair pairs duo trio dozen doz bulk wholesale haul stash grab surprise multipack
kit kits twin twins doubles triples quad quads quartet quartets
necklace necklaces pendant pendants bracelet bracelets earring earrings cufflinks keyring keyrings wallet wallets
purse coaster coasters pillow pillows clock clocks replica replicas miniature miniatures
painting paintings artwork sign signs holster holsters pouch pouches lamp lamps
toy toys pet pets puppy puppies fetch chew k9 canine
card cards tcg magazine magazines dvd dvds plaque plaques trophy trophies medal medals
ornament ornaments figurine figurines puzzle puzzles chain chains guidebook
discatcher discatchers
""".split())
# Multi-word phrases, written joined ("gift card" == "giftcard" == "gift-card").
_IGNORE_PHRASES = frozenset("""
giftcard giftcards giftcertificate egiftcard starterset beginnerset discset giftset
lotof lotsof packof boxof pairof shippingprotection packageprotection shippinginsurance
tanktop teepad teesign tradingcard ultrastar keyring wallart
""".split())
_MULTI_PACK_RE = re.compile(r"\d+pk")  # "3pk"
_WEIGHT_WORDS = frozenset(("wt", "weight", "weights"))
_BAG_FILLER = frozenset("a the plastic poly ziploc clear original protective sealed".split())   # "in a plastic bag"
_IGNORE_PHRASE_IDX = dict.fromkeys(_IGNORE_PHRASES, True)
_IGNORE_PHRASE_MAXN = 4
# product_type: any of these words marks a category that is not a disc.
_IGNORE_TYPE_WORDS = frozenset("""
bag bags backpack backpacks basket baskets apparel clothing shirt shirts hat hats towel towels
sticker stickers accessory accessories merch merchandise cart carts marker markers giftcard giftcards
gift lanyard lanyards bundle bundles
""".split())
# tags: only short, category-like labels count ("Bags", not "Bag Builder").
_IGNORE_TAG_EXACT = frozenset("""
bag basket hat towel sticker accessory giftcard apparel clothing merch merchandise
bags baskets hats towels stickers accessories giftcards backpacks carts markers shirts
discgolfbags discgolfbaskets discgolfaccessories discgolfapparel
""".split())
_IGNORE_TAG_MAX_TOKENS = 3


# Colours seller soup uses beyond the basic ones in _NOISE. Never a mold, a player or a second disc.
_COLOR_WORDS = frozenset("""
violet maroon burgundy magenta cyan navy turquoise lavender peach salmon mint coral cream bronze copper camo
camouflage ivory beige khaki olive indigo lilac amber crimson scarlet blush charcoal slate smoke smokey
chrome pewter
""".split())

# Words that are never mold names and never a useful "unknown mold" guess.
_NOISE = frozenset("""
a an and the of in on for with by from to at is it or as vs w x z
disc discs golf frisbee new nib nwt brand rare oop used grade graded sleepy beat pre owned preowned
ink inked signed dyed stamp stamped foil bottom top color colors colour colours assorted random
various choose pick your weight weights plastic series line edition limited run putter putters
driver drivers midrange mid range fairway distance approach putt pdga approved
red blue green yellow orange pink purple white black grey gray teal aqua lime clear tan brown
gold silver rainbow swirl swirly tie dye tiedye neon pastel opaque translucent
max min lbs oz mm cm g gr gm inc llc co ltd corp company
free shipping ship ships shipped stock instock sold out sale clearance deal deals offer offers condition excellent
great good best hot item items box open seller sellers choice low cyber monday friday holiday soon coming just
released release flight numbers number fast back order orders preorder quantity qty only very find hard made usa
vary varies varying like
unthrown thrown never nwot bnib bnwt bnip nip nos mint shape tested authentic official original genuine
vintage collectible collectable htf lbs grams gram once twice times
speed glide turn fade stable understable overstable hyzer flip anhyzer beefy lightweight
hot cold stamps
""".split()) | _COLOR_WORDS
_NAME_STOP = _NOISE | frozenset(
    "tour team signature first second third special anniversary prototype misprint glow glo gitd factory seconds "
    "awesome amazing beautiful gorgeous stunning perfect cool sweet nice pretty fantastic fabulous incredible "
    "unique custom colorful vibrant bright shiny wonderful lovely super".split())

_NOT_USED_RE = re.compile(
    r"\b(?:never|not|hasn'?t|has\s+not|haven'?t|have\s+not)(?:\s+been)?\s+(?:previously\s+)?(?:used|owned|thrown|played)\b"
    r"|\bunused\b"
    r"|\bused\s+(?:by|for|to)\b"          # "as used by Ricky Wysocki", "used for disc golf": not a condition
)
_USED_RE = re.compile(
    r"\b(?:used|pre[\s-]*owned|(?:second|2nd)[\s-]*hand|pre[\s-]*loved|beat[\s-]*in|beat[\s-]*up|well[\s-]*loved|sleepy"
    r"|previously\s+(?:owned|used|thrown|played|flown))\b"
)
# "Like new" is how sellers describe a used disc, but eBay's own condition (or an unthrown/NIB
# remark) outranks it: only a hard word, a grade or a `condition:used` tag beats those.
_SOFT_USED_RE = re.compile(
    r"\b(?:like[\s-]*new|near[\s-]*new|(?:excellent|great|good|very\s+good|fair|decent|nice)\s+(?:condition|shape)"
    r"|(?:lightly|gently|barely|slightly)\s+(?:thrown|flown|worn)|(?:light|minor|some)\s+wear"
    r"|flight[\s-]*test(?:ed)?|test[\s-]*(?:thrown|flown|flight)"
    r"|thrown\s+(?:only\s+)?(?:once|twice|\d+\s*x\b|\d+\s+times|a\s+(?:few|couple)\s+times|a\s+handful))\b"
)
_NEW_RE = re.compile(
    r"\b(?:nib|nwot|nwt|bnib|bnwt|bnip|brand[\s-]*new|un[\s-]*thrown|never(?:\s+been)?[\s-]*(?:thrown|used|played)"
    r"|not(?:\s+been)?\s+thrown|unused|new\s+(?:in|with|without)\s+(?:box|bag|package|packaging|tags?))\b"
)
_GRADE_RES = (
    # "9/10" but not the start of a date ("9/10/2025")
    re.compile(r"(?<![\d./])(\d{1,2}(?:\.\d)?)\s*/\s*10\b(?!\s*/\s*\d)"),
    re.compile(r"(?<![\d.])(\d{1,2}(?:\.\d)?)\s*(?:out\s+of|of)\s*10\b"),
    re.compile(r"\bgrade[d]?\s*[:=#]?\s*(\d{1,2}(?:\.\d)?)\b"),
    re.compile(r"\bsleepy(?:\s*scale)?\s*[:=#]?\s*(\d{1,2}(?:\.\d)?)\b"),
)
# Weights inside a title, found so they can be masked. A descending pair is not a range:
# in "Prodigy PA-3 200 - 175g" the 200 is the plastic, only "175g" is the weight.
_WEIGHT_RANGE_RE = re.compile(
    rf"(?<![\d.])(1\d\d|200)(?:\.\d)?(?:\s*{_UNIT})?\s*{_RANGE_SEP}\s*(1\d\d|200)(?:\.\d)?(?:\s*{_UNIT})?(?![\w])", re.I)
_WEIGHT_UNIT_RE = re.compile(rf"(?<![\d.])(?:1\d\d|200)(?:\.\d)?\s*{_UNIT}(?![a-z])", re.I)

# joined phrase -> flag
_FLAG_PHRASES = {
    "oop": "oop", "outofproduction": "oop", "discontinued": "oop",
    "ink": "ink", "inked": "ink", "inkd": "ink", "sharpie": "ink",
    "dyed": "dyed", "tiedye": "dyed", "tiedyed": "dyed", "hydrodip": "dyed", "hydrodipped": "dyed",
    "signed": "signed", "autographed": "signed", "autograph": "signed", "autographs": "signed",
    "prototype": "prototype", "prototypes": "prototype",
    "misprint": "stamped_error", "misstamp": "stamped_error", "misstamped": "stamped_error",
    "stamperror": "stamped_error", "errorstamp": "stamped_error", "wrongstamp": "stamped_error",
    "doublestamp": "stamped_error",
}
_FLAG_MAXN = 3
_EDITION_FLAGS = {"prototype": "prototype", "misprint": "stamped_error"}

_NAME_PARTICLES = frozenset("van von de del della di da la le mac mc st".split())
_PLAYER_MAX_WORDS = 2
_MAX_LEFTOVER_WORDS = 2

# Words that can never be part of a person's name. Mold words are deliberately
# allowed ("Eagle McMahon", "Hailey King"); see _find_player.
_HARD_WORDS = frozenset(_NAME_STOP | _PLASTIC_WORDS | _EDITION_WORDS | _MFR_WORDS)
_FUZZY_SKIP = frozenset(_NOISE | _PLASTIC_WORDS | _EDITION_WORDS | _MFR_WORDS)


# --- lots, pairs, bundles (DESIGN.md 10.5): never a single-disc price -----------------------------
_NUMBER_WORDS = frozenset("two three four five six seven eight nine ten eleven twelve dozen couple several both".split())
# "assorted", "random"...: a grab bag, unless they only describe ONE disc's colour or weight
_VAGUE_WORDS = frozenset(
    "assorted assortment assortments medley random various variety mixed miscellaneous misc multiple multi collection collections".split())
_ATTR_WORDS = frozenset("""
color colors colour colours weight weights wt wts stamp stamps plastic plastics dye dyes pattern patterns
shade shades size sizes colored coloured tone toned tones
""".split())
_COUNT_NOUNS = frozenset("pcs pc pieces piece ct count".split())
_PLURAL_DISCS = frozenset(("discs", "dics", "molds"))
_STAR_RATING = frozenset("seller sellers rated rating review reviews customer service quality".split())  # "5 Star Seller"
_COUNT_TOKEN_RE = re.compile(r"(\d{1,2})(?:pcs?|ct|pieces?|packs?)")
_PAREN_COUNT_RE = re.compile(r"[(\[{]\s*(\d{1,2})\s*[)\]}]")
# "Paul McBeth 6X Signature Series", "McBeth 6X Luna": a count of world titles, not six discs
_TITLES_WON_RE = re.compile(r"\b\d{1,2}x\b(?=\s+(?:signature|sig|series|world|champion|champ|edition|claw)\b)|(?<=mcbeth )\d{1,2}x\b")
_TIMES_VERBS = frozenset("thrown used flown played tested".split())   # "thrown 2x": twice, not two discs
_MAX_QTY_BACK = 4          # how far "discs" looks back for a count ("5 Innova Star discs")
_MAX_LEADING_COUNT = 20    # "3 Innova DX Aviar": a title that starts with a small number is a quantity
_MULTI_PHRASE_RE = re.compile(
    r"\b(?:pick|choose|select)\s+(?:your\s+|any\s+|a\s+|an\s+|the\s+)?(?:own\s+)?"
    r"(?:disc|discs|mold|molds|model|models|driver|putter|midrange|fairway|one|two|three|\d{1,2})\b"
    r"|\b(?:you|u)\s*(?:pick|choose|select)\b|\b(?:pick|choose|select)\s+(?:any|your\s+own|one\s+of)\b"
    r"|\b(?:your|ur)\s+(?:pick|choice|selection)\b(?!\s+(?:of\s+)?(?:color|colour|weight|wt|plastic|stamp|ink|dye|design))"
    r"|\bchoice\s+of\s+(?:\d{1,2}|disc|discs|mold|molds|model|models|any)\b"
    r"|\b(?:buy|get)\s+(?:\d{1,2}(?!\s*%)|one|two|three)\b"
    r"|\b(?:bonus|extra|free)\s+discs?\b(?!\s+golf)"      # "w/ bonus disc" (but not "Free Disc Golf Shipping")
    r"|\bbogo\b|\bmix\s*(?:and|&|n|\+)?\s*match\b"
    r"|(?<![\d.])\d{1,2}\s+for\s+\$?\d"
)
# Numbers that belong to a disc description and say nothing about quantity.
_FLIGHT_RE = re.compile(
    r"(?<![\w.])(\d{1,2})(?:\.\d)?\s*[/|,-]\s*(\d)(?:\.\d)?\s*[/|,-]\s*(-?\d)(?:\.\d)?\s*[/|,-]\s*(-?\d)(?:\.\d)?(?![\w.])")
_FLIGHT_LABEL_RE = re.compile(r"\b(?:speed|glide|turn|fade|stability)\s*[:=]?\s*[-+]?\d{1,2}(?:\.\d)?\b|\b\d{1,2}\s*speed\b")
_COLOR_PHRASE_RE = re.compile(r"\bhunter\s+green\b")   # a colour, not the Dynamic Discs Hunter
# "5 Star Seller", "Star Rated", "Top Rated Seller 5 Star": a seller rating, not Innova's Star plastic
_STAR_RATING_RE = re.compile(
    r"\b(?:[1-5][\s-]*)?stars?(?=\s+(?:seller|sellers|rated|rating|ratings|review|reviews|feedback|customer|service)\b)"
    r"|(?<=seller )[1-5][\s-]*stars?\b|(?<=rated )[1-5][\s-]*stars?\b|(?<=rating )[1-5][\s-]*stars?\b")
_SINGLE_RES = (
    re.compile(r"[(\[{]\s*1\s*[)\]}]"),                            # "(1)"
    re.compile(r"\b(?:qty|quantity|count)\s*[:=]?\s*1\b"),
    re.compile(r"#\s*\d+"),                                          # "#1 seller"
)
# "Destroyer Max Distance": a Max that is just marketing, not a "Destroyer Max" sibling mold
_MAX_PLAIN_AFTER = frozenset("weight weights wt distance dist dis glide power speed stability range".split())


# Listing boilerplate that follows a "+" or "&" without naming a second disc.
_SOUP_WORDS = frozenset("""
sealed tracking tracked insured insurance warranty guarantee guaranteed handling return returns packaging packaged
wrapped protected protection extras extra gift receipt invoice label tags tag bonus accessories accessory
more other others additional available options option choices styles style models model different
""".split())
_JOIN_CHARS_RE = re.compile(r"[&+/]")
_JOIN_WORD_RE = re.compile(r"\b(?:and|plus)\b")
_MAX_JOIN_GAP = 3   # words allowed between a joiner and the disc it joins to
_BENIGN = frozenset(_NOISE | _NAME_STOP | _HARD_WORDS | _MOLD_WORDS | _ATTR_WORDS | _VAGUE_WORDS | _NUMBER_WORDS
                    | _STAR_RATING | _COLOR_WORDS | _SOUP_WORDS | frozenset(_FLAG_PHRASES))


# "Compare to Innova Destroyer", "Destroyer knockoff": a listing that names a disc only to say what it
# resembles is somebody else's product, and must not carry the named disc's price.
_RESEMBLES_RE = re.compile(
    r"\b(?:compare[sd]?|comparable|similar|equivalent|alternatives?)\s+(?:to|with|for)\b"
    r"|\b(?:clones?|copy|copies|dupe|inspired|substitutes?|replacements?)\s+(?:of|by|for)\b|\binstead\s+of\b"
    r"|\b(?:knock[\s-]?offs?|rip[\s-]?offs?|look[\s-]?alikes?|dupes?|clones?|imitation|unbranded|counterfeit|fake)\b"
)
_RESEMBLES_HINTS = ("compar", "similar", "equivalent", "alternative", "clone", "copy", "copies", "dupe", "inspired",
                    "substitute", "replacement", "instead", "knock", "rip", "look", "imitation", "unbranded",
                    "counterfeit", "fake")


# Bidding language: the price of an auction is a bid, not an asking price. ebay.py already drops AUCTION
# listings; a title that still talks about bidding is not trusted as a single asking price either.
_AUCTION_RE = re.compile(r"\bauctions?\b|\bno\s+reserve\b|\breserve\s+price\b|\bbid(?:s|ding|ders?)?\b")
_AUCTION_HINTS = ("auction", "reserve", "bid")


# A cracked, cut or "for parts" disc is a used disc nobody can throw: its price says nothing about the
# disc and would set the floor of the used price. (eBay's own wording "For parts or not working" and
# "Damaged" arrive as the product type.) A negation before the word ("not cracked") is a boast, not a flaw.
_DAMAGED_RE = re.compile(
    r"(?<!\bnot )(?<!\bno )(?<!\bnever )(?<!\bwithout )(?<!\bnon-)\b(?:broken|cracked|snapped|shattered|damaged)\b"
    r"|\bcut\s+in\s+half\b|\bfor\s+parts\b|\bparts\s+only\b|\bnot\s+working\b"
)
_DAMAGED_HINTS = ("broken", "crack", "snap", "shatter", "damaged", "half", "parts", "not working")


# Words that qualify a plastic line ("Lucid Chameleon", "Star Shimmer", "K1 Hard"). A line we list that sits
# next to one of them, outside its own name, is a variant we do not know: the plain line's price would
# be the wrong one.
_PLASTIC_QUALIFIERS = frozenset("""
burst chameleon shimmer shimmery sparkle sparkly glitter glittery orbit ice icy overmold overmolded rubber
rubberized flx flex flexible soft medium firm hard metal metallic flake pearl pearlized marble marbled confetti
galaxy nebula frost frosted holo holographic splatter speckled lite
""".split())
# The qualifiers that are never anything else: they also count when they stand behind the mold
# ("P2 Flex 3", "Judge Chameleon"). The others ("soft", "hard", "ice", "medium", "metal") are ordinary words
# and only count right next to the plastic.
_STRONG_QUALIFIERS = frozenset("""
burst chameleon shimmer shimmery sparkle sparkly glitter glittery orbit overmold overmolded flx flex pearl
pearlized confetti galaxy nebula holo holographic
""".split())


def _qualified_plastic(doc: "_Doc", masked: list[bool], plastic_span: tuple[int, int],
                       mold_span: tuple[int, int]) -> bool:
    """Is the plastic we found directly next to a qualifier word that is not part of its name? Brand,
    weight, edition and similar tokens between them are stepped over; the mold is not. A strong
    qualifier is also noticed right behind the mold (the plastic stepped over)."""
    low, n = doc.low, doc.n
    for step, k in ((-1, plastic_span[0] - 1), (1, plastic_span[1])):
        while 0 <= k < n and masked[k]:
            k += step
        if 0 <= k < n and not (mold_span[0] <= k < mold_span[1]) and low[k] in _PLASTIC_QUALIFIERS:
            return True
    for step, k in ((-1, mold_span[0] - 1), (1, mold_span[1])):
        while 0 <= k < n and (masked[k] or plastic_span[0] <= k < plastic_span[1]):
            k += step
        if 0 <= k < n and low[k] in _STRONG_QUALIFIERS:
            return True
    return False


_LETTER_TAILS = frozenset("ss os gt sl xl".split())   # tails that are never ordinary words


def _sibling_apart(doc: "_Doc", masked: list[bool], mold: "_Mold", span: tuple[int, int],
                   plastic_span: tuple[int, int] | None) -> bool:
    """Is the nearest other word on either side of the mold (weights, brands and the plastic stepped over)
    the tail of a longer mold of the same maker? "SS Buzzz", "Pro Ballista", "Max D2", "Buzzz Z SS",
    "Aviar 170g Classic" all name Buzzz SS, Ballista Pro, D2 Max, Aviar Classic, which are other discs.
    A two-letter tail (SS, OS, GT) is also found behind up to three filler words ("Buzzz Midrange OS")."""
    low, n = doc.low, doc.n
    for step, k in ((1, span[1]), (-1, span[0] - 1)):
        skipped = 0
        while 0 <= k < n:
            if masked[k] or (plastic_span is not None and plastic_span[0] <= k < plastic_span[1]):
                k += step
                continue
            w = low[k]
            # ("Disc Golf Driver Innova Aviar": "driver" is listing boilerplate, not Aviar Driver)
            if w != "driver" and (skipped == 0 or w in _LETTER_TAILS) \
                    and any(m.mfr == mold.mfr for m in _MOLD_IDX.get(mold.key + w, ())):
                return True
            if w in _NOISE and skipped < 3:
                skipped += 1
                k += step
                continue
            break
    return False


def _joined(doc: "_Doc", first: tuple[int, int], second: tuple[int, int]) -> bool:
    """Do two token spans (in either order) sit within a few words of each other with a "+", "&", "/",
    "and" or "plus" between them?"""
    a, b = sorted((first, second))
    if b[0] - a[1] > _MAX_JOIN_GAP or a[1] > b[0]:
        return False
    gap = doc.lower_text[doc.ends[a[1] - 1]:doc.starts[b[0]]]
    return _JOIN_CHARS_RE.search(gap) is not None or _JOIN_WORD_RE.search(gap) is not None


def _joined_stranger(doc: "_Doc", evidence: list[bool]) -> bool:
    """"Star Destroyer + Sparrow", "Sparrow & Innova Star Destroyer": a plus, ampersand, slash, "and"
    or "plus" between a word that is part of the disc we recognised and a word we know nothing about is
    most likely a second disc whose mold is not in our list, so the listing is not a single disc.
    ``evidence`` marks the tokens already explained (mold, plastic, brand, edition, weights...)."""
    low, n = doc.low, doc.n

    def stranger(k: int) -> bool:
        return (0 <= k < n and not evidence[k] and low[k].isalpha()
                and 4 <= len(low[k]) <= _MAX_WORD_CHARS and low[k] not in _BENIGN)

    for k in range(n - 1):
        sep = doc.lower_text[doc.ends[k]:doc.starts[k + 1]]
        if _JOIN_CHARS_RE.search(sep):
            left, right = k, k + 1
        elif low[k + 1] in ("and", "plus") and k + 2 < n and not sep.strip(" ,"):
            left, right = k, k + 2
        else:
            continue
        if (evidence[left] and stranger(right)) or (stranger(left) and evidence[right]):
            return True
    return False


# --------------------------------------------------------------------------
# Detection helpers
# --------------------------------------------------------------------------

# (words that must appear in the text for the pattern to be worth running, pattern): most titles
# contain none of them, and this runs for every listing
_PRE_MASK_RES = (
    (("speed", "glide", "turn", "fade", "stability"), _FLIGHT_LABEL_RE),
    (("hunter",), _COLOR_PHRASE_RE),
    (("sig", "series", "world", "champ", "edition", "claw", "mcbeth"), _TITLES_WON_RE),
    (("(", "[", "{"), _SINGLE_RES[0]),
    (("qty", "quantity", "count"), _SINGLE_RES[1]),
    (("#",), _SINGLE_RES[2]),
    (("star",), _STAR_RATING_RE),
    (("10",), _GRADE_RES[0]),
    (("10",), _GRADE_RES[1]),
    (("grade",), _GRADE_RES[2]),
    (("sleepy",), _GRADE_RES[3]),
)


def _pre_mask(doc: _Doc) -> list[bool]:
    """Tokens that are numbers of a disc description (flight numbers, grades, "(1)", "#1") and so
    can never be a quantity, a mold digit or a variant."""
    masked = [False] * doc.n
    text = doc.lower_text
    if sum(text.count(c) for c in "/|,-") >= 3:  # a flight-number list has three separators
        pos = 0
        while True:
            m = _FLIGHT_RE.search(text, pos)
            if m is None:
                break
            speed, glide, turn, fade = (int(g) for g in m.groups())
            if 1 <= speed <= 15 and 1 <= glide <= 7 and -5 <= turn <= 2 and 0 <= fade <= 5:
                _mask_chars(doc, masked, m.start(), m.end())
                pos = m.end()
            else:
                pos = m.start() + 1  # "3 6/5/-1/1": the real numbers may start inside a failed match
    for needles, rx in _PRE_MASK_RES:
        if any(w in text for w in needles):
            for m in rx.finditer(text):
                _mask_chars(doc, masked, m.start(), m.end())
    return masked


def _looks_like_disc(doc: _Doc) -> bool:
    """Any disc evidence at all: the word "disc(s)", a brand, a mold or a plastic."""
    low, free = doc.low, [False] * doc.n
    return ("disc" in low or "discs" in low or bool(_scan(low, free, _TITLE_MFR, _MFR_MAXN))
            or bool(_scan(low, free, _MOLD_IDX, _MOLD_MAXN)) or bool(_scan(low, free, _PLASTIC_ALL, _PLASTIC_MAXN)))


def _multi_disc(doc: _Doc, masked: list[bool]) -> bool:
    """A lot, pair, bundle or pick-your-disc listing: "3x", "(2)", "x2", "lot", "2 discs", "pick any 3"...

    A number that completes a mold name ("Roc 3", "Aviar X 3", "Latitude 64") is never a quantity.
    """
    low, n = doc.low, doc.n

    def count_at(k: int) -> bool:
        w = low[k]
        return (w.isdigit() and len(w) <= 2 and int(w) >= 2 and not masked[k]
                and not (k and (low[k - 1] + w in _MOLD_IDX or low[k - 1] + w in _TITLE_MFR)))

    def count_after(k: int) -> bool:  # "Qty 3", "Quantity of 3", "Count: 3", "Total of 3"
        j = k + 1
        if j < n and low[j] == "of":
            j += 1
        return j < n and count_at(j)

    if n > 1 and count_at(0) and int(low[0]) <= _MAX_LEADING_COUNT and low[1].isalpha() \
            and not (low[1] == "star" and low[2:3] and low[2] in _STAR_RATING):
        return True
    for k, w in enumerate(low):
        if masked[k]:
            continue
        nxt = low[k + 1] if k + 1 < n else ""
        prv = low[k - 1] if k else ""
        if w[-1] == "x" and w[:-1].isdigit() and len(w) <= 3:      # "3x", but "thrown 2x" is a condition
            if int(w[:-1]) >= 2 and prv not in _TIMES_VERBS:
                return True
        elif w[0] == "x" and w[1:].isdigit() and len(w) <= 3:      # "x3", but not "Aviar X3"
            if int(w[1:]) >= 2 and prv + w not in _MOLD_IDX:
                return True
        elif w[:3] == "qty" and w[3:].isdigit() and int(w[3:]) >= 2:   # "Qty4"
            return True
        elif w == "total":                                          # "4 Total", "Total of 3", but not "2 Total Eclipse"
            if nxt != "eclipse" and ((k and count_at(k - 1)) or count_after(k)):
                return True
        elif w[0].isdigit() and _COUNT_TOKEN_RE.fullmatch(w):       # "2pc", "10ct"
            if int(_COUNT_TOKEN_RE.fullmatch(w).group(1)) >= 2:
                return True
        elif w == "x":                                              # "2 x", "x 2", but not "Aviar X 3"
            if nxt and count_at(k + 1) and prv + w + nxt not in _MOLD_IDX:
                return True
            if k and count_at(k - 1) and nxt not in ("out", "outs"):
                return True
        elif w in ("qty", "quantity"):
            if count_after(k):
                return True
        elif w in _COUNT_NOUNS:                                     # "5 pcs", "Count: 3"
            if (k and count_at(k - 1)) or (w == "count" and count_after(k)):
                return True
        elif w == "number" and nxt == "of":                         # "Number of discs: 3"
            j = k + 2 + (low[k + 2:k + 3] in (["disc"], ["discs"], ["pieces"], ["pcs"]))
            if j < n and count_at(j):
                return True
        elif w == "disc":                                           # "3 Disc Innova Set", "Two Disc Deal", but "Roc 3 Disc Golf"
            if k and nxt != "golf" and (count_at(k - 1) or low[k - 1] in _NUMBER_WORDS):
                return True
        elif w in _PLURAL_DISCS:                                    # "2 discs", "five Innova discs"
            if any("".join(low[max(0, k - span):k]) + w in _TITLE_MFR for span in range(1, 5)):
                continue                                            # "Dynamic Discs", "Latitude 64 Golf Discs"
            j = k - 1
            for _ in range(_MAX_QTY_BACK):
                if j < 0:
                    break
                if count_at(j) or low[j] in _NUMBER_WORDS:
                    return True
                if not low[j].isalpha():
                    break
                j -= 1
        elif w in _VAGUE_WORDS and nxt not in _ATTR_WORDS and _looks_like_disc(doc):
            return True
    for m in _PAREN_COUNT_RE.finditer(doc.text):                    # "(2) Discs"
        k = bisect_left(doc.starts, m.start(1))
        if k < n and doc.starts[k] == m.start(1) and count_at(k):
            return True
    return _MULTI_PHRASE_RE.search(doc.lower_text) is not None


def _in_a_bag(low: list[str], k: int) -> bool:
    """"new in bag", "in a plastic bag": the disc's packaging, not a disc bag (token k is the "bag")."""
    j = k - 1
    while j >= 0 and k - j <= 3 and low[j] in _BAG_FILLER:
        j -= 1
    return j >= 0 and low[j] == "in"


def _ignore_reason(doc: _Doc, product_type: str, tags: list[str], masked: list[bool]) -> str:
    """Why this is not a disc ("" when it might be one)."""
    pt = _tokens(product_type)
    if _IGNORE_TYPE_WORDS.intersection(pt):
        return "product_type"
    for tag in tags:
        tt = _tokens(tag)
        if tt and len(tt) <= _IGNORE_TAG_MAX_TOKENS and "".join(tt) in _IGNORE_TAG_EXACT:
            return "tag"
    low = doc.low
    # "Net Wt. 175g" / "net weight" is a weight, not a practice net; "new in bag" is how the disc
    # arrived, not a disc bag
    words = [w for k, w in enumerate(low) if not (
        (w == "net" and low[k + 1:k + 2] and low[k + 1] in _WEIGHT_WORDS)
        or (w in ("bag", "bags") and _in_a_bag(low, k)))]
    if _IGNORE_TITLE_WORDS.intersection(words):
        return "title"
    if _scan(low, [False] * len(low), _IGNORE_PHRASE_IDX, _IGNORE_PHRASE_MAXN):
        return "title"
    if any(_MULTI_PACK_RE.fullmatch(w) for w in low):
        return "title"
    if _multi_disc(doc, masked):
        return "title"
    return ""


# Cheap substring tests that let _condition skip regexes (it runs for every listing, mostly on
# titles that say nothing about condition).
_GRADE_HINTS = ("10", "grade", "sleepy")
_USED_HINTS = ("used", "owned", "hand", "loved", "beat", "sleepy", "previously")
_SOFT_HINTS = ("new", "condition", "shape", "thrown", "flown", "worn", "wear", "test", "flight")
_NEW_HINTS = ("nib", "nwot", "nwt", "bnip", "new", "thrown", "never", "not", "unused")


def _has(text: str, hints: tuple[str, ...]) -> bool:
    return any(h in text for h in hints)


# eBay's own condition wording (it arrives as the product type) that means "not new". The
# `condition:new` / `condition:used` tag comes from eBay's numeric id and outranks this; the text only
# decides when that tag is missing. "New", "New other", "New with defects" and "Open box" stay new.
_USED_CONDITION_WORDS = frozenset("refurbished damaged parts acceptable".split())
_USED_CONDITION_PHRASES = frozenset("good verygood excellent fair poor".split())


def _used_condition_text(ptype_l: str) -> bool:
    toks = _tokens(ptype_l)
    return bool(toks) and (bool(_USED_CONDITION_WORDS.intersection(toks)) or "".join(toks) in _USED_CONDITION_PHRASES)


def _condition(title_l: str, tags_l: list[str], ptype_l: str) -> tuple[str, float | None]:
    """("new"|"used", grade). Used wins over new:

    1. a ``condition:used`` tag (eBay's own field), a stated N/10 grade, or a word such as
       used / pre-owned / beat in / sleepy in the title, tags or condition text -> used;
    2. a soft remark ("like new", "thrown once") -> used, unless the ``condition:new`` tag or
       an unthrown / NIB / NWOT / brand new remark in the title speaks against it;
    3. otherwise new (the default, which "unthrown", "NIB" and "brand new" confirm).
    """
    title = title_l.replace("_", " ")                    # \b treats "_" as a letter
    tag_texts = [t.replace("_", " ") for t in tags_l]
    texts = [t for t in (title, *tag_texts, ptype_l.replace("_", " ")) if t.strip()]
    flat_tags = [_WS_RE.sub("", t) for t in tag_texts]
    tag_used, tag_new = "condition:used" in flat_tags, "condition:new" in flat_tags
    new_words = _has(title, _NEW_HINTS) and _NEW_RE.search(title) is not None
    for text in texts:
        if _has(text, _GRADE_HINTS):
            for rx in _GRADE_RES:
                m = rx.search(text)
                if m and 1 <= float(m.group(1)) <= 10:
                    # a perfect score next to unthrown / NIB is a description of the disc, not a grade
                    if float(m.group(1)) == 10 and new_words and not tag_used:
                        continue
                    return "used", float(m.group(1))
    if tag_used:
        return "used", None
    if not tag_new and _used_condition_text(ptype_l):
        return "used", None
    for text in texts:
        if _has(text, _USED_HINTS) and _USED_RE.search(_NOT_USED_RE.sub(" ", text)):
            return "used", None
    if not (tag_new or new_words) and any(_has(t, _SOFT_HINTS) and _SOFT_USED_RE.search(t) for t in texts):
        return "used", None
    return "new", None


def _flags(title_low: list[str], tags: list[str]) -> set[str]:
    found: set[str] = set()
    for toks in [title_low, *(_tokens(t) for t in tags)]:
        for _i, _j, key in _scan(toks, [False] * len(toks), _FLAG_PHRASES, _FLAG_MAXN):
            found.add(_FLAG_PHRASES[key])
    return found


def _name_case(word: str) -> str:
    if word.isupper() or word.islower():
        w = word.capitalize()
        if w.startswith("Mc") and len(w) > 2:
            w = "Mc" + w[2:].capitalize()
        return w
    return word  # mixed case ("McBeth", "DeVries") is already right


def _find_player(doc: _Doc, marker: tuple[int, int], masked: list[bool],
                 mold_spans: list[tuple[int, int, str]] = ()) -> tuple[str, tuple[int, int] | None]:
    """The person named beside a Tour/Team/Signature marker, as (name, token span).

    Conservative on purpose: two capitalised words that are not brand, plastic,
    edition or filler words, read straight off the side of the marker (a year or
    weight in between is skipped). A pair made only of mold words ("Ballista
    Pro") is not a person, and a pair that swallows the title's only mold
    ("Star Destroyer Wysocki Tour Series") is not either. Words glued by a
    hyphen or apostrophe ("Jones-Smith", "O'Brien") are one word.
    """
    letters = [c for c in doc.text if c.isalpha()]
    shouting = bool(letters) and sum(c.isupper() for c in letters) / len(letters) > 0.7
    flat = bool(letters) and not any(c.isupper() for c in letters)
    glue = ("'", "-")

    def glued_end(k: int) -> int:  # exclusive end of the glued run that starts at token k
        while k + 1 < doc.n and doc.sep_after(k) in glue:
            k += 1
        return k + 1

    def glued_start(k: int) -> int:  # start of the glued run that ends at token k
        while k > 0 and doc.sep_after(k - 1) in glue:
            k -= 1
        return k

    def name_like(a: int, b: int) -> bool:
        raw = doc.raw[a:b]
        # the one-letter piece of a glued word ("O" in "O'Brien") skips the vocabulary test
        if any(masked[k] or not w.isalpha() or (doc.low[k] in _HARD_WORDS and (len(w) > 1 or b - a == 1))
               for k, w in zip(range(a, b), raw)):
            return False
        if sum(map(len, raw)) < 2:
            return False
        return shouting or flat or raw[0][0].isupper()

    def collect(forward: bool) -> list[tuple[int, int]]:
        units: list[tuple[int, int]] = []
        k = marker[1] if forward else marker[0] - 1
        while 0 <= k < doc.n:
            a, b = (k, glued_end(k)) if forward else (glued_start(k), k + 1)
            if not units and masked[a] and not doc.low[a].isalpha():
                k = b if forward else a - 1  # a year / weight / grade between the name and the marker
                continue
            if not name_like(a, b):
                break
            units.append((a, b))
            k = b if forward else a - 1
        return units

    def is_particle(unit: tuple[int, int]) -> bool:
        return unit[1] - unit[0] == 1 and doc.low[unit[0]] in _NAME_PARTICLES

    for forward in (False, True):
        units = collect(forward)
        if len(units) < 2:
            continue
        take = _PLAYER_MAX_WORDS
        # "Vanessa Van Dyken", "Ricky De La Hoya": a particle at the far edge pulls in the next word
        while take < len(units) and take < _PLAYER_MAX_WORDS + 2 and is_particle(units[take - 1]):
            take += 1
        picked = sorted(units[:take])
        lo, hi = picked[0][0], picked[-1][1]
        words = range(lo, hi)
        if all(doc.low[k] in _MOLD_WORDS for k in words):
            continue
        covered = set(words)
        if any(covered.intersection(range(a, b)) for a, b, _k in mold_spans) and \
                not any(covered.isdisjoint(range(a, b)) for a, b, _k in mold_spans):
            continue  # the "name" holds the only mold in the title
        out: list[str] = []
        for pos, (a, b) in enumerate(picked):
            for k in range(a, b):
                out.append(_name_case(doc.raw[k]))
                if k + 1 < b:
                    out.append(doc.sep_after(k))
            if pos + 1 < len(picked):
                out.append(" ")
        return "".join(out), (lo, hi)
    return "", None


# --------------------------------------------------------------------------
# Mold resolution
# --------------------------------------------------------------------------

def _outside_plastics(pairs: list, low: list[str]) -> tuple[list, bool]:
    """Drop mold hits that are only part of a longer plastic name, if anything else is left.

    "Prime Burst Judge": Westside's Burst is just half of Dynamic's plastic, and
    in "Origio Burst Burst" only the second Burst is the mold. The flag says every hit was
    inside a plastic name ("Origio Burst Swoord"): then the real mold may be something else.
    """
    spans = _scan(low, [False] * len(low), _PLASTIC_ALL, _PLASTIC_MAXN)
    free = [p for p in pairs if not any(a <= p[0] and p[1] <= b and (b - a) > (p[1] - p[0]) for a, b, _ in spans)]
    return (free, False) if free else (pairs, True)


def _narrow(top: list, low: list[str], masked: list[bool]) -> list:
    """Break a tie between candidate molds using the plastic words in the title."""
    if len({(p[3].mfr, p[3].name) for p in top}) > 1:
        def backed(p) -> bool:  # does this mold's maker have a plastic elsewhere in the title?
            idx = _PLASTIC_OWN.get(p[3].mfr)
            if not idx:
                return False
            probe = list(masked)
            _mask(probe, p[0], p[1])
            return bool(_scan(low, probe, idx, _PLASTIC_MAXN))

        backed_pairs = [p for p in top if backed(p)]
        if backed_pairs and len(backed_pairs) < len(top):
            top = backed_pairs
    return top


def _pick_exact(doc: _Doc, hits: list[tuple[int, int, str]], vendor_mfr: str | None, title_mfrs: set[str],
                low: list[str], masked: list[bool]):
    """Choose one (span, mold) among the exact hits.

    Returns (start, end, mold, state); state is "ok", "conflict" (the mold does
    not belong to the manufacturer the listing names), "ambiguous", or "inside" (the
    mold word is only the tail of a plastic name, "Origio Burst": fine unless another
    word could be the real mold, which the caller checks).
    """
    hinted = bool(vendor_mfr or title_mfrs)

    def consistency(m: _Mold) -> int:
        if not hinted:
            return 0
        if m.mfr in title_mfrs:
            return 2
        # A brand named in the title that is not the mold's brand is a contradiction,
        # even if the vendor field happens to agree with the mold.
        return 1 if (m.mfr == vendor_mfr and not title_mfrs) else -1

    pairs, inside_plastic = _outside_plastics([(i, j, key, m) for i, j, key in hits for m in _MOLD_IDX[key]], low)
    best = max(consistency(p[3]) for p in pairs)
    top = [p for p in pairs if consistency(p[3]) == best]
    if len({(p[3].mfr, p[3].name) for p in top}) > 1:
        # "Innova Star Destroyer Discraft Zeus": two brands, a mold for each. A plastic that backs
        # one of them cannot settle which disc this is (a comparison, a lot...).
        if not (best == 2 and len({p[3].mfr for p in top}) > 1 and len({p[3].name for p in top}) > 1):
            top = _narrow(top, low, masked)
    top.sort(key=lambda p: (-len(p[2]), p[0]))
    i, j, _key_, mold = top[0]
    if len({(p[3].mfr, p[3].name) for p in top}) > 1:
        return i, j, mold, "ambiguous"
    if best > 0 and any((p[0] >= j or p[1] <= i) and consistency(p[3]) < best
                        and (len(p[2]) >= _FOREIGN_MOLD_MIN_KEY or _joined(doc, (i, j), (p[0], p[1])))
                        for p in pairs):
        # "Discraft ESP Buzzz and Wraith": another brand's mold, with no brand named for it, sits in
        # the title: a second disc (a lot, a comparison), not decoration. A short one ("Pure", "Zone")
        # only counts when a "+", "&" or "and" joins it to the first disc.
        return i, j, mold, "ambiguous"
    if best < 0:
        return i, j, mold, "conflict"
    return i, j, mold, ("inside" if inside_plastic else "ok")


def _fuzzy_mold(doc: _Doc, masked: list[bool], hint_mfrs: set[str]):
    """Best near-miss mold as (start, end, molds, ratio, extension) or None. Deliberately conservative.

    ``extension`` is True when one string is just the other plus trailing letters
    ("Aviary" vs "Aviar"): usually a different word, not a typo, so never `matched`.
    """
    if hint_mfrs:
        pools = [_FUZZY_BUCKETS[m] for m in sorted(hint_mfrs) if m in _FUZZY_BUCKETS]
    else:
        pools = [_FUZZY_BUCKETS[None]]
    if not pools:
        return None
    low, n = doc.low, doc.n
    best = None
    for i in range(n):
        if masked[i]:
            continue
        q = ""
        for j in range(i, min(n, i + 2)):
            if masked[j] or low[j] in _FUZZY_SKIP or len(low[j]) > _MAX_WORD_CHARS:
                break
            q += low[j]
            if len(q) < _FUZZY_MIN_LEN or not q[0].isalpha():
                continue
            for pool in pools:
                cands = pool.get(q[0])
                if not cands:
                    continue
                r = process.extractOne(q, cands, scorer=fuzz.ratio, score_cutoff=_FUZZY_REVIEW)
                if r is None or abs(len(r[0]) - len(q)) > 2:
                    continue
                # "Aviary" / "Challenges" vs Aviar / Challenger: one string is the other plus letters,
                # or only the last letter differs. That is likely another word (a plural, a verb),
                # not a typo ("Destroyr" is: a letter went missing inside the word)
                shared = len(os.path.commonprefix([q, r[0]]))
                tail_only = shared == min(len(q), len(r[0])) or (len(q) == len(r[0]) and shared == len(q) - 1)
                cand = (r[1], len(q), -i, i, j + 1, _MOLD_IDX[r[0]], tail_only)
                if best is None or cand[:3] > best[:3]:
                    best = cand
    if best is None:
        return None
    return best[3], best[4], best[5], best[0] / 100.0, best[6]


def _plastics_combine(a: str, b: str) -> bool:
    """Prodigy writes a numbered line and Spectrum together ("500 Spectrum"): not two lines in conflict."""
    return (a.endswith("Spectrum") and b[:1].isdigit()) or (b.endswith("Spectrum") and a[:1].isdigit())


def _plastic_owners(low: list[str], masked: list[bool]) -> set[str]:
    """Manufacturers whose own plastics appear in the title (generic plastics belong to nobody)."""
    keys = {k for _i, _j, k in _scan(low, masked, _PLASTIC_ALL, _PLASTIC_MAXN)}
    if not keys:
        return set()
    return {m for m, idx in _PLASTIC_OWN.items() if keys.intersection(idx)}


def _leftover_candidate(doc: _Doc, masked: list[bool], plastic_span: tuple[int, int] | None) -> str:
    """A mold-ish word we do not know: the only 1-2 plain words left once everything else is consumed."""
    left = [k for k in range(doc.n)
            if not masked[k]
            and not (plastic_span and plastic_span[0] <= k < plastic_span[1])
            and doc.low[k].isalpha() and 3 <= len(doc.low[k]) <= _MAX_WORD_CHARS
            and doc.low[k] not in _NOISE and doc.low[k] not in _PLASTIC_WORDS]
    if not left or len(left) > _MAX_LEFTOVER_WORDS:
        return ""
    if len(left) == 2 and left[1] != left[0] + 1:
        return ""
    return " ".join(doc.raw[k].capitalize() for k in left)


# --------------------------------------------------------------------------
# parse_listing
# --------------------------------------------------------------------------

def _as_text(value, limit: int | None = None) -> str:
    """Any value as text (cut to ``limit`` first, so hostile input costs nothing), with HTML
    entities decoded: some themes leave "P&amp;A" or "O&#39;Brien" in titles."""
    if isinstance(value, str):
        text = value
    elif value is None:
        return ""
    elif isinstance(value, (bytes, bytearray)):
        text = bytes(value).decode("utf-8", "replace")
    else:
        text = str(value)
    if limit is not None:
        text = text[:limit]
    return html.unescape(text) if "&" in text else text


def _tag_list(tags) -> list[str]:
    """Tags as a list of stripped strings, whatever shape they arrive in (list, "a, b", a lone value)."""
    if tags is None:
        return []
    if isinstance(tags, (bytes, bytearray)):
        tags = bytes(tags).decode("utf-8", "replace")
    if isinstance(tags, str):
        tags = tags.split(",", _MAX_TAGS)
    elif isinstance(tags, (set, frozenset)):
        tags = sorted(tags, key=lambda t: _as_text(t, 400))  # a set has no order: keep results reproducible
    else:
        try:
            tags = list(islice(iter(tags), _MAX_TAGS))
        except TypeError:  # a number or other non-iterable: treat it as a single tag
            tags = [tags]
    return [t.strip() for t in (_as_text(t, 400)[:200] for t in tags[:_MAX_TAGS]) if t.strip()]


def _vendor_manufacturer(vendor: str) -> str | None:
    toks = _tokens(vendor)
    if not toks:
        return None
    hits = _maximal(_scan(toks, [False] * len(toks), _VENDOR_MFR, _MFR_MAXN))
    found = {_VENDOR_MFR[k] for _, _, k in hits}
    return found.pop() if len(found) == 1 else None  # unknown, or a vendor naming several brands


def parse_listing(title, vendor="", product_type="", tags=()) -> ParsedListing:
    """Identify one store listing. Never raises on odd input; see the module docstring."""
    title = _as_text(title, 4 * _MAX_TITLE_CHARS)
    if len(title) > _MAX_TITLE_CHARS:  # cut on a word boundary so no half-word becomes a guess
        head = title[:_MAX_TITLE_CHARS].rsplit(None, 1)
        title = head[0] if head else ""
    vendor, product_type = _as_text(vendor, 400)[:200], _as_text(product_type, 400)[:200]
    tag_list = _tag_list(tags)
    doc = _Doc(title)
    low, n = doc.low, doc.n
    if not n:  # nothing to identify, but a `condition:used` tag is still true
        condition, grade = _condition("", [_fold(t).lower() for t in tag_list], _fold(product_type).lower())
        return ParsedListing(status="unparsed", condition=condition, grade=grade, confidence=0.0)

    # Not a disc at all.
    masked = _pre_mask(doc)
    reason = _ignore_reason(doc, product_type, tag_list, masked)
    if reason:
        return ParsedListing(status="ignored", confidence=0.85 if reason == "title" else 0.95)

    # Condition, grade, flags, and the numbers that must never be read as names.
    condition, grade = _condition(doc.lower_text, [_fold(t).lower() for t in tag_list], _fold(product_type).lower())
    flags = _flags(low, tag_list)
    for m in _WEIGHT_RANGE_RE.finditer(doc.text):
        _mask_chars(doc, masked, m.start() if int(m.group(1)) <= int(m.group(2)) else m.start(2), m.end())
    for m in _WEIGHT_UNIT_RE.finditer(doc.text):
        _mask_chars(doc, masked, m.start(), m.end())
    year = None
    for k, w in enumerate(low):
        if masked[k] or not w.isdigit():
            continue
        if len(w) == 4 and _YEAR_MIN <= int(w) <= _YEAR_MAX:
            year = year or int(w)
            masked[k] = True
        elif len(w) == 3 and 120 <= int(w) <= 199:  # a bare weight: "Star Destroyer 175"
            masked[k] = True

    # Manufacturer: the vendor field first, then brand words in the title.
    vendor_mfr = _vendor_manufacturer(vendor)
    brand_hits = _maximal(_scan(low, [False] * n, _TITLE_MFR, _MFR_MAXN))
    title_mfrs = {_TITLE_MFR[k] for _, _, k in brand_hits}
    for i, j, _k in brand_hits:
        _mask(masked, i, j)
    hints = title_mfrs | ({vendor_mfr} if vendor_mfr else set())

    # Edition, and the player named beside a Tour/Team/Signature marker.
    edition, player = "", ""
    ed_hits = _maximal(_scan(low, masked, _EDITION_IDX, _EDITION_MAXN))
    if ed_hits:
        # "Proto Glow" is a plastic, not a glow edition: leave words inside a longer plastic name alone.
        plastic_spans = _scan(low, masked, _PLASTIC_ALL, _PLASTIC_MAXN)
        ed_hits = [h for h in ed_hits
                   if not any(a <= h[0] and h[1] <= b and (b - a) > (h[1] - h[0]) for a, b, _ in plastic_spans)]
    if ed_hits:
        chosen = min(ed_hits, key=lambda h: (_EDITION_IDX[h[2]][0], h[0]))
        edition = _EDITION_IDX[chosen[2]][1]
        for i, j, k in ed_hits:
            _mask(masked, i, j)
            if _EDITION_IDX[k][1] in _EDITION_FLAGS:
                flags.add(_EDITION_FLAGS[_EDITION_IDX[k][1]])
        marker = next((h for h in ed_hits if _EDITION_IDX[h[2]][2]), None)
        if marker is not None:
            player, pspan = _find_player(doc, (marker[0], marker[1]), masked,
                                         _maximal(_scan(low, masked, _MOLD_IDX, _MOLD_MAXN)))
            if pspan is not None:
                _mask(masked, pspan[0], pspan[1])

    # Mold.
    mold: _Mold | None = None
    span = (0, 0)
    score = 0.0
    state = "ok"
    fuzzy = False
    extension = False
    exact = _trim_plastic_tail(_maximal(_scan(low, masked, _MOLD_IDX, _MOLD_MAXN)), low)
    if exact:
        i, j, mold, state = _pick_exact(doc, exact, vendor_mfr, title_mfrs, low, masked)
        span, score = (i, j), 1.0
    else:
        fz = _fuzzy_mold(doc, masked, hints)
        if fz is not None:
            i, j, molds, ratio, extension = fz
            mold = molds[0] if len(molds) == 1 else next((m for m in molds if m.mfr in hints), None)
            if mold is not None:
                span, score, fuzzy = (i, j), ratio, True

    # Which manufacturer, and how sure are we?
    manufacturer = ""
    mfr_conf = 0.0
    inferred = False
    if mold is not None:
        if mold.mfr in title_mfrs:
            manufacturer, mfr_conf = mold.mfr, 0.95
        elif mold.mfr == vendor_mfr:
            manufacturer, mfr_conf = mold.mfr, 1.0
        elif not hints and state != "ambiguous":
            manufacturer, mfr_conf, inferred = mold.mfr, 0.5, True  # only the mold name points to a brand
        elif hints:
            manufacturer = mold.mfr  # conflicting evidence: keep the guess, but it will be `review`
    elif hints:
        manufacturer = vendor_mfr or sorted(title_mfrs)[0]
        mfr_conf = 1.0 if vendor_mfr else 0.95

    # Plastic: the longest match among the manufacturer's plastics, outside every consumed span.
    pmask = list(masked)
    if mold is not None:
        _mask(pmask, *span)
    plastic, plastic_span = "", None
    pidx = _PLASTIC_IDX.get(manufacturer or None, _PLASTIC_IDX[None])
    phits = _scan(low, pmask, pidx, _PLASTIC_MAXN)
    own_plastics = _PLASTIC_OWN.get(manufacturer)
    if own_plastics:  # "Star Destroyer, Premium Plastic": the maker's own line beats a generic word
        phits = [h for h in phits if h[2] in own_plastics] or phits
    plastic_clash = False
    if phits:
        i, j, k = min(phits, key=lambda h: (-pidx[h[2]].ntok, -len(h[2]), h[0]))
        plastic, plastic_span = pidx[k].name, (i, j)
        # "Latitude 64 Gold Stamp Opto Ballista": two different lines of the maker in one title
        # (a colour or a marketing word can spell a plastic), so we cannot say which one the disc is
        plastic_clash = any((h[0] >= j or h[1] <= i) and pidx[h[2]].name != plastic
                            and not _plastics_combine(pidx[h[2]].name, plastic) for h in phits)
        if edition == "glow" and pidx[k].glow:
            edition = ""  # "Moonshine Glow", "Eclipse Glow": the plastic already says it
    if state == "inside":  # "Origio Burst Swoord": the Burst is a plastic's tail and "Swoord" may be the mold
        state = "ambiguous" if _leftover_candidate(doc, pmask, plastic_span) else "ok"

    if mold is None:
        cand = ""
        if manufacturer or _scan(low, pmask, _PLASTIC_ALL, _PLASTIC_MAXN):  # a plastic word says it is a disc
            cand = _leftover_candidate(doc, masked, plastic_span)
        return ParsedListing(
            status="review" if cand else "unparsed", manufacturer=manufacturer, mold=cand,
            plastic=plastic, edition=edition, player=player, year=year, condition=condition,
            grade=grade, flags=sorted(flags),
            confidence=(0.2 if manufacturer else 0.15) if cand else (0.1 if manufacturer else 0.0),
        )

    # A brand guessed from the mold name alone must be distinctive or backed by a plastic,
    # and no other brand's plastic may be in the title ("Lucid Destroyer" is not an Innova).
    if inferred:
        owners = _plastic_owners(low, pmask)
        if (len(mold.key) < _INFER_MIN_KEY and not plastic) or (owners and mold.mfr not in owners):
            mfr_conf = 0.0
    risky = False
    tail = doc.ends[span[1] - 1]
    if doc.text[tail:tail + 1] == "+":  # "Roc+", "Aviar+": the plus is a different mold, not decoration
        risky = True
    if span[1] < n and not masked[span[1]]:
        nxt = low[span[1]]
        if low[span[1] - 1] == "max" and nxt in _MAX_PLAIN_AFTER:
            risky = True  # "Prodigy D2 Max Distance": the D2 Max, or a D2 with "max distance"?
        tails = _MOLD_EXTENSIONS.get((mold.mfr, mold.key))
        if (fuzzy and mold.key + nxt in _MOLD_IDX) or (
                tails and _MIN_TAIL <= len(nxt) <= _MAX_WORD_CHARS
                and process.extractOne(nxt, tails, scorer=fuzz.ratio, score_cutoff=_TAIL_SCORE) is not None):
            # "Balllista Pro" (the typo'd base mold, or the longer Ballista Pro?) and "Aviar Classc"
            # (a near miss of "Aviar Classic"): the next word may finish a longer mold
            risky = True
        in_plastic = plastic_span is not None and plastic_span[0] <= span[1] < plastic_span[1]
        # a lone digit, "X" or "Z" ("Roc 5", "Eagle X", "Kaxe Z") or a size/version word may name a
        # sibling mold we do not know ("w/" and other lone letters are just filler)
        if not in_plastic and (nxt in _VARIANT_SUFFIXES or nxt in _VARIANT_LETTERS or (len(nxt) == 1 and nxt.isdigit())):
            after = low[span[1] + 1] if span[1] + 1 < n else ""
            risky = risky or not (nxt == "max" and after in _MAX_PLAIN_AFTER)
    risky = risky or plastic_clash
    if not risky and span[1] < n and not masked[span[1]] and low[span[1]] == "style":
        risky = True    # "Destroyer Style": resembles the mold, is not the mold
    if not risky:
        damage_text = f"{doc.lower_text} | {_fold(product_type).lower()}"
        if any(h in damage_text for h in _DAMAGED_HINTS):
            risky = _DAMAGED_RE.search(damage_text) is not None
    if not risky and any(h in doc.lower_text for h in _AUCTION_HINTS):
        risky = _AUCTION_RE.search(doc.lower_text) is not None
    if not risky and any(h in doc.lower_text for h in _RESEMBLES_HINTS):
        risky = _RESEMBLES_RE.search(doc.lower_text) is not None
    if not risky:
        risky = _sibling_apart(doc, masked, mold, span, plastic_span)
    if not risky and plastic_span is not None:
        risky = _qualified_plastic(doc, masked, plastic_span, span)
    if not risky:
        evidence = list(masked)
        _mask(evidence, *span)
        if plastic_span is not None:
            _mask(evidence, *plastic_span)
        risky = _joined_stranger(doc, evidence)
    if plastic_span is not None and plastic_span[1] < n and not masked[plastic_span[1]] and low[plastic_span[1]] == "x":
        risky = True  # "VIP-X", "Opto X": a variant of the plastic we may not know, not the plastic itself
    trusted = (
        mfr_conf > 0 and state == "ok" and not risky
        and not (fuzzy and (inferred or extension or score < _FUZZY_MATCHED / 100))
    )
    confidence = 0.55 * score + 0.25 * mfr_conf + (0.10 if plastic else 0.0) + (0.10 if state == "ok" and not risky else 0.0)
    if fuzzy:
        confidence = min(confidence, 0.85)  # never as sure as an exact hit
    if not trusted:
        confidence = min(confidence, 0.4 if state == "conflict" else 0.6)
    return ParsedListing(
        status="matched" if trusted else "review",
        manufacturer=manufacturer,
        mold=mold.name,
        plastic=plastic,
        edition=edition,
        player=player,
        year=year,
        condition=condition,
        grade=grade,
        disc_type=mold.type,
        flags=sorted(flags),
        confidence=round(max(0.0, min(1.0, confidence)), 2),
    )
