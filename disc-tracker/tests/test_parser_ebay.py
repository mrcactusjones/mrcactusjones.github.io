"""Offline tests for the parser on eBay-style titles (DESIGN.md section 10.5).

eBay titles are written by the seller: keyword soup, ALL CAPS, emoji, "Free Shipping",
weight ranges, quantities. They arrive with ``vendor=""``, the eBay condition text as
``product_type`` and exactly one of ``condition:new`` / ``condition:used`` in the tags (see
``ebay._convert``). Nothing here touches the network.

The central rule the whole file defends: a wrong ``matched`` is far worse than a ``review``.
Lots, pairs, bundles and "pick your disc" listings must never become a single-disc price, and
everything that is not a disc (baskets, bags, shirts, cards, dog toys...) is ``ignored``.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import pytest

from disctracker import parser
from disctracker.models import disc_key
from disctracker.parser import PARSER_VERSION, parse_listing

NEW_TAG = ("condition:new",)
USED_TAG = ("condition:used",)


@dataclass(frozen=True)
class Case:
    title: str
    kind: str                       # "matched" | "ignored" | "unsure" (anything but matched) | "review"
    tags: tuple = ()
    ptype: str = ""
    mfr: str = ""
    mold: str = ""
    plastic: str = ""
    condition: str = "new"
    extra: dict = field(default_factory=dict, hash=False)   # edition / player / year / grade / flags

    @property
    def id(self) -> str:
        return self.title[:48]


def m(title, mfr, mold, plastic="", cond="new", *, tags=(), ptype="", **extra) -> Case:
    """A single disc that must come out `matched` as exactly this disc."""
    return Case(title, "matched", tuple(tags), ptype, mfr, mold, plastic, cond, extra)


def ign(title, *, tags=(), ptype="") -> Case:
    """Not a single disc at all (lot, bundle, accessory...): status `ignored`."""
    return Case(title, "ignored", tuple(tags), ptype)


def unsure(title, *, tags=(), ptype="") -> Case:
    """We cannot tell which disc this is: anything but `matched` is acceptable."""
    return Case(title, "unsure", tuple(tags), ptype)


def rev(title, *, tags=(), ptype="") -> Case:
    """Must be exactly `review` (a disc, but not sure enough to count)."""
    return Case(title, "review", tuple(tags), ptype)


def parse(c: Case):
    return parse_listing(c.title, "", c.ptype, c.tags)


# --------------------------------------------------------------------------
# The curated title set. Each expected outcome is what a person reading the title would say.
# Where the parser first disagreed, the parser was fixed; a title that is genuinely ambiguous is
# `unsure` (anything but `matched`) rather than being pinned to a guess.
# --------------------------------------------------------------------------

SINGLE_DISCS = [
    # --- keyword soup, ALL CAPS, emoji, shipping / approval boilerplate -------------------
    m("NEW Innova Star Destroyer 175g Disc Golf Driver Max Distance!!", "Innova", "Destroyer", "Star"),
    m("Innova Champion Roc3 Disc Golf Midrange 180g NEW FREE SHIPPING", "Innova", "Roc3", "Champion"),
    m("DISCRAFT ESP BUZZZ 177G PDGA APPROVED DISC GOLF MIDRANGE", "Discraft", "Buzzz", "ESP"),
    m("Discraft Z Buzzz 175-176g Midrange Disc Golf Free Shipping Choose Your Color", "Discraft", "Buzzz", "Z"),
    m("Dynamic Discs Lucid Judge 174g Putter Disc Golf - Brand New", "Dynamic Discs", "Judge", "Lucid"),
    m("Latitude 64 Opto Ballista Pro 172g Disc Golf Driver", "Latitude 64", "Ballista Pro", "Opto"),
    m("✨ Innova DX Aviar Putter 170-172g Disc Golf Choose Your Weight & Color ✨", "Innova", "Aviar", "DX"),
    m("🔥 Prodigy 400 D2 Max 174g Disc Golf Distance Driver 🔥", "Prodigy", "D2 Max", "400"),
    m("Kastaplast K1 Berg 174g Disc Golf Putter Free Shipping", "Kastaplast", "Berg", "K1"),
    m("Westside Discs VIP Warship 176g Disc Golf Driver New", "Westside Discs", "Warship", "VIP"),
    m("Discmania S-Line PD 173g Disc Golf Distance Driver Free Shipping", "Discmania", "PD", "S-Line"),
    m("MVP Neutron Volt 165g Disc Golf Fairway Driver NEW", "MVP", "Volt", "Neutron"),
    m("Axiom Prism Proton Envy 173g Disc Golf Midrange", "Axiom", "Envy", "Prism Proton"),
    m("Streamline Neutron Drift 172g Disc Golf Fairway Driver", "Streamline", "Drift", "Neutron"),
    m("🥏 Innova Star Destroyer 🥏 175g 🔥 FREE SHIPPING 🔥", "Innova", "Destroyer", "Star"),
    m("INNOVA CHAMPION WRAITH 171G DISC GOLF DISTANCE DRIVER L@@K!!!", "Innova", "Wraith", "Champion"),
    m("Innova Star Boss 175g Disc Golf Driver Distance PDGA Approved Brand New Free Ship", "Innova", "Boss", "Star"),
    m("***NEW*** Discraft Titanium Zeus 173-174g Disc Golf Driver ***FREE SHIPPING***", "Discraft", "Zeus", "Titanium"),
    m("Innova Disc Golf Star Destroyer, 175 gram, Blue, Max Distance Driver, PDGA Approved", "Innova", "Destroyer", "Star"),
    m("Innova Star Destroyer Disc Golf Driver Max Distance 12/5/-1/3 175g", "Innova", "Destroyer", "Star"),
    m("Discraft ESP Buzzz 5/4/-1/1 177g Midrange Disc Golf", "Discraft", "Buzzz", "ESP"),
    m("Innova DX Aviar 2/3/0/1 Putter 170g", "Innova", "Aviar", "DX"),
    m("Discraft Z Buzzz Speed 5 Glide 4 Turn -1 Fade 1 Midrange", "Discraft", "Buzzz", "Z"),
    m("Innova Star Destroyer 175g Disc Golf Driver Free Shipping Max Distance PDGA Approved Hyzer Flip", "Innova",
      "Destroyer", "Star"),
    m("Innova Star Destroyer Max Distance Driver", "Innova", "Destroyer", "Star"),
    m("Innova Star Destroyer Max Dist 175g", "Innova", "Destroyer", "Star"),

    # --- weights: ranges, "choose your weight", spellings --------------------------------
    m("Innova Star Destroyer 170-175g Choose Your Weight", "Innova", "Destroyer", "Star"),
    m("Innova Star Destroyer 165-169 Grams Disc Golf Driver", "Innova", "Destroyer", "Star"),
    m("Innova Star Wraith 173 gram Disc Golf Driver", "Innova", "Wraith", "Star"),
    m("Innova Champion Wraith (173) Disc Golf Distance Driver", "Innova", "Wraith", "Champion"),
    m("Innova Star Destroyer Weight Range 160-175 Pick Your Weight", "Innova", "Destroyer", "Star"),
    m("Innova Star Destroyer 175+ grams Disc Golf", "Innova", "Destroyer", "Star"),
    m("Innova Champion Teebird3 Approx 172g Fairway Driver", "Innova", "Teebird3", "Champion"),

    # --- Roc3 vs "Roc 3", mold spelling variants ---------------------------------------
    m("Innova DX Roc 3 168g Disc Golf Midrange", "Innova", "Roc3", "DX"),
    m("INNOVA ROC3 DX 168G MIDRANGE", "Innova", "Roc3", "DX"),
    m("Innova Roc-3 Champion Disc Golf", "Innova", "Roc3", "Champion"),
    m("Innova DX Roc 175g Disc Golf Midrange Free Ship", "Innova", "Roc", "DX"),
    m("Innova Star Roc 4 Disc Golf Midrange 172g", "Innova", "Roc4", "Star"),
    m("Innova Champion Teebird 3 Disc Golf Fairway Driver 170g", "Innova", "Teebird3", "Champion"),
    m("Innova Tee Bird Star 172g", "Innova", "Teebird", "Star"),
    m("Innova Leopard 3 Champion 168g", "Innova", "Leopard3", "Champion"),
    m("Innova DX Aviar X3 Putter", "Innova", "AviarX3", "DX"),
    m("Innova Star Roc X3 Midrange 175g", "Innova", "Rocx3", "Star"),
    m("Innova Star Mako 3 Midrange 180g", "Innova", "Mako3", "Star"),

    # --- "Z Buzzz" vs "Buzzz Z" ---------------------------------------------------------
    m("Z Buzzz Discraft 177g", "Discraft", "Buzzz", "Z"),
    m("Discraft Buzzz Z 177g", "Discraft", "Buzzz", "Z"),
    m("DISCRAFT BUZZZ Z 177G PINK STAMP", "Discraft", "Buzzz", "Z"),
    m("Discraft Buzz Z Line Midrange 176g", "Discraft", "Buzzz", "Z"),
    m("Discraft Big Z Buzzz Midrange 176g", "Discraft", "Buzzz", "Big Z"),
    m("Discraft Buzzz Big Z 176g", "Discraft", "Buzzz", "Big Z"),
    m("Discraft Z Buzzz SS 175g", "Discraft", "Buzzz SS", "Z"),
    m("Discraft Buzzz SS Z 175g", "Discraft", "Buzzz SS", "Z"),
    m("Discraft Z Buzzz OS Midrange 177g", "Discraft", "Buzzz OS", "Z"),
    m("Discraft Elite Z Buzzz 177g", "Discraft", "Buzzz", "Elite Z"),
    m("Discraft Buzzz Z Lite 170g", "Discraft", "Buzzz", "Z Lite"),
    m("Discraft ESP Zone 173g Approach Putter", "Discraft", "Zone", "ESP"),
    m("Discraft Z Zone OS 175g", "Discraft", "Zone OS", "Z"),

    # --- no brand in the title (the mold is distinctive) ---------------------------------
    m("Star Destroyer 175g Disc Golf Driver", "Innova", "Destroyer", "Star"),
    m("Z Buzzz 177g Disc Golf Midrange", "Discraft", "Buzzz", "Z"),
    m("Lucid Judge 174g Disc Golf Putter", "Dynamic Discs", "Judge", "Lucid"),

    # --- typos ---------------------------------------------------------------------------
    m("Innova Star Destoyer 175g Disc Golf Driver", "Innova", "Destroyer", "Star"),
    m("Innova Star Detroyer 175g", "Innova", "Destroyer", "Star"),

    # --- condition words ---------------------------------------------------------------
    m("Used Innova Star Destroyer 175g 9/10 Disc Golf Driver", "Innova", "Destroyer", "Star", "used", grade=9.0),
    m("Pre-Owned Discraft ESP Zone 173g Disc Golf Approach", "Discraft", "Zone", "ESP", "used"),
    m("Innova Champion Teebird3 171g Pre-owned Fairway Driver", "Innova", "Teebird3", "Champion", "used"),
    m("Innova DX Aviar Putter BEAT IN 168g Disc Golf", "Innova", "Aviar", "DX", "used"),
    m("Innova DX Roc3 Beat-In Midrange 168g", "Innova", "Roc3", "DX", "used"),
    m("Innova Star Wraith Grade 8 Disc Golf Driver", "Innova", "Wraith", "Star", "used", grade=8.0),
    m("Innova Star Mako3 8/10 175g", "Innova", "Mako3", "Star", "used", grade=8.0),
    m("Innova Star Destroyer 175g Condition 9/10", "Innova", "Destroyer", "Star", "used", grade=9.0),
    m("Innova Star Destroyer 175g 9.5/10 Barely Thrown", "Innova", "Destroyer", "Star", "used", grade=9.5),
    m("Innova Champion Valkyrie Disc Golf Driver Excellent Condition", "Innova", "Valkyrie", "Champion", "used"),
    m("Innova DX Aviar Like New 167g", "Innova", "Aviar", "DX", "used"),
    m("Like New Discraft Z Buzzz 177g", "Discraft", "Buzzz", "Z", "used"),
    m("Innova Star Destroyer 175g Thrown Once", "Innova", "Destroyer", "Star", "used"),
    m("Lightly Used Innova Star Destroyer 175g", "Innova", "Destroyer", "Star", "used"),
    m("NIB Innova Star Destroyer 175g", "Innova", "Destroyer", "Star", "new"),
    m("NWOT Discraft Z Buzzz 177g", "Discraft", "Buzzz", "Z", "new"),
    m("Brand New Innova Star Wraith 173g Disc Golf Driver", "Innova", "Wraith", "Star", "new"),
    m("Unthrown Innova Champion Boss 170g Disc Golf Driver", "Innova", "Boss", "Champion", "new"),
    m("Never Thrown Innova Star Mako3 Midrange 180g", "Innova", "Mako3", "Star", "new"),
    m("Innova Star Shryke 171g (Never Used)", "Innova", "Shryke", "Star", "new"),
    m("Innova Star Shryke 171g Unused Disc Golf Driver", "Innova", "Shryke", "Star", "new"),
    m("Like New Condition NIB Innova Star Destroyer", "Innova", "Destroyer", "Star", "new"),
    m("Innova Star Destroyer 175g 10/10 Brand New Unthrown", "Innova", "Destroyer", "Star", "new"),
    # the eBay condition tag and condition text
    m("Innova Star Destroyer 175g", "Innova", "Destroyer", "Star", "used", tags=USED_TAG, ptype="Pre-Owned"),
    m("Innova Star Destroyer 175g", "Innova", "Destroyer", "Star", "new", tags=NEW_TAG, ptype="New"),
    m("Innova Star Destroyer 175g Pre-owned", "Innova", "Destroyer", "Star", "used", tags=NEW_TAG, ptype="New"),
    m("Innova Star Destroyer 175g Beat In", "Innova", "Destroyer", "Star", "used", tags=NEW_TAG),
    m("Innova Star Destroyer 175g Unthrown", "Innova", "Destroyer", "Star", "new", tags=NEW_TAG),
    m("Innova Star Destroyer 175g Unthrown", "Innova", "Destroyer", "Star", "used", tags=USED_TAG),
    m("NIB Innova Star Destroyer", "Innova", "Destroyer", "Star", "new", tags=NEW_TAG, ptype="New"),
    m("Innova Star Destroyer 175g Like New", "Innova", "Destroyer", "Star", "new", tags=NEW_TAG,
      ptype="New other (see details)"),
    m("Innova Star Destroyer 175g", "Innova", "Destroyer", "Star", "used", ptype="Pre-owned"),
    m("Innova Star Destroyer 175g Brand New", "Innova", "Destroyer", "Star", "used", tags=USED_TAG, ptype="Used"),
    m("Innova Star Destroyer 175g 9/10", "Innova", "Destroyer", "Star", "used", tags=NEW_TAG, grade=9.0),
    m("Innova Star Destroyer 175g", "Innova", "Destroyer", "Star", "new"),   # no tag, no words: new

    # --- ink / stamp / flags / editions -----------------------------------------------------
    m("Innova Champion Roc3 168g Inked Autographed Stamp", "Innova", "Roc3", "Champion", flags=["ink", "signed"]),
    m("Innova DX Aviar 167g w/ Ink & Sharpie Rainbow Stamp", "Innova", "Aviar", "DX", flags=["ink"]),
    m("Innova Star Destroyer Misprint Stamp Error 175g", "Innova", "Destroyer", "Star",
      edition="misprint", flags=["stamped_error"]),
    m("Innova Star Destroyer 175g OOP Rare HTF Collectible", "Innova", "Destroyer", "Star", flags=["oop"]),
    m("Innova Star Destroyer 175g Dyed Tie Dye Custom Dyed", "Innova", "Destroyer", "Star", flags=["dyed"]),
    m("Hydro Dipped Innova Star Wraith 172g", "Innova", "Wraith", "Star", flags=["dyed"]),
    m("Vintage Innova DX Aviar Pancake 167g Putter OOP", "Innova", "Aviar", "DX", flags=["oop"]),
    m("Innova Star Destroyer Ricky Wysocki 2015 Tour Series 175g OOP 9/10", "Innova", "Destroyer", "Star", "used",
      edition="tour series", player="Ricky Wysocki", year=2015, grade=9.0, flags=["oop"]),
    m("Paul McBeth Signature Series Discraft ESP Luna 173-174g Putter", "Discraft", "Luna", "ESP",
      edition="signature series", player="Paul McBeth"),
    m("Discraft Z Buzzz First Run 2021 177g", "Discraft", "Buzzz", "Z", edition="first run", year=2021),
    m("Dynamic Discs Lucid Escape First Run 173g", "Dynamic Discs", "Escape", "Lucid", edition="first run"),
    m("Innova Champion Katana Prototype 170g", "Innova", "Katana", "Champion",
      edition="prototype", flags=["prototype"]),
    m("Innova Star Destroyer Factory Second 175g", "Innova", "Destroyer", "Star", edition="factory second"),

    m("Discraft Paul McBeth 6X Signature Series ESP Zeus 173-174g", "Discraft", "Zeus", "ESP",
      edition="signature series", player="Paul McBeth"),                    # "6X" is world titles, not six discs
    m("Discraft Paul McBeth 6X Luna ESP 173g", "Discraft", "Luna", "ESP"),
    m("Innova Star Wraith 175g Unthrown New In Bag", "Innova", "Wraith", "Star"),
    m("Innova Star Wraith NIB (new in a plastic bag) 175g", "Innova", "Wraith", "Star"),
    m("Innova Star Wraith Flight Tested 175g", "Innova", "Wraith", "Star", "used"),
    m("Innova Star Wraith Slightly Used 9/10", "Innova", "Wraith", "Star", "used", grade=9.0),
    m("Innova Star Wraith [Pre-Owned] 170g", "Innova", "Wraith", "Star", "used"),

    # --- must not be mistaken for lots / variants ---------------------------------------------
    m("Innova Star Destroyer Choose Your Weight", "Innova", "Destroyer", "Star"),
    m("Innova Star Destroyer Random Color 175g", "Innova", "Destroyer", "Star"),
    m("Innova Star Destroyer Random Weight 170-175g", "Innova", "Destroyer", "Star"),
    m("Innova Star Destroyer Assorted Colors 175g", "Innova", "Destroyer", "Star"),
    m("Innova Star Destroyer Various Weights Available", "Innova", "Destroyer", "Star"),
    m("Innova Star Destroyer Your Choice of Color 175g", "Innova", "Destroyer", "Star"),
    m("Kastaplast K1 Lots 174g Disc Golf Driver", "Kastaplast", "Lots", "K1"),
    m("Prodigy 400 PX-3 Disc Golf Driver 174g", "Prodigy", "PX-3", "400"),
    m("Dynamic Discs Lucid Judge 12/4/-1/2 Putter", "Dynamic Discs", "Judge", "Lucid"),
    m("Innova Star Destroyer 175g Multi Color", "Innova", "Destroyer", "Star"),
    m("Innova Star Destroyer 175g Mixed Colors Available", "Innova", "Destroyer", "Star"),
    m("Innova Star Destroyer 175g Multiple Weights Available", "Innova", "Destroyer", "Star"),
    m("Innova Star Destroyer 175g Free Disc Golf Shipping", "Innova", "Destroyer", "Star"),
    m("5 Star Seller Innova Star Destroyer 175g", "Innova", "Destroyer", "Star"),
    m("Dynamic Discs Lucid Escape 175g 5 Star Seller Grade 8", "Dynamic Discs", "Escape", "Lucid", "used", grade=8.0),
    m("Grade 9 Dynamic Discs Fuzion Truth 175g", "Dynamic Discs", "Truth", "Fuzion", "used", grade=9.0),
    m("9/10 Latitude 64 Golf Discs Opto River 173g", "Latitude 64", "River", "Opto", "used", grade=9.0),
    m("Innova Star Destroyer 175g listed 9/10/2025", "Innova", "Destroyer", "Star"),   # a date, not a grade
    m("Dynamic Discs Lucid Judge Hunter Green 174g", "Dynamic Discs", "Judge", "Lucid"),
    m("Innova Star Destroyer 12-5--1-3 175g", "Innova", "Destroyer", "Star"),
    m("Innova Roc3 DX 4-4-0-3 168g", "Innova", "Roc3", "DX"),

    # --- plastics and molds added for eBay ------------------------------------------------------
    m("Discraft ESP FLX Buzzz 177g", "Discraft", "Buzzz", "ESP FLX"),
    m("Discraft Z FLX Zone 173g Approach", "Discraft", "Zone", "Z FLX"),
    m("Discraft Buzzz Z FLX 175g", "Discraft", "Buzzz", "Z FLX"),
    m("Innova Pro KC Aviar Putter 170g", "Innova", "Aviar", "KC Pro"),
    m("Innova DX Daedalus 170g Disc Golf Distance Driver", "Innova", "Daedalus", "DX"),
    m("Innova Champion Xcaliber 171g", "Innova", "Xcaliber", "Champion"),
    m("Discraft ESP Passion 174g Fairway Driver", "Discraft", "Passion", "ESP"),
    m("Dynamic Discs Lucid Freedom 173g", "Dynamic Discs", "Freedom", "Lucid"),
]

LOTS_AND_BUNDLES = [
    ign("Lot of 3 Innova Star Destroyer Discs 170g+ Disc Golf"),
    ign("Innova Champion Roc3 x2 Disc Golf Midrange Lot"),
    ign("(2) Discraft ESP Buzzz 177g Disc Golf Midrange"),
    ign("3x Innova DX Aviar Putter 170g"),
    ign("Innova DX Aviar Putter 3x 170g"),
    ign("x2 Innova Star Wraith 172g Disc Golf Driver"),
    ign("Innova Star Wraith X2 172g Disc Golf Driver"),
    ign("Innova Star Wraith 2 x 172g"),
    ign("2 x Innova Star Wraith 172g"),
    ign("2 Pack Discraft Z Buzzz 177g"),
    ign("Set of 3 Innova Champion Roc3 Midrange Discs"),
    ign("Innova Disc Golf Starter Set Destroyer Roc3 Aviar"),
    ign("Innova Star Destroyer Bundle Deal Free Shipping"),
    ign("MYSTERY Innova Star Destroyer Disc Golf"),
    ign("Pick Your Disc Innova Star Teebird3 Wraith Boss"),
    ign("Innova Star Destroyer Choose Your Disc Free Shipping"),
    ign("Select Your Mold Innova Star Destroyer 175g"),
    ign("You Pick! Innova Star Destroyer Wraith Boss Katana"),
    ign("Your Choice Innova DX Aviar Roc Birdie Dart Putter"),
    ign("Innova Star Wraith - Pick Any 3 - Discount"),
    ign("5 Disc Lot Discraft ESP Buzzz Zone Heat Force"),
    ign("Assorted Innova Disc Golf Discs DX Roc3 Aviar Leopard3"),
    ign("Random Innova Disc Golf Disc Star Champion DX"),
    ign("Random Innova Star Destroyer 175g"),
    ign("Innova Champion Roc3 PAIR 2 Discs"),
    ign("Pair of Innova Star Destroyers 175g"),
    ign("Two Innova Star Destroyer Discs 173g And 175g"),
    ign("Innova Star Destroyer 2 Discs 173g and 175g"),
    ign("3 Innova DX Aviar Putters 170g"),
    ign("12 Disc Golf Discs Innova Discraft Dynamic Used"),
    ign("Disc Golf Lot Innova Discraft Latitude 64 Prodigy MVP 12 discs"),
    ign("Innova Star Destroyer 175g Qty 3"),
    ign("Buy 2 Get 1 Free Innova Star Destroyer 175g"),
    ign("Innova Star Destroyer Lot of 2"),
    ign("Innova Star Destroyer Duo 175g"),
    ign("Bulk Disc Golf Discs Innova Star Destroyer Wraith"),
    ign("Grab Bag Innova Disc Golf Discs"),
    ign("Innova Champion Roc3 Combo Deal 2 Discs"),
    ign("Innova Star Destroyer 175g (3 Pack)"),
    ign("Innova Star Destroyer 3pk"),
    ign("Innova DX Aviar 5 Piece Lot"),
    ign("3 for $50 Innova DX Aviar Putter Disc Golf"),
    ign("Innova Star Destroyer 2 for $30 Free Shipping"),
    ign("Innova Star Destroyer Mix and Match Discs"),
    ign("Innova Star Destroyer Multiple Discs Available"),
    ign("LOT OF 4 DISCRAFT ESP BUZZZ 175G"),
    ign("Lot Innova Star Destroyer"),
    ign("Pair Innova DX Aviar Putters"),
    ign("Dozen Innova DX Aviar Putters"),
    ign("Innova Star Destroyer 175g w/ bonus disc"),
    ign("Innova Star Destroyer 2 Disc Special"),
    ign("3 Disc Innova DX Aviar Roc Leopard"),
    ign("Multi Disc Innova Star Destroyer Deal"),
    ign("Disc Golf Disc Collection Innova Star Destroyer"),
    ign("Discraft ESP Buzzz 177g Stash Sale"),
    ign("Innova Star Destroyer Wraith Lot", tags=NEW_TAG, ptype="New"),
    ign("Innova Pro Aviar (4) Putters", tags=USED_TAG, ptype="Pre-owned"),
    unsure("Innova Star Destroyer Wraith Boss 175g Disc Golf Drivers"),
    unsure("Innova Destroyer Roc3 Leopard3 Aviar Disc Golf Discs"),
    unsure("Innova Discraft Dynamic Discs Prodigy MVP Disc Golf Discs"),
    unsure("Innova Star Destroyer Discraft Zeus 175g"),
    unsure("Discraft ESP Buzzz and Wraith 177g"),                  # another brand's mold, no brand named for it
]

NOT_DISCS = [
    ign("Innova Discatcher Pro Disc Golf Basket Portable Practice"),
    ign("Innova Champion Disc Golf Bag Backpack Black"),
    ign("Innova Star Destroyer T-Shirt Disc Golf Shirt Size L"),
    ign("Discraft Buzzz Disc Golf Hat Snapback Cap Black"),
    ign("Innova Star Mini Disc Marker Roc3 Mini"),
    ign("Discraft ESP Mini Buzzz Disc Golf Mini Marker Disc"),
    ign("Innova Star Destroyer Mini Marker Disc"),
    ign("Disc Golf Rule Book Official PDGA Rules"),
    ign("Innova Disc Golf Towel Microfiber Black"),
    ign("2023 Disc Golf Pro Tour Ricky Wysocki Trading Card Rookie"),
    ign("Paul McBeth Disc Golf Rookie Card Signed Discraft"),
    ign("Hyperflite K9 Jawz Frisbee Dog Toy Competition Disc"),
    ign("Kong Flyer Frisbee Dog Toy Rubber Flying Disc"),
    ign("Frisbee Pet Toy Flying Disc For Dogs Fetch"),
    ign("Innova Star Destroyer Sticker Decal Vinyl"),
    ign("Discraft Buzzz Putter Lanyard Keychain"),
    ign("Disc Golf Scorecard Pad PDGA Approved"),
    ign("Disc Golf Cart Rolling Pull Cart"),
    ign("Dynamic Discs Backpack Disc Golf Bag"),
    ign("Disc Golf Basket Chains Replacement"),
    ign("Disc Golf Disc Holder Display Wall Mount Stand"),
    ign("Innova Pulsar Ultimate Disc 175g"),
    ign("Discraft Ultra-Star 175g Ultimate Frisbee"),
    ign("Disc Golf Tee Sign Course Signs"),
    ign("Disc Golf Pole Hole Basket Target Pro"),
    ign("Gift Card Innova Disc Golf"),
    ign("Disc Golf Disc Retriever Telescoping Pole"),
    ign("Innova Beanie Winter Hat Disc Golf"),
    ign("Innova Star Destroyer Hoodie Sweatshirt"),
    ign("Disc Golf Umbrella Cooler Water Bottle"),
    ign("Vintage Wham-O Frisbee Dog Toy 1970s"),
    ign("Innova Star Destroyer Putting Practice Net Target"),
    ign("Innova Disc Golf Bag Tag Set"),
    ign("Innova Disc Golf Bag with Cooler"),
    ign("Disc Golf Bag in Box Innova"),
    ign("Disc Golf Mini Marker Set 10 Pack"),
    ign("Innova Champion Wraith Polo Shirt Mens"),
    ign("Discraft Buzzz Disc Golf Glove Sticker Pack"),
    ign("Innova Star Destroyer", ptype="Bags"),
    ign("Innova Star Destroyer", tags=("Baskets",)),
]

NOT_SURE = [
    unsure("Disc Golf Disc 175g Blue Driver Free Shipping"),
    unsure("Innova Disc Golf Disc Star 175g New Free Shipping"),
    unsure("Innova Champion Wriath 170g Disc Golf Driver"),                  # typo too far from Wraith
    unsure("Innova Star Roc+ 175g Disc Golf Midrange"),                      # Roc+ is a different mold
    unsure("Innova DX Roc 5 Disc Golf Midrange"),                            # a sibling we do not know
    unsure("Discraft Star Destroyer 175g"),                                  # brand and mold disagree
    unsure("Innova Star Destroyers 175g Disc Golf"),                         # plural: another word
    unsure("Wham-O Frisbee Classic Disc 175g"),
    unsure("Discraft Zone GT 173g Disc Golf Putter"),                        # an unknown sibling
    unsure("Innova Star Leopord 3 168g"),                                    # near miss, not exact
    unsure("INNOVA DISC GOLF DISC NEW RARE OOP L@@K"),
    rev("Innova Star Frobnicator 175g Disc Golf Driver"),                    # unknown mold of a known brand
    unsure("Westside VIP-X Warship 176g"),                                   # VIP-X is a plastic we do not list
    unsure("Latitude 64 Opto Ballista Gold Stamp 172g"),                     # "Gold" can be a plastic or a stamp colour
    unsure("Innova Champion Metal Flake Star Destroyer 175g"),               # two plastics
    unsure("Air Mail Prodigy 400 F7 175g"),                                  # "Air" is a Prodigy plastic, "400" the other
    unsure("Innova Star Cobra 175g Disc Golf Driver"),                       # a mold we do not know: review, never a neighbour
    unsure("Discraft ESP Venom 175g"),
    unsure("Dynamic Discs Lucid Stratus 174g"),
    unsure("Discraft Z Lite Challengr SS 173g"),                             # typo'd base + SS: Challenger SS, not Challenger
    unsure("Latitude 64 Gold Balllista Pro 174g"),                           # typo'd Ballista Pro, not the plain Ballista
    unsure("Innova Star Aviar Classc 159g"),                                 # near miss of Aviar Classic, not the plain Aviar
    unsure("Westside Discs Origio Burst Swoord 150g"),                       # Burst is the plastic's tail; the mold is a typo
]

ALL_CASES = SINGLE_DISCS + LOTS_AND_BUNDLES + NOT_DISCS + NOT_SURE


def test_the_curated_set_is_big_enough_and_has_unique_titles():
    assert len(ALL_CASES) >= 80
    assert len(SINGLE_DISCS) >= 80
    # the same title may repeat only with different tags / condition text
    keys = [(c.title, c.tags, c.ptype) for c in ALL_CASES]
    assert len(keys) == len(set(keys))


@pytest.mark.parametrize("case", ALL_CASES, ids=[c.id for c in ALL_CASES])
def test_ebay_title(case):
    p = parse(case)
    assert p.status in {"matched", "review", "ignored", "unparsed"}
    if case.kind == "matched":
        assert (p.status, p.manufacturer, p.mold, p.plastic) == ("matched", case.mfr, case.mold, case.plastic), p
        assert p.condition == case.condition, p
        for name, expected in case.extra.items():
            assert getattr(p, name) == expected, (name, p)
        key = disc_key(p)
        assert key is not None and len(key.split("|")) == 5
    elif case.kind == "ignored":
        assert p.status == "ignored", p
        assert p.manufacturer == p.mold == p.plastic == "" and disc_key(p) is None
    elif case.kind == "review":
        assert p.status == "review" and disc_key(p) is None, p
    else:
        assert p.status != "matched" and disc_key(p) is None, p


# --------------------------------------------------------------------------
# Precision on the curated set: of the titles called `matched`, every one must be the
# right disc (the expected outcome of a `matched` call is a specific disc).
# --------------------------------------------------------------------------

def precision_report(cases=ALL_CASES) -> dict:
    called = right = expected_matches = found = 0
    wrong: list[tuple[str, str]] = []
    for c in cases:
        p = parse(c)
        is_match = c.kind == "matched"
        expected_matches += is_match
        if p.status == "matched":
            called += 1
            if is_match and (p.manufacturer, p.mold, p.plastic) == (c.mfr, c.mold, c.plastic):
                right += 1
                found += 1
            else:
                wrong.append((c.title, f"{p.manufacturer}|{p.mold}|{p.plastic}"))
    return {"called": called, "right": right, "wrong": wrong, "expected_matches": expected_matches,
            "recall": found / expected_matches if expected_matches else 1.0,
            "precision": right / called if called else 1.0}


def test_precision_on_the_curated_set_is_100_percent():
    report = precision_report()
    assert report["wrong"] == []
    assert report["precision"] == 1.0
    assert report["called"] >= 80


def test_recall_on_the_curated_set_is_high():
    # every clean single-disc title in the set is expected to be matched
    assert precision_report()["recall"] == 1.0


def test_no_lot_or_accessory_is_ever_matched():
    for c in LOTS_AND_BUNDLES + NOT_DISCS:
        assert parse(c).status != "matched", c.title


# --------------------------------------------------------------------------
# Condition: the eBay tag, the condition text and the title all speak
# --------------------------------------------------------------------------

@pytest.mark.parametrize("title,tags,ptype,expected", [
    # tag only
    ("Innova Star Destroyer 175g", USED_TAG, "", "used"),
    ("Innova Star Destroyer 175g", NEW_TAG, "", "new"),
    ("Innova Star Destroyer 175g", (), "", "new"),
    ("Innova Star Destroyer 175g", ("condition: used",), "", "used"),
    ("Innova Star Destroyer 175g", ("Condition:Used",), "", "used"),
    ("Innova Star Destroyer 175g", ("condition_used",), "", "used"),
    # a title that says used wins over a `new` tag
    ("Used Innova Star Destroyer 175g", NEW_TAG, "New", "used"),
    ("Pre-owned Innova Star Destroyer 175g", NEW_TAG, "New", "used"),
    ("Preowned Innova Star Destroyer 175g", NEW_TAG, "", "used"),
    ("Innova Star Destroyer 175g Beat In", NEW_TAG, "", "used"),
    ("Innova Star Destroyer 175g BEAT-UP", NEW_TAG, "", "used"),
    ("Innova Star Destroyer 175g Sleepy", NEW_TAG, "", "used"),
    ("Innova Star Destroyer 175g Gently Used", NEW_TAG, "", "used"),
    ("Innova Star Destroyer 175g 8/10", NEW_TAG, "", "used"),
    ("Innova Star Destroyer 175g Grade 7", NEW_TAG, "", "used"),
    # ...and a `used` tag wins over words that say new
    ("Brand New Innova Star Destroyer 175g", USED_TAG, "", "used"),
    ("NIB Innova Star Destroyer 175g", USED_TAG, "", "used"),
    ("NWOT Innova Star Destroyer 175g", USED_TAG, "", "used"),
    ("Unthrown Innova Star Destroyer 175g", USED_TAG, "", "used"),
    ("Never Thrown Innova Star Destroyer 175g", USED_TAG, "", "used"),
    # words that say new
    ("NIB Innova Star Destroyer 175g", (), "", "new"),
    ("NWOT Innova Star Destroyer 175g", (), "", "new"),
    ("Brand New Innova Star Destroyer 175g", (), "", "new"),
    ("Brand-New Innova Star Destroyer 175g", NEW_TAG, "", "new"),
    ("Unthrown Innova Star Destroyer 175g", (), "", "new"),
    ("Never Thrown Innova Star Destroyer 175g", NEW_TAG, "", "new"),
    ("Innova Star Destroyer 175g Never Used", (), "", "new"),
    ("Innova Star Destroyer 175g Not Used", NEW_TAG, "", "new"),
    ("Innova Star Destroyer 175g Has Not Been Used", (), "", "new"),
    ("Innova Star Destroyer 175g Unused", NEW_TAG, "", "new"),
    # "used" that is not about this disc
    ("Innova Star Destroyer 175g As Used By Ricky Wysocki", (), "", "new"),
    ("Innova Star Destroyer 175g Used For Disc Golf", NEW_TAG, "", "new"),
    # soft signals: "like new" says used, unless eBay says new or the seller says unthrown
    ("Innova Star Destroyer 175g Like New", (), "", "used"),
    ("Innova Star Destroyer 175g Like-New", (), "", "used"),
    ("Innova Star Destroyer 175g Like New", NEW_TAG, "New other (see details)", "new"),
    ("Innova Star Destroyer 175g Like New", USED_TAG, "Pre-owned", "used"),
    ("Innova Star Destroyer 175g Like New Unthrown", (), "", "new"),
    ("Innova Star Destroyer 175g Thrown A Few Times", (), "", "used"),
    ("Innova Star Destroyer 175g Thrown 2x", (), "", "used"),
    ("Innova Star Destroyer 175g Excellent Condition", (), "", "used"),
    ("Innova Star Destroyer 175g Excellent Condition", NEW_TAG, "New", "new"),
    # eBay condition text alone
    ("Innova Star Destroyer 175g", (), "Pre-owned", "used"),
    ("Innova Star Destroyer 175g", (), "Used", "used"),
    ("Innova Star Destroyer 175g", (), "New", "new"),
    ("Innova Star Destroyer 175g", (), "New with defects", "new"),
    ("Innova Star Destroyer 175g", (), "Open box", "new"),
])
def test_condition_precedence(title, tags, ptype, expected):
    p = parse_listing(title, "", ptype, tags)
    assert p.status == "matched", p
    assert p.condition == expected, p


@pytest.mark.parametrize("title,grade", [
    ("Innova Star Destroyer 9/10", 9.0), ("Innova Star Destroyer 8.5/10", 8.5), ("Innova Star Destroyer Grade 8", 8.0),
    ("Innova Star Destroyer Grade: 7", 7.0), ("Innova Star Destroyer 6 out of 10", 6.0),
    ("Innova Star Destroyer Sleepy 5", 5.0), ("Innova Star Destroyer 10/10", 10.0),
])
def test_grades_imply_used(title, grade):
    p = parse_listing(title)
    assert (p.status, p.condition, p.grade) == ("matched", "used", grade), p


def test_a_perfect_grade_on_a_brand_new_disc_stays_new():
    p = parse_listing("NIB Innova Star Destroyer 175g 10/10")
    assert (p.status, p.condition, p.grade) == ("matched", "new", None)


def test_a_date_is_not_a_grade():
    p = parse_listing("Innova Star Destroyer 175g listed 9/10/2025")
    assert (p.status, p.condition, p.grade) == ("matched", "new", None), p


def test_flight_numbers_are_not_a_grade_or_a_weight_or_a_variant():
    for title in ("Discraft Buzzz 5/4/-1/1 177g", "Discraft Buzzz 5 / 4 / -1 / 1", "Discraft Buzzz 5|4|-1|1",
                  "Discraft Buzzz (5, 4, -1, 1)"):
        p = parse_listing(title)
        assert (p.status, p.mold, p.condition, p.grade) == ("matched", "Buzzz", "new", None), (title, p)


# --------------------------------------------------------------------------
# Quantities must not be confused with mold names
# --------------------------------------------------------------------------

@pytest.mark.parametrize("title,mold", [
    ("Innova Star Aviar X3", "AviarX3"), ("Innova Star Aviar X 3", "AviarX3"), ("Innova Star AviarX3", "AviarX3"),
    ("Innova Star Roc X3", "Rocx3"), ("Innova Star Rocx3", "Rocx3"), ("Innova Star Roc 3", "Roc3"),
    ("Innova Star Teebird 3", "Teebird3"), ("Innova Star Mako (3)", "Mako3"),
    ("Innova Star Roc 3 X-Out", "Roc3"),
])
def test_x_and_digits_that_belong_to_a_mold_are_not_a_quantity(title, mold):
    p = parse_listing(title)
    assert p.status != "ignored", p
    assert p.mold == mold, p


@pytest.mark.parametrize("title", [
    "Innova Star Destroyer 1x", "Innova Star Destroyer x1", "Innova Star Destroyer (1)", "Innova Star Destroyer Qty 1",
    "Innova Star Destroyer 175g Free Shipping Orders Over $35", "2021 Innova Star Destroyer 175g",
    "Innova Star Destroyer 175 g", "Innova Star Destroyer #1 Seller", "Innova Star Destroyer 100% Authentic",
])
def test_single_quantities_and_ordinary_numbers_are_still_discs(title):
    p = parse_listing(title)
    assert p.status == "matched" and p.mold == "Destroyer", p


# --------------------------------------------------------------------------
# Properties over generated titles
# --------------------------------------------------------------------------

LOT_MARKERS = [
    "Lot of {n}", "{n}x", "{n} x", "x{n}", "X {n}", "({n})", "[{n}]", "Qty{n}", "QTY: {n}", "Qty {n}", "{n} Discs",
    "{n} disc lot", "{n}pc", "{n} pcs", "{n}ct", "{n}-Pack", "{n} pack", "Pack of {n}", "Set of {n}", "{n} Piece",
    "{n}pk", "{n} Count", "{n} for $40", "Buy {n}", "Choice of {n}", "Pick {n}", "Pick any {n}", "{n} Disc Bundle",
    "{n}-Disc Set", "Bundle of {n}", "{n} Total",
]
LOT_WORDS = ["Pair", "Duo", "Trio", "Bulk", "Wholesale", "Mystery", "Grab Bag", "Bundle", "Combo", "Dozen", "Lot",
             "You Pick", "Pick Your Disc", "Choose Your Disc", "Assorted Discs", "Random Discs", "Mixed Discs",
             "Multi Disc", "Stash", "Haul", "Collection", "BOGO", "Mix and Match", "w/ bonus disc", "Your Choice"]


def _generated_singles(count: int, seed: int = 5) -> list[str]:
    import json
    import random
    rng = random.Random(seed)
    molds = json.loads((parser.DATA_DIR / "molds.json").read_text("utf-8"))["molds"]
    plastics = json.loads((parser.DATA_DIR / "plastics.json").read_text("utf-8"))
    out = []
    for e in rng.sample(molds, min(count, len(molds))):
        pls = [p["name"] for p in plastics["manufacturers"].get(e["manufacturer"], [])] or ["Premium"]
        out.append(f"{e['manufacturer']} {rng.choice(pls)} {e['mold']} {rng.randint(150, 175)}g")
    return out


def test_every_generated_single_disc_is_matched_before_we_make_lots_of_it():
    singles = _generated_singles(80)
    assert all(parse_listing(t).status == "matched" for t in singles)


@pytest.mark.parametrize("marker", LOT_MARKERS)
def test_quantity_markers_never_leave_a_match(marker):
    for single in _generated_singles(40):
        for n in (2, 3, 5, 10):
            text = marker.format(n=n)
            for title in (f"{text} {single}", f"{single} {text}", f"{single} - {text} - Free Ship"):
                assert parse_listing(title).status != "matched", title


@pytest.mark.parametrize("word", LOT_WORDS)
def test_lot_words_never_leave_a_match(word):
    for single in _generated_singles(40):
        for title in (f"{word} {single}", f"{single} {word}", f"{single} - {word} - Free Ship"):
            assert parse_listing(title).status != "matched", title


@pytest.mark.parametrize("suffix", [
    " FREE SHIPPING", " PDGA Approved Disc Golf Driver", " L@@K", " 🔥🔥", " Choose Your Weight", " Max Distance",
    " Hyzer Flip Overstable", " - Brand New In Hand Ships Fast",
])
def test_soup_after_a_good_title_does_not_change_what_it_is(suffix):
    for c in SINGLE_DISCS:
        if c.kind != "matched" or c.title.endswith(("Max Distance", "Driver", "Max Dist")):
            continue
        p = parse_listing(c.title + suffix, "", c.ptype, c.tags)
        base = parse(c)
        assert (p.status, p.manufacturer, p.mold, p.plastic) == (base.status, base.manufacturer, base.mold, base.plastic), \
            (c.title + suffix, p, base)
        # brand new / unthrown in the soup may only move a "soft used" title towards new, never the reverse
        assert p.condition == base.condition or "New" in suffix, (c.title + suffix, p, base)


SOUP = ["FREE SHIPPING", "NEW", "RARE", "OOP", "HTF", "L@@K", "PDGA APPROVED", "Disc Golf Driver", "Disc Golf", "Max Distance",
        "Choose Your Weight", "Pink", "Blue Stamp", "175g", "170-175g", "165-169 grams", "Flight 12/5/-1/3", "BRAND NEW",
        "NIB", "🔥", "✨", "Fast Ship", "Collectible", "Hot Stamp", "Midrange", "Putter", "Fairway Driver", "Overstable",
        "Understable", "Bottom Stamp", "Rainbow", "Glow Stamp", "Great Condition", "Used", "Pre-owned", "9/10", "Beat In",
        "Sleepy", "Grade 8", "Unthrown", "Never Thrown", "NWOT", "Like New", "Excellent Condition", "w/ Ink", "Signed", "Dyed"]
HARD_USED = {"Used", "Pre-owned", "9/10", "Beat In", "Sleepy", "Grade 8"}
SOFT_USED = {"Like New", "Excellent Condition", "Great Condition"}
STRONG_NEW = {"NIB", "NWOT", "BRAND NEW", "Unthrown", "Never Thrown"}


def _soup_cases(count: int = 900, seed: int = 11):
    import json
    import random
    rng = random.Random(seed)
    molds = json.loads((parser.DATA_DIR / "molds.json").read_text("utf-8"))["molds"]
    plastics = json.loads((parser.DATA_DIR / "plastics.json").read_text("utf-8"))
    generic = [p["name"] for p in plastics["generic"]]
    combos = []
    for e in molds:
        for p in plastics["manufacturers"].get(e["manufacturer"]) or [{"name": g} for g in generic]:
            combos.append((e["manufacturer"], e["mold"], p["name"]))
    for mfr, mold, plastic in rng.sample(combos, count):
        noise = rng.sample(SOUP, rng.randint(1, 4))
        core = rng.choice([f"{mfr} {plastic} {mold}", f"{plastic} {mold} {mfr}", f"{mfr} {mold} {plastic}"])
        where = rng.randint(0, 2)
        title = {0: " ".join(noise) + " " + core, 1: core + " " + " ".join(noise),
                 2: noise[0] + " " + core + " " + " ".join(noise[1:])}[where]
        if rng.random() < 0.3:
            title = title.upper()
        yield title, (mfr, mold, plastic), set(noise)


def test_soup_around_any_disc_keeps_the_right_identity_and_condition():
    wrong, unmatched, cond_wrong = [], 0, []
    cases = list(_soup_cases())
    for title, (mfr, mold, plastic), noise in cases:
        p = parse_listing(title)
        if p.status != "matched":
            unmatched += 1
            continue
        if (p.manufacturer, p.mold, p.plastic) != (mfr, mold, plastic):
            wrong.append((title, p.manufacturer, p.mold, p.plastic))
        want = "used" if noise & HARD_USED else ("used" if noise & SOFT_USED and not noise & STRONG_NEW else "new")
        if p.condition != want:
            cond_wrong.append((title, p.condition, want))
    assert wrong == []
    assert cond_wrong == []
    assert unmatched <= len(cases) * 0.02          # the soup alone should hardly ever cost a match


def test_junk_titles_never_raise_and_keep_the_invariants():
    import random
    rng = random.Random(9)
    vocab = ["Innova", "Discraft", "Star", "Z", "Buzzz", "Destroyer", "Roc", "3", "x", "2x", "(2)", "lot", "of", "175g",
             "9/10", "NEW", "used", "NIB", "--", "///", "12/5/-1/3", "Free", "Shipping", "#", "&", "+", "🔥", "Pre-owned",
             "pick", "your", "disc", "discs", "set", "1", "100", "0", "-1", "Lucid", "Judge", "ESP", "FLX", "OS", "Max"]
    for _ in range(4000):
        title = " ".join(rng.choice(vocab) for _ in range(rng.randint(0, 12)))
        tags = rng.choice([(), NEW_TAG, USED_TAG])
        p = parse_listing(title, "", rng.choice(["", "New", "Pre-owned", "Used"]), tags)
        assert p.status in {"matched", "review", "ignored", "unparsed"}
        assert p.condition in {"new", "used"}
        assert 0.0 <= p.confidence <= 1.0
        assert p.grade is None or 1 <= p.grade <= 10
        if tags == USED_TAG:
            assert p.condition == "used" or p.status == "ignored", (title, p)
        if p.status == "matched":
            assert p.manufacturer and p.mold and p.confidence >= 0.7, (title, p)
        if p.status == "ignored":
            assert p.manufacturer == p.mold == p.plastic == ""


# --------------------------------------------------------------------------
# Housekeeping: version, speed, determinism
# --------------------------------------------------------------------------

def test_parser_version_was_bumped_for_ebay():
    assert PARSER_VERSION >= 3


def test_new_molds_and_plastics_are_in_the_data_files():
    names = {(m.mfr, m.name) for ms in parser._MOLD_IDX.values() for m in ms}
    assert {("Innova", "Daedalus"), ("Discraft", "Passion"), ("Dynamic Discs", "Freedom")} <= names
    assert "espflx" in parser._PLASTIC_OWN["Discraft"] and "zflx" in parser._PLASTIC_OWN["Discraft"]


def test_ebay_titles_parse_quickly_and_deterministically():
    titles = [c for c in ALL_CASES]
    for c in titles:
        assert parse(c) == parse(c)
    start = time.perf_counter()
    rounds = 200
    for _ in range(rounds):
        for c in titles:
            parse(c)
    elapsed = time.perf_counter() - start
    per = elapsed / (rounds * len(titles))
    assert per < 0.002, f"{per * 1000:.2f} ms per title"          # typically ~0.05 ms


def test_ignore_vocabulary_does_not_collide_with_real_discs():
    vocabulary = parser._MOLD_WORDS | parser._PLASTIC_WORDS | parser._EDITION_WORDS | parser._MFR_WORDS
    assert not (parser._IGNORE_TITLE_WORDS & vocabulary)
    assert not (parser._VAGUE_WORDS & vocabulary)
    assert not (parser._NUMBER_WORDS & vocabulary)


# --------------------------------------------------------------------------
# Review round 1: gaps found by attacking the parser. Every title below was reproduced first
# (it came out `matched`, or with a wrong field) and is fixed in parser.py version 4.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("title", [
    # multiplication signs are not the letter x for NFKD, so "3x" was spelled around the detector
    "Innova Star Destroyer 175g ×3", "Innova Star Destroyer 175g 3×", "Innova Star Destroyer 175g ✕3",
    "Innova Star Destroyer 175g ✖ 2", "×2 Innova Star Destroyer 175g", "Innova Star Destroyer × 2 175g",
    # a spelled-out count in front of "disc"
    "Two Disc Innova Star Destroyer 175g", "Innova Star Destroyer 175g Three-Disc Deal", "Four Disc Innova Star Destroyer",
    "Innova Star Destroyer Both Discs 175g",
    # quantity phrases that are not "qty N"
    "Innova Star Destroyer 175g Count: 3", "Innova Star Destroyer 175g Count 3", "Innova Star Destroyer 175g Quantity of 3",
    "Innova Star Destroyer 175g Total of 3", "Innova Star Destroyer 175g Number of discs: 3",
    "Innova Star Destroyer 175g (Count 3)",
    # kits and picks
    "Innova DX Aviar Starter Kit", "Innova Beginner Disc Golf Kit Aviar", "Innova Star Destroyer 175g Pick Any",
    "Innova Star Destroyer 175g Pick Your Own", "Innova Star Destroyer 175g U Pick", "Innova Star Destroyer 175g Select Any",
    "Innova Star Destroyer 175g Twin Pack", "Innova Star Destroyer Twins", "Innova Star Destroyer Doubles",
])
def test_review_more_lot_spellings_are_never_a_single_disc(title):
    p = parse_listing(title, "", "New", NEW_TAG)
    assert p.status == "ignored" and disc_key(p) is None, p


@pytest.mark.parametrize("title", [
    "Innova Star Destroyer 175g Count 1", "Innova Star Destroyer 175g Quantity of 1", "Kastaplast K1 Lots 174g",
    "Innova Star Destroyer 175g Two Tone", "Innova Star Destroyer 175g Two-Tone Dye", "Innova Roc 3 Disc Golf Midrange 168g",
    "MVP Total Eclipse Volt 170g", "Innova Star Destroyer 175g Pick Your Weight", "Innova Star Destroyer 175g Pick Color",
    "Innova Star Destroyer 175g Sold Individually", "Innova Star Destroyer 175g Price Per Disc",
    "Innova Star Destroyer 175g Kitten Blue",
])
def test_review_lot_detectors_leave_single_discs_alone(title):
    assert parse_listing(title, "", "New", NEW_TAG).status == "matched", title


@pytest.mark.parametrize("title", [
    # another brand's short mold, joined to the first disc: a second disc
    "Innova Champion Roc3 & Zone", "Innova Champion Roc3 + Zone", "Innova Champion Roc3 / Fuse", "Innova Champion Roc3 and Wolf",
    "Innova Champion Roc3 plus Pure", "Discraft ESP Buzzz & Roc", "Zone & Innova Champion Roc3", "Innova Star Destroyer + Sol",
    # a second disc whose mold we do not know at all
    "Innova Star Destroyer + Sparrow", "Innova Star Destroyer & Sparrow 175g", "Innova Star Destroyer and Hawkeye",
    "Sparrow + Innova Star Destroyer 175g", "Innova Star Destroyer 175g / Sparrow", "Innova Star Destroyer plus Starfire",
    "Discraft ESP Buzzz & Cyclone 177g",
])
def test_review_a_second_disc_joined_by_a_plus_or_and_is_never_a_single_disc(title):
    p = parse_listing(title, "", "New", NEW_TAG)
    assert p.status != "matched" and disc_key(p) is None, p


@pytest.mark.parametrize("title,mold", [
    ("Discraft ESP Buzzz Pure Plastic", "Buzzz"),
    ("Innova DX Aviar 167g w/ Ink & Sharpie Rainbow Stamp", "Aviar"),
    ("Innova Star Destroyer + Free Shipping", "Destroyer"),
    ("Innova Star Destroyer 175g Blue & Pink Stamp", "Destroyer"),
    ("Innova Star Destroyer 175g Fast and Free Shipping", "Destroyer"),
    ("Innova Star Destroyer 170g / 175g Hyzer/Flip", "Destroyer"),
    ("Innova Star Destroyer 175g Pink/Purple Swirl", "Destroyer"),
    ("Innova Star Destroyer 175g Stable/Overstable", "Destroyer"),
    ("Innova Star Destroyer 12/5/-1/3 + Free Shipping", "Destroyer"),
    ("Innova Star Destroyer 175g Disc Golf Driver & Midrange", "Destroyer"),
    ("Innova Star Destroyer Choose Weight & Color", "Destroyer"),
    ("Innova Star Destroyer 175g Violet & Maroon", "Destroyer"),
    ("Innova Star Destroyer 175g & Original Sealed Packaging", "Destroyer"),
])
def test_review_joiners_that_are_just_soup_do_not_cost_the_match(title, mold):
    p = parse_listing(title, "", "New", NEW_TAG)
    assert (p.status, p.mold) == ("matched", mold), p


@pytest.mark.parametrize("title", [
    "Innova Star Destroyer Necklace Pendant", "Innova Star Destroyer Charm Bracelet", "Innova Star Destroyer Earrings",
    "Innova Star Destroyer Cufflinks", "Innova Star Destroyer Lamp", "Innova Star Destroyer Wall Clock",
    "Innova Star Destroyer Clock", "Innova Star Destroyer Pillow", "Innova Star Destroyer Key Ring",
    "Innova Star Destroyer Keyring", "Innova Star Destroyer Wallet", "Innova Star Destroyer Coaster Set",
    "Innova Star Destroyer Replica", "Innova Star Destroyer Miniature", "Innova Star Destroyer Wall Art Painting",
    "Innova Star Destroyer Sign", "Innova Star Destroyer Holster", "Innova Star Destroyer Pouch",
])
def test_review_more_things_that_are_not_discs_are_ignored(title):
    p = parse_listing(title, "", "New", NEW_TAG)
    assert p.status == "ignored" and disc_key(p) is None, p


@pytest.mark.parametrize("title", [
    "Dynamic Discs Lucid Chameleon Judge 174g", "Dynamic Discs Fuzion Orbit Judge", "Dynamic Discs Lucid Glitter Judge",
    "Dynamic Discs Lucid Sparkle Judge", "Innova Shimmer Star Destroyer 175g", "Innova Star Ice Destroyer",
    "Innova Star Overmold Destroyer", "Innova Star Rubber Destroyer", "Innova Star Sparkle Destroyer",
    "Discraft Jawbreaker Z FLX Buzzz 177g", "Discraft Z Metal Flake Buzzz", "Discraft ESP Soft Buzzz 177g",
    "Kastaplast K1 Hard Berg", "Kastaplast K1 Glow Soft Berg", "Discmania Lux S-Line PD",
    "Innova Destroyer Star Pearl 175g", "Innova Star Marble Destroyer 175g", "Innova Star Confetti Destroyer",
])
def test_review_an_unlisted_plastic_variant_is_not_the_plain_plastic(title):
    p = parse_listing(title, "", "New", NEW_TAG)
    assert p.status != "matched" and disc_key(p) is None, p


@pytest.mark.parametrize("title,plastic", [
    ("Dynamic Discs Lucid Ice Judge 174g", "Lucid Ice"), ("Dynamic Discs Lucid Air Judge", "Lucid Air"),
    ("Discraft Z FLX Zone 173g", "Z FLX"), ("Discraft ESP Flex Buzzz 177g", "ESP FLX"), ("Discraft Z Flex Zone 173g", "Z FLX"),
    ("Kastaplast K1 Soft Berg", "K1 Soft"), ("Westside BT Hard Warship", "BT Hard"),
    ("Westside Origio Burst Warship", "Origio Burst"), ("Dynamic Discs Prime Burst Judge", "Prime Burst"),
    ("Innova Metal Flake Champion Destroyer", "Metal Flake Champion"), ("MVP Electron Firm Volt", "Electron Firm"),
    ("Innova Star Blue Destroyer 175g", "Star"), ("Innova Star Destroyer Soft Feel 175g", "Star"),
])
def test_review_listed_plastics_with_their_own_qualifier_still_match(title, plastic):
    p = parse_listing(title, "", "New", NEW_TAG)
    assert (p.status, p.plastic) == ("matched", plastic), p


@pytest.mark.parametrize("title", [
    "Innova Aviar 175g 5 Star Seller", "Innova Aviar Putter Star Seller Fast Ship", "Innova Roc3 Top Rated Seller 5 Star",
    "Innova DX Aviar 5-Star Rated Seller", "5 Star Seller Innova Roc3 168g",
])
def test_review_a_seller_rating_is_not_the_star_plastic(title):
    p = parse_listing(title, "", "New", NEW_TAG)
    assert p.status == "matched" and p.plastic != "Star", p


@pytest.mark.parametrize("title", [
    "Innova Star Destroyer 175g Lighter Weight", "Innova DX Aviar Lighter Weights Available 150g",
    "Innova Star Destroyer Autographed", "Innova Star Destroyer Signed By The Player",
])
def test_review_words_that_look_like_accessories_but_describe_discs_stay_discs(title):
    assert parse_listing(title, "", "New", NEW_TAG).status in {"matched", "review"}
    assert parse_listing("Innova Star Destroyer 175g Lighter Weight").status == "matched"


@pytest.mark.parametrize("title,condition", [
    ("Innova Star Destroyer 175g Previously Owned", "used"), ("Innova Star Destroyer 175g Previously Thrown", "used"),
    ("Innova Star Destroyer 175g 2nd Hand", "used"), ("Innova Star Destroyer 175g Previously Used", "used"),
    ("Innova Star Destroyer 175g Never Previously Owned", "new"),
    ("Innova Star Destroyer 175g Brand New Not Previously Thrown", "new"),
])
def test_review_more_ways_to_say_used(title, condition):
    p = parse_listing(title)
    assert (p.status, p.condition) == ("matched", condition), p


@pytest.mark.parametrize("ptype,condition", [
    ("Good", "used"), ("Very Good", "used"), ("Acceptable", "used"), ("Fair", "used"), ("Excellent", "used"),
    ("Certified - Refurbished", "used"), ("Seller refurbished", "used"), ("Excellent - Refurbished", "used"),
    ("New", "new"), ("New other (see details)", "new"), ("New with defects", "new"), ("Open box", "new"),
    ("Brand New", "new"), ("", "new"),
])
def test_review_the_condition_text_alone_is_enough_when_the_id_tag_is_missing(ptype, condition):
    p = parse_listing("Innova Star Destroyer 175g", "", ptype, ())
    assert (p.status, p.condition) == ("matched", condition), p


@pytest.mark.parametrize("ptype", ["For parts or not working", "Damaged"])
def test_review_a_damaged_condition_text_is_used_and_not_a_matched_disc(ptype):
    p = parse_listing("Innova Star Destroyer 175g", "", ptype, ())
    assert (p.status, p.condition) == ("review", "used"), p


def test_review_the_new_tag_still_beats_a_soft_condition_text():
    p = parse_listing("Innova Star Destroyer 175g", "", "Good", NEW_TAG)
    assert p.condition == "new", p        # eBay's own id outranks our reading of its text


@pytest.mark.parametrize("title", [
    "Innova Star Destroyer Tour Series Awesome Beautiful 175g", "Innova Star Destroyer Awesome Beautiful Tour Series 175g",
    "Innova Star Destroyer Signature Series Amazing Stunning", "Gorgeous Perfect Innova Star Destroyer Tour Series",
    "Innova Star Destroyer Tour Series Violet Maroon", "Innova Star Destroyer Tour Series Nice Cool",
])
def test_review_soup_next_to_an_edition_marker_is_not_a_player(title):
    p = parse_listing(title)
    assert p.status == "matched" and p.player == "", p


def test_review_a_real_player_next_to_a_marker_is_still_found():
    for title, player in [("Innova Star Destroyer Ricky Wysocki Tour Series", "Ricky Wysocki"),
                          ("Innova Star Destroyer Tour Series Gregg Barsby 2012", "Gregg Barsby"),
                          ("Paul McBeth Signature Series Discraft ESP Luna", "Paul McBeth")]:
        assert parse_listing(title).player == player, title


def test_review_the_version_was_bumped_again_for_the_review_fixes():
    # every stored listing re-parses once: the rules above changed what a title means
    assert PARSER_VERSION >= 4


@pytest.mark.parametrize("title", [
    # a longer sibling mold written back to front ...
    "Discraft Z SS Buzzz", "Discraft ESP SS Buzzz 177g", "Discraft ESP OS Zone", "Latitude 64 Opto Pro Ballista",
    "Latitude 64 Opto Pro Saint 172g", "Prodigy 400 Max D2", "Prodigy 400 Max D3 174g", "Innova Classic Aviar",
    "Innova DX Classic Aviar 170g",
    # ... or with its tail pushed behind the plastic or the weight
    "Discraft Buzzz Z SS 175g", "Discraft Buzzz ESP SS", "Discraft Zone Z OS 173g", "Discraft Buzzz 175g SS",
    "Discraft Z Buzzz 175g OS", "Discraft Challenger ESP SS", "Latitude 64 Ballista Opto Pro",
    "Latitude 64 Opto Ballista 172g Pro", "Prodigy D2 400 Max 174g", "Innova Aviar DX Classic", "Innova Aviar 170g Classic",
    "Innova Star Aviar 175g 3",
])
def test_review_a_sibling_mold_with_its_parts_apart_is_not_the_base_mold(title):
    p = parse_listing(title, "", "New", NEW_TAG)
    assert p.status != "matched" and disc_key(p) is None, p


@pytest.mark.parametrize("title,mold,plastic", [
    ("Discraft Z Buzzz SS", "Buzzz SS", "Z"), ("Discraft Buzzz SS Z 175g", "Buzzz SS", "Z"),
    ("Latitude 64 Opto Ballista Pro", "Ballista Pro", "Opto"), ("Prodigy 400 D2 Max", "D2 Max", "400"),
    ("Innova Aviar Classic DX", "Aviar Classic", "DX"), ("Discraft Buzzz Z 175g", "Buzzz", "Z"),
    ("Latitude 64 Opto Ballista 172g", "Ballista", "Opto"), ("Prodigy 400 D2 174g", "D2", "400"),
    ("Innova DX Aviar 170g Pancake", "Aviar", "DX"), ("Discraft Z Zone Free Shipping", "Zone", "Z"),
    ("Innova Pro Aviar 170g", "Aviar", "Pro"), ("Discraft Z Buzzz 175g Rare Pink", "Buzzz", "Z"),
    ("Disc Golf Driver Innova Aviar DX 170g", "Aviar", "DX"), ("Innova DX Aviar 170g Disc Golf Driver", "Aviar", "DX"),
])
def test_review_the_exact_sibling_and_the_plain_mold_are_still_told_apart(title, mold, plastic):
    p = parse_listing(title, "", "New", NEW_TAG)
    assert (p.status, p.mold, p.plastic) == ("matched", mold, plastic), p


@pytest.mark.parametrize("title", [
    "Innova Star Destroyer II", "Innova Star Destroyer III 175g", "Discraft ESP Zone IV", "Discraft Z Buzzz SuperSoft",
])
def test_review_a_roman_numeral_or_supersoft_after_a_mold_names_another_disc(title):
    p = parse_listing(title, "", "New", NEW_TAG)
    assert p.status != "matched" and disc_key(p) is None, p


@pytest.mark.parametrize("title", [
    # a listing that only names the disc it resembles is somebody else's product
    "Similar to Innova Destroyer Disc Golf Driver 175g", "Compare to Innova Star Destroyer 175g",
    "Comparable to Innova Roc3 Disc Golf Midrange", "Alternative to Innova Aviar Putter", "Clone of Innova Wraith",
    "Inspired by Innova Destroyer Disc Golf Disc", "Replacement for Innova Star Destroyer 175g",
    "Innova Destroyer Knockoff", "Innova Destroyer Knock-off", "Innova Destroyer Dupe", "Dupe of Innova Destroyer",
    "Innova Destroyer Look Alike", "Innova Destroyer Lookalike", "Innova Destroyer Style Disc Golf Driver",
    "Destroyer Style Disc Golf Driver 175g Innova", "Innova Star Destroyer Counterfeit", "Innova Star Destroyer Fake",
    "Innova Aviar Style Putter Instead Of Innova Star Destroyer",
])
def test_review_a_listing_that_only_resembles_a_disc_is_not_that_disc(title):
    p = parse_listing(title, "", "New", NEW_TAG)
    assert p.status != "matched" and disc_key(p) is None, p


@pytest.mark.parametrize("title", [
    "Innova Star Destroyer 175g Same As Pictured", "Innova Star Destroyer 175g Exactly As Shown",
    "Innova Star Destroyer 175g Best Style In Class", "Innova Star Destroyer 175g Free Shipping Compare Prices",
    "Innova Star Destroyer 175g Like New", "Innova Star Destroyer 175g Looks Great",
])
def test_review_comparison_words_in_ordinary_soup_do_not_cost_the_match(title):
    assert parse_listing(title, "", "New", NEW_TAG).status == "matched", title


@pytest.mark.parametrize("title", [
    "Innova Star Destroyer auction no reserve", "Innova Star Destroyer 175g Auction", "Innova Star Destroyer No Reserve",
    "Innova Star Destroyer 175g Starting Bid", "Innova Star Destroyer Opening Bid 0.99", "Innova Star Destroyer Bid Now",
    "Innova Star Destroyer Bidding Starts At 99 cents", "Innova Star Destroyer 175g Current Bid",
])
def test_review_a_title_that_talks_about_bidding_is_never_an_asking_price(title):
    # ebay.py already drops AUCTION listings; the title is a second line of defence
    p = parse_listing(title, "", "New", NEW_TAG)
    assert p.status != "matched" and disc_key(p) is None, p


@pytest.mark.parametrize("title", [
    "Innova Star Destroyer 175g Best Offer Accepted", "Innova Star Destroyer 175g Buy It Now",
    "Innova Star Destroyer 175g Make An Offer", "Innova Star Destroyer 175g Rebid Rare",
])
def test_review_offer_words_are_not_bids(title):
    assert parse_listing(title, "", "New", NEW_TAG).status == "matched", title


@pytest.mark.parametrize("title,ptype", [
    ("Innova Star Destroyer 175g CRACKED", ""), ("Innova Star Destroyer Broken", ""), ("Innova Star Destroyer Cut In Half", ""),
    ("Innova Star Destroyer 175g Damaged Rim", ""), ("Innova Star Destroyer Snapped", ""),
    ("Innova Star Destroyer 175g For Parts", ""), ("Innova Star Destroyer Parts Only", ""),
    ("Innova Star Destroyer 175g", "For parts or not working"), ("Innova Star Destroyer 175g", "Damaged"),
])
def test_review_a_damaged_disc_does_not_set_the_price_floor_of_a_disc(title, ptype):
    # a cracked or cut disc is a used disc nobody can throw: its price says nothing about the disc
    p = parse_listing(title, "", ptype, USED_TAG)
    assert p.status != "matched" and disc_key(p) is None, p


@pytest.mark.parametrize("title", [
    "Innova Star Destroyer 175g Not Cracked", "Innova Star Destroyer 175g No Damage Never Broken",
    "Innova Star Destroyer 175g Never Cracked", "Innova Star Destroyer 175g Without Damaged Rim",
    "Innova Star Destroyer 175g Crack Free", "Innova Star Destroyer 175g Rim Scuffs Only",
    "Innova Star Destroyer 175g Half Moon Stamp",
])
def test_review_a_disc_that_says_it_is_not_damaged_stays_a_disc(title):
    assert parse_listing(title, "", "Pre-owned", USED_TAG).status == "matched", title


@pytest.mark.parametrize("title", [
    "Disc Golf Disc Assortment Innova Star Destroyer", "Innova Disc Golf Assortment Of Discs", "Innova Star Destroyer Medley",
])
def test_review_assortments_are_lots(title):
    p = parse_listing(title, "", "New", NEW_TAG)
    assert p.status == "ignored", p


@pytest.mark.parametrize("title", [
    "Discraft Z Buzzz Midrange SS 175g", "DISCRAFT Z BUZZZ MIDRANGE OS 177G", "Discraft Z Zone Rare Pink OS 175g",
    "Discraft ESP Buzzz Condition SS", "Discraft SS Rare Buzzz Z",
])
def test_review_a_two_letter_tail_behind_filler_words_is_still_the_sibling_mold(title):
    p = parse_listing(title, "", "New", NEW_TAG)
    assert p.status != "matched" and disc_key(p) is None, p


@pytest.mark.parametrize("title", [
    "Discraft Z Buzzz Midrange 175g Pink Free Shipping", "Discraft Z Zone 173g Approach Rare Pink Stamp",
    "Discraft Z Buzzz Midrange 177g Max Distance",
])
def test_review_filler_words_alone_do_not_make_a_sibling(title):
    assert parse_listing(title, "", "New", NEW_TAG).status == "matched", title


@pytest.mark.parametrize("title", [
    # the qualifier of a plastic often comes after the mold: "P2 Flex 3", "Judge Chameleon"
    "Discmania D-Line P3 Flex 2", "Discmania D-Line P2 Flex 3 173g", "Discraft ESP Buzzz FLX 177g",
    "Dynamic Discs Lucid Judge Chameleon 174g", "Dynamic Discs Fuzion Judge Orbit", "Innova Star Destroyer Shimmer 175g",
])
def test_review_a_plastic_qualifier_behind_the_mold_is_not_the_plain_plastic(title):
    p = parse_listing(title, "", "New", NEW_TAG)
    assert p.status != "matched" and disc_key(p) is None, p


@pytest.mark.parametrize("title", [
    "Innova Star Destroyer Soft Feel 175g", "Innova Star Destroyer Hard To Find", "Discraft ESP Buzzz Metal Detector Find",
    "Innova Star Destroyer Medium Weight 175g", "Innova Star Destroyer Ice Blue", "Discraft ESP Buzzz Light Weight 150g",
])
def test_review_ordinary_adjectives_behind_the_mold_do_not_cost_the_match(title):
    assert parse_listing(title, "", "New", NEW_TAG).status == "matched", title
