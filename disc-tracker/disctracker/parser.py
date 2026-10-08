"""Listing title -> ParsedListing (DESIGN.md section 4).

Pure and deterministic: all vocabulary lives in ``data/*.json`` and is loaded and
indexed once at import time; ``parse_listing`` does no I/O.

A title is read in this order:
  1. decode stray HTML entities, fold accents / odd unicode / dash look-alikes, collapse
     blanks, and split into alphanumeric tokens;
  2. non-disc products (bags, baskets, apparel, sets, packs, minis...) -> ``ignored``;
  3. condition, grade and flags; weights, years and grades are located and masked
     so they can never be mistaken for a plastic or a mold;
  4. manufacturer: ``vendor`` first, then brand names found in the title;
  5. edition (and the player named next to a Tour/Team/Signature marker);
  6. mold: exact match on the space-insensitive joined form ("Roc 3" == "Roc3"),
     then a conservative rapidfuzz fallback; plastic: longest match in the
     manufacturer's plastics.

The central design rule is that a wrong ``matched`` is worse than a ``review``:
anything ambiguous, conflicting, only fuzzily matched, or that looks like a
sibling mold we do not know ("Roc 4", "Zone GT") is demoted to ``review``.
"""
from __future__ import annotations

import html
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Iterable

from rapidfuzz import fuzz, process

from .models import ParsedListing

PARSER_VERSION = 2  # bump whenever rules or anything under data/ change

DATA_DIR = Path(__file__).resolve().parent / "data"
MOLD_TYPES = ("Distance Driver", "Fairway Driver", "Midrange", "Putter", "Approach")

_FUZZY_MIN_LEN = 5       # shorter strings are too easy to confuse ("Roc" ~ "Rock")
_FUZZY_REVIEW = 85       # rapidfuzz ratio needed for a mold-ish candidate
_FUZZY_MATCHED = 90      # ratio needed before a fuzzy hit may be `matched`
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
_VARIANT_SUFFIXES = frozenset("ss os gt sl xl max plus lite v2 v3 v4".split())
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
""".split())
# Multi-word phrases, written joined ("gift card" == "giftcard" == "gift-card").
_IGNORE_PHRASES = frozenset("""
giftcard giftcards giftcertificate egiftcard starterset beginnerset discset giftset
lotof packof boxof pairof shippingprotection packageprotection shippinginsurance
tanktop teepad teesign
""".split())
_MULTI_PACK_RE = re.compile(r"\d+pk")  # "3pk"
_WEIGHT_WORDS = frozenset(("wt", "weight", "weights"))
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
""".split())
_NAME_STOP = _NOISE | frozenset(
    "tour team signature first second third special anniversary prototype misprint glow glo gitd factory seconds".split())

_NOT_USED_RE = re.compile(r"\b(?:never|not)\s+used\b|\bunused\b")
_USED_RE = re.compile(
    r"\b(?:used|pre[\s-]*owned|second[\s-]*hand|pre[\s-]*loved|beat[\s-]*in|beat[\s-]*up|well[\s-]*loved|sleepy)\b"
)
_GRADE_RES = (
    re.compile(r"(?<![\d.])(\d{1,2}(?:\.\d)?)\s*/\s*10\b"),
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


# --------------------------------------------------------------------------
# Detection helpers
# --------------------------------------------------------------------------

def _ignore_reason(doc: _Doc, product_type: str, tags: list[str]) -> str:
    """Why this is not a disc ("" when it might be one)."""
    pt = _tokens(product_type)
    if _IGNORE_TYPE_WORDS.intersection(pt):
        return "product_type"
    for tag in tags:
        tt = _tokens(tag)
        if tt and len(tt) <= _IGNORE_TAG_MAX_TOKENS and "".join(tt) in _IGNORE_TAG_EXACT:
            return "tag"
    low = doc.low
    # "Net Wt. 175g" / "net weight" is a weight, not a practice net
    words = [w for k, w in enumerate(low) if not (w == "net" and low[k + 1:k + 2] and low[k + 1] in _WEIGHT_WORDS)]
    if _IGNORE_TITLE_WORDS.intersection(words):
        return "title"
    if _scan(low, [False] * len(low), _IGNORE_PHRASE_IDX, _IGNORE_PHRASE_MAXN):
        return "title"
    if any(_MULTI_PACK_RE.fullmatch(w) for w in low):
        return "title"
    return ""


def _condition(title_l: str, tags_l: list[str], ptype_l: str) -> tuple[str, float | None]:
    """("new"|"used", grade). A stated N/10 grade implies used."""
    texts = tuple(t.replace("_", " ") for t in (title_l, *tags_l, ptype_l))  # \b treats "_" as a letter
    for text in texts:
        for rx in _GRADE_RES:
            m = rx.search(text)
            if m and 1 <= float(m.group(1)) <= 10:
                return "used", float(m.group(1))
    for text in texts:
        if _USED_RE.search(_NOT_USED_RE.sub(" ", text)):
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

def _outside_plastics(pairs: list, low: list[str]) -> list:
    """Drop mold hits that are only part of a longer plastic name, if anything else is left.

    "Prime Burst Judge": Westside's Burst is just half of Dynamic's plastic, and
    in "Origio Burst Burst" only the second Burst is the mold.
    """
    spans = _scan(low, [False] * len(low), _PLASTIC_ALL, _PLASTIC_MAXN)
    free = [p for p in pairs if not any(a <= p[0] and p[1] <= b and (b - a) > (p[1] - p[0]) for a, b, _ in spans)]
    return free or pairs


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


def _pick_exact(hits: list[tuple[int, int, str]], vendor_mfr: str | None, title_mfrs: set[str],
                low: list[str], masked: list[bool]):
    """Choose one (span, mold) among the exact hits.

    Returns (start, end, mold, state); state is "ok", "conflict" (the mold does
    not belong to the manufacturer the listing names) or "ambiguous".
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

    pairs = _outside_plastics([(i, j, key, m) for i, j, key in hits for m in _MOLD_IDX[key]], low)
    best = max(consistency(p[3]) for p in pairs)
    top = [p for p in pairs if consistency(p[3]) == best]
    if len({(p[3].mfr, p[3].name) for p in top}) > 1:
        top = _narrow(top, low, masked)
    top.sort(key=lambda p: (-len(p[2]), p[0]))
    i, j, _key_, mold = top[0]
    if len({(p[3].mfr, p[3].name) for p in top}) > 1:
        return i, j, mold, "ambiguous"
    return i, j, mold, ("conflict" if best < 0 else "ok")


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
    if not n:
        return ParsedListing(status="unparsed", confidence=0.0)

    # Not a disc at all.
    reason = _ignore_reason(doc, product_type, tag_list)
    if reason:
        return ParsedListing(status="ignored", confidence=0.85 if reason == "title" else 0.95)

    # Condition, grade, flags, and the numbers that must never be read as names.
    condition, grade = _condition(doc.lower_text, [_fold(t).lower() for t in tag_list], _fold(product_type).lower())
    flags = _flags(low, tag_list)
    masked = [False] * n
    for rx in _GRADE_RES:
        for m in rx.finditer(doc.lower_text):
            _mask_chars(doc, masked, m.start(), m.end())
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
    exact = _maximal(_scan(low, masked, _MOLD_IDX, _MOLD_MAXN))
    if exact:
        i, j, mold, state = _pick_exact(exact, vendor_mfr, title_mfrs, low, masked)
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
    if phits:
        i, j, k = min(phits, key=lambda h: (-pidx[h[2]].ntok, -len(h[2]), h[0]))
        plastic, plastic_span = pidx[k].name, (i, j)
        if edition == "glow" and pidx[k].glow:
            edition = ""  # "Moonshine Glow", "Eclipse Glow": the plastic already says it

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
    if not fuzzy:
        tail = doc.ends[span[1] - 1]
        if doc.text[tail:tail + 1] == "+":  # "Roc+", "Aviar+": the plus is a different mold, not decoration
            risky = True
    if not fuzzy and span[1] < n and not masked[span[1]]:
        nxt = low[span[1]]
        in_plastic = plastic_span is not None and plastic_span[0] <= span[1] < plastic_span[1]
        # a lone digit, "X" or "Z" ("Roc 5", "Eagle X", "Kaxe Z") or a size/version word may name a
        # sibling mold we do not know ("w/" and other lone letters are just filler)
        if not in_plastic and (nxt in _VARIANT_SUFFIXES or nxt in _VARIANT_LETTERS or (len(nxt) == 1 and nxt.isdigit())):
            after = low[span[1] + 1] if span[1] + 1 < n else ""
            risky = risky or not (nxt == "max" and after in ("weight", "weights"))
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
