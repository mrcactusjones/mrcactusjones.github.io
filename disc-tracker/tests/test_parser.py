"""Offline tests for disctracker.parser (DESIGN.md section 4).

Titles are hand-written in the shapes Shopify disc stores actually use. Nothing
here touches the network.
"""
from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path

import pytest

from disctracker import parser
from disctracker.models import ParsedListing, disc_key, key_slug
from disctracker.parser import PARSER_VERSION, parse_listing, parse_weight

DATA = Path(parser.__file__).resolve().parent / "data"
STATUSES = {"matched", "review", "ignored", "unparsed"}
FLAGS = {"oop", "ink", "dyed", "signed", "prototype", "stamped_error"}
TYPES = {"Distance Driver", "Fairway Driver", "Midrange", "Putter", "Approach", ""}


def P(title, vendor="", product_type="", tags=()) -> ParsedListing:
    return parse_listing(title, vendor, product_type, tags)


def ident(p: ParsedListing) -> tuple[str, str, str, str]:
    return p.status, p.manufacturer, p.mold, p.plastic


# ---------------------------------------------------------------- parse_weight

@pytest.mark.parametrize("text,expected", [
    ("173g", 173), ("173 g", 173), ("173G", 173), ("173gr", 173), ("175 grams", 175),
    ("173", 173),                      # variants are often just the number
    ("175g / Blue", 175), ("Weight: 168g", 168), ("174.5g", 174), ("170g+", 170),
    ("175g 10/10", 175),               # a grade is not a range
    ("100g", 100), ("200g", 200),
    ("170-175g", None), ("170 - 175 g", None), ("170 to 175g", None), ("172/173g", None),
    ("172-174", None),
    ("99g", None), ("201g", None), ("250g", None), ("20g", None), ("1750g", None),
    ("Max Weight", None), ("Default Title", None), ("", None), ("   ", None),
    (None, None), (173, None),
])
def test_parse_weight(text, expected):
    assert parse_weight(text) == expected


# ---------------------------------------------------------------- data files

@pytest.fixture(scope="module")
def molds_json():
    return json.loads((DATA / "molds.json").read_text(encoding="utf-8"))["molds"]


def test_parser_version_is_a_positive_int():
    # not pinned to a number: DESIGN.md says to bump it whenever the rules or the data change
    assert isinstance(PARSER_VERSION, int) and not isinstance(PARSER_VERSION, bool) and PARSER_VERSION >= 1


def test_data_files_are_valid_utf8_json():
    for name in ("manufacturers.json", "molds.json", "plastics.json", "editions.json"):
        json.loads((DATA / name).read_text(encoding="utf-8"))


def test_every_mold_entry_is_well_formed(molds_json):
    manufacturers = {m["name"] for m in json.loads((DATA / "manufacturers.json").read_text("utf-8"))["manufacturers"]}
    seen = set()
    for e in molds_json:
        assert set(e) <= {"manufacturer", "mold", "type", "aliases"}, e   # no flight numbers sneak in
        assert e["manufacturer"] in manufacturers, e
        assert e["mold"].strip() == e["mold"] and e["mold"], e
        assert e["type"] in TYPES, e
        assert all(isinstance(a, str) and a for a in e.get("aliases", [])), e
        ident_key = (e["manufacturer"], parser._key(e["mold"]))
        assert ident_key not in seen, f"duplicate mold {e}"
        seen.add(ident_key)
        assert not re.search(r"\d+\s*/\s*\d+\s*/", e["mold"]), e


def test_seed_list_is_substantial_and_covers_the_big_brands(molds_json):
    assert len(molds_json) >= 200
    brands = {e["manufacturer"] for e in molds_json}
    assert {"Innova", "Discraft", "Dynamic Discs", "Latitude 64", "Westside Discs", "Discmania",
            "MVP", "Axiom", "Streamline", "Prodigy", "Kastaplast", "Gateway"} <= brands
    # most molds carry a speed class; a handful are knowingly left blank
    typed = sum(1 for e in molds_json if e["type"])
    assert typed / len(molds_json) > 0.7


def test_plastic_and_edition_vocabularies_load():
    plastics = json.loads((DATA / "plastics.json").read_text("utf-8"))
    assert plastics["generic"] and "Innova" in plastics["manufacturers"]
    names = {p["name"] for p in plastics["manufacturers"]["Innova"]}
    assert {"Champion", "Star", "GStar", "DX", "Pro", "KC Pro", "Blizzard", "XT", "Nexus"} <= names
    assert {"ESP", "Z", "Jawbreaker", "Titanium", "Big Z", "Cryztal", "Elite Z", "Pro-D", "ColorShift"} <= \
        {p["name"] for p in plastics["manufacturers"]["Discraft"]}
    assert {"Lucid", "Fuzion", "Classic", "Prime", "Moonshine", "BioFuzion"} <= \
        {p["name"] for p in plastics["manufacturers"]["Dynamic Discs"]}
    assert {"Opto", "Gold", "Grip", "Retro", "Zero"} <= {p["name"] for p in plastics["manufacturers"]["Latitude 64"]}
    assert {"Tournament", "VIP", "BT"} <= {p["name"] for p in plastics["manufacturers"]["Westside Discs"]}
    assert {"C-Line", "S-Line", "P-Line", "D-Line", "Neo", "Lux", "Exo"} <= \
        {p["name"] for p in plastics["manufacturers"]["Discmania"]}
    for brand in ("MVP", "Axiom"):
        assert {"Neutron", "Proton", "Electron", "Fission", "Plasma", "Eclipse", "Cosmic Neutron"} <= \
            {p["name"] for p in plastics["manufacturers"][brand]}
    assert {"400", "500", "750", "350G", "400G", "Air"} <= {p["name"] for p in plastics["manufacturers"]["Prodigy"]}
    editions = [e["name"] for e in json.loads((DATA / "editions.json").read_text("utf-8"))["editions"]]
    assert {"tour series", "team series", "first run", "signature series", "limited edition", "prototype",
            "factory second", "glow", "misprint"} <= set(editions)
    assert editions == [e.lower() for e in editions]


@pytest.mark.parametrize("vendor,expected", [
    ("Innova Champion Discs", "Innova"), ("Innova Discs", "Innova"), ("INNOVA", "Innova"),
    ("Discraft Inc", "Discraft"), ("Discraft", "Discraft"),
    ("Dynamic Discs", "Dynamic Discs"), ("dynamic discs", "Dynamic Discs"),
    ("MVP Disc Sports", "MVP"), ("Latitude 64", "Latitude 64"), ("Latitude 64°", "Latitude 64"),
    ("Westside Discs", "Westside Discs"), ("Discmania", "Discmania"), ("Prodigy Disc", "Prodigy"),
    ("Prodigy Discs", "Prodigy"), ("Axiom Discs", "Axiom"), ("Streamline Discs", "Streamline"),
    ("Gateway Disc Sports", "Gateway"), ("Kastaplast", "Kastaplast"), ("Lone Star Disc", "Lone Star Disc"),
    ("Legacy Discs", "Legacy Discs"), ("Millennium Golf Discs", "Millennium"),
    ("Thought Space Athletics", "Thought Space Athletics"), ("Infinite Discs", "Infinite Discs"),
    ("Clash Discs", "Clash Discs"), ("RPM Discs", "RPM Discs"), ("Wham-O", "Wham-O"),
    ("Viking Discs", "Viking Discs"), ("Doomsday Discs", "Doomsday Discs"),
])
def test_vendor_strings_resolve_even_with_no_mold(vendor, expected):
    p = P("175g", vendor)
    assert p.manufacturer == expected
    assert p.status == "unparsed"          # a vendor alone never makes a disc


@pytest.mark.parametrize("vendor", ["", "Gotta Go Gotta Throw", "Some Random Store", "Hyperflite", None])
def test_unknown_vendor_does_not_invent_a_manufacturer(vendor):
    assert P("175g", vendor).manufacturer == ""


def test_every_mold_resolves_from_vendor_plus_name(molds_json):
    """Self-consistency: no mold name collides with a brand, plastic, edition or ignore word."""
    bad = []
    for e in molds_json:
        p = P(f"{e['mold']} 175g", e["manufacturer"])
        if (p.status, p.manufacturer, p.mold, p.disc_type) != ("matched", e["manufacturer"], e["mold"], e["type"]):
            bad.append((e["mold"], p.status, p.mold))
    assert not bad, bad


def test_every_mold_and_plastic_combination_round_trips(molds_json):
    plastics = json.loads((DATA / "plastics.json").read_text("utf-8"))["manufacturers"]
    bad = []
    for e in molds_json:
        for pl in plastics.get(e["manufacturer"], []):
            title = f"{e['manufacturer']} {pl['name']} {e['mold']} 173g"
            p = P(title)
            if (p.status, p.manufacturer, p.mold, p.plastic) != ("matched", e["manufacturer"], e["mold"], pl["name"]):
                bad.append((title, ident(p)))
    assert not bad, bad[:10]


def test_every_mold_survives_the_other_word_order(molds_json):
    bad = []
    for e in molds_json:
        title = f"{e['mold']} - {e['manufacturer']} (new) 170-175g"
        p = P(title)
        if (p.status, p.manufacturer, p.mold) != ("matched", e["manufacturer"], e["mold"]):
            bad.append((title, ident(p)))
    assert not bad, bad[:10]


# ---------------------------------------------------------------- the contract's own examples

def test_contract_example_full_noisy_title():
    p = P("Innova Star Destroyer 175g Ricky Wysocki Tour Series 2015 OOP 9/10", "Innova Champion Discs")
    assert (p.status, p.manufacturer, p.mold, p.plastic) == ("matched", "Innova", "Destroyer", "Star")
    assert p.edition == "tour series"
    assert p.player == "Ricky Wysocki"
    assert p.year == 2015
    assert p.condition == "used" and p.grade == 9.0
    assert p.flags == ["oop"]
    assert p.disc_type == "Distance Driver"
    assert p.confidence >= 0.85


def test_contract_example_champion_roc3():
    p = P("Champion Roc3", "Innova Champion Discs")
    assert ident(p) == ("matched", "Innova", "Roc3", "Champion")
    assert p.condition == "new" and p.grade is None and p.flags == [] and p.year is None
    assert p.disc_type == "Midrange"


def test_contract_example_used_dx_leopard3():
    p = P("[Used] DX Leopard3 - 168g", "Innova")
    assert ident(p) == ("matched", "Innova", "Leopard3", "DX")
    assert p.condition == "used" and p.grade is None


def test_contract_example_latitude_opto_river():
    p = P("Latitude 64 Opto River", "Latitude 64")
    assert ident(p) == ("matched", "Latitude 64", "River", "Opto")
    assert p.disc_type == "Fairway Driver"


def test_matched_listing_gets_a_disc_key_with_five_fields():
    key = disc_key(P("Innova Star Destroyer Tour Series Ricky Wysocki", "Innova"))
    assert key == "innova|destroyer|star|tour series|ricky wysocki"
    assert key_slug(key) == "innova-destroyer-star-tour-series-ricky-wysocki"
    assert disc_key(P("Innova Star 175g", "Innova")) is None


# ---------------------------------------------------------------- realistic catalogue titles

REALISTIC = [
    # title, vendor, manufacturer, mold, plastic  (all must come out `matched`)
    ("Innova Star Destroyer 175g", "Innova Champion Discs", "Innova", "Destroyer", "Star"),
    ("Innova Blizzard Champion Wraith 145g", "Innova Champion Discs", "Innova", "Wraith", "Blizzard"),
    ("Innova GStar Teebird3 Disc Golf Fairway Driver", "Innova", "Innova", "Teebird3", "GStar"),
    ("Innova G-Star Eagle", "Innova", "Innova", "Eagle", "GStar"),
    ("Innova KC Pro Aviar Putter", "Innova", "Innova", "Aviar", "KC Pro"),
    ("KC Roc", "Innova", "Innova", "Roc", "KC Pro"),
    ("Innova R-Pro Mako3", "Innova", "Innova", "Mako3", "R-Pro"),
    ("Innova Metal Flake Champion Katana", "Innova", "Innova", "Katana", "Metal Flake Champion"),
    ("Innova Luster Champion Valkyrie", "Innova", "Innova", "Valkyrie", "Luster Champion"),
    ("Innova Champion Luster Valkyrie", "Innova", "Innova", "Valkyrie", "Luster Champion"),
    ("Innova Halo Star Firebird", "Innova", "Innova", "Firebird", "Halo Star"),
    ("Innova Proto Glow Champion Thunderbird", "Innova", "Innova", "Thunderbird", "Proto Glow"),
    ("Innova Pro Aviar", "Innova", "Innova", "Aviar", "Pro"),
    ("Innova XT Pig", "Innova", "Innova", "Pig", "XT"),
    ("Innova Nexus Dart", "Innova", "Innova", "Dart", "Nexus"),
    ("Innova DX Aviar Classic Putter", "Innova", "Innova", "Aviar Classic", "DX"),
    ("Innova Champion Discs Wraith", "Innova", "Innova", "Wraith", ""),
    ("Discraft ESP Buzzz 177g", "Discraft Inc", "Discraft", "Buzzz", "ESP"),
    ("Discraft Z Buzz", "Discraft", "Discraft", "Buzzz", "Z"),
    ("Discraft Big Z Anax", "Discraft", "Discraft", "Anax", "Big Z"),
    ("Discraft Elite Z Undertaker", "Discraft", "Discraft", "Undertaker", "Elite Z"),
    ("Discraft Z Lite Wasp", "Discraft", "Discraft", "Wasp", "Z Lite"),
    ("Discraft Jawbreaker Zone", "Discraft", "Discraft", "Zone", "Jawbreaker"),
    ("Discraft Z Metallic Heat", "Discraft", "Discraft", "Heat", "Metallic Z"),
    ("Discraft Titanium Zeus 173-174g", "Discraft", "Discraft", "Zeus", "Titanium"),
    ("Discraft Cryztal Magnet", "Discraft", "Discraft", "Magnet", "Cryztal"),
    ("Discraft Pro-D Challenger 172g", "Discraft", "Discraft", "Challenger", "Pro-D"),
    ("Discraft ColorShift Nuke", "Discraft", "Discraft", "Nuke", "ColorShift"),
    ("Discraft Z Nuke SS", "Discraft", "Discraft", "Nuke SS", "Z"),
    ("Lucid Judge", "Dynamic Discs", "Dynamic Discs", "Judge", "Lucid"),
    ("Dynamic Discs Fuzion Truth 177g", "Dynamic Discs", "Dynamic Discs", "Truth", "Fuzion"),
    ("Classic Soft Judge", "Dynamic Discs", "Dynamic Discs", "Judge", "Classic Soft"),
    ("Lucid Air Escape", "Dynamic Discs", "Dynamic Discs", "Escape", "Lucid Air"),
    ("Prime Trespass", "Dynamic Discs", "Dynamic Discs", "Trespass", "Prime"),
    ("BioFuzion Warden", "Dynamic Discs", "Dynamic Discs", "Warden", "BioFuzion"),
    ("Lucid-X Glimmer Maverick", "Dynamic Discs", "Dynamic Discs", "Maverick", "Lucid-X"),
    ("Lucid Ice Verdict", "Dynamic Discs", "Dynamic Discs", "Verdict", "Lucid Ice"),
    ("Moonshine Raider", "Dynamic Discs", "Dynamic Discs", "Raider", "Moonshine"),
    ("Latitude 64 Opto Air Pure", "Latitude 64", "Latitude 64", "Pure", "Opto Air"),
    ("Latitude 64 Gold Line Pioneer", "Latitude 64", "Latitude 64", "Pioneer", "Gold"),
    ("Latitude 64 Zero Hard Compass", "Latitude 64", "Latitude 64", "Compass", "Zero Hard"),
    ("Retro Line Explorer", "Latitude 64", "Latitude 64", "Explorer", "Retro"),
    ("Latitude 64 Grip Ballista Pro", "Latitude 64", "Latitude 64", "Ballista Pro", "Grip"),
    ("Westside Discs Tournament Hatchet", "Westside Discs", "Westside Discs", "Hatchet", "Tournament"),
    ("VIP Ice Underworld", "Westside Discs", "Westside Discs", "Underworld", "VIP Ice"),
    ("BT Soft Harp", "Westside Discs", "Westside Discs", "Harp", "BT Soft"),
    ("Discmania C-Line P2", "Discmania", "Discmania", "P2", "C-Line"),
    ("Discmania Neo Method", "Discmania", "Discmania", "Method", "Neo"),
    ("Discmania S-Line DD3 175g", "Discmania", "Discmania", "DD3", "S-Line"),
    ("Discmania Swirly S-Line MD3", "Discmania", "Discmania", "MD3", "Swirly S-Line"),
    ("MVP Neutron Volt", "MVP Disc Sports", "MVP", "Volt", "Neutron"),
    ("MVP Plasma Ion", "MVP Disc Sports", "MVP", "Ion", "Plasma"),
    ("MVP Cosmic Neutron Wave", "MVP Disc Sports", "MVP", "Wave", "Cosmic Neutron"),
    ("Total Eclipse Octane", "MVP Disc Sports", "MVP", "Octane", "Total Eclipse"),
    ("Axiom Electron Firm Envy", "Axiom Discs", "Axiom", "Envy", "Electron Firm"),
    ("Axiom Fission Hex", "Axiom Discs", "Axiom", "Hex", "Fission"),
    ("Streamline Cosmic Electron Pilot", "Streamline Discs", "Streamline", "Pilot", "Cosmic Electron"),
    ("Prodigy 400 Series PA-3 173g", "Prodigy Disc", "Prodigy", "PA-3", "400"),
    ("Prodigy 400G D3 Max", "Prodigy Disc", "Prodigy", "D3 Max", "400G"),
    ("Prodigy 750 F5", "Prodigy Disc", "Prodigy", "F5", "750"),
    ("Prodigy 300 A3", "Prodigy Disc", "Prodigy", "A3", "300"),
    ("Prodigy Air Spectrum M3", "Prodigy Disc", "Prodigy", "M3", "Air Spectrum"),
    ("Kastaplast K1 Berg", "Kastaplast", "Kastaplast", "Berg", "K1"),
    ("K3 Reko", "Kastaplast", "Kastaplast", "Reko", "K3"),
    ("Gateway Super Stupid Soft Wizard", "Gateway Disc Sports", "Gateway", "Wizard", "Super Stupid Soft"),
    ("Gateway Wizard SSS 170", "Gateway Disc Sports", "Gateway", "Wizard", "Super Stupid Soft"),
]


@pytest.mark.parametrize("title,vendor,mfr,mold,plastic", [
    pytest.param(*row, id=f"{i}-{row[0][:40]}") for i, row in enumerate(REALISTIC)
])
def test_realistic_titles(title, vendor, mfr, mold, plastic):
    p = P(title, vendor)
    assert (p.status, p.manufacturer, p.mold, p.plastic) == ("matched", mfr, mold, plastic), p


def test_realistic_title_with_two_molds_is_review_not_matched():
    p = P("Westside Origio Harp Swan 1", "Westside Discs")
    assert p.status == "review" and p.confidence < 0.7


def test_origio_burst_is_a_plastic_not_the_burst_mold():
    # Burst is a Westside mold *and* the second half of the Origio Burst plastic
    assert ident(P("Westside Discs Origio Burst Harp", "Westside Discs")) == \
        ("matched", "Westside Discs", "Harp", "Origio Burst")
    assert ident(P("Westside Origio Burst Swan 1", "Westside Discs")) == \
        ("matched", "Westside Discs", "Swan 1", "Origio Burst")
    assert ident(P("Westside Discs Origio Burst Burst 173g")) == ("matched", "Westside Discs", "Burst", "Origio Burst")


def test_prodigy_max_variant_matches_the_listed_mold():
    p = P("Prodigy 500 Spectrum D2 Max", "Prodigy Disc")
    assert ident(p) == ("matched", "Prodigy", "D2 Max", "Spectrum")


def test_title_order_and_vendor_do_not_matter_when_title_names_the_brand():
    a = P("Star Destroyer Innova", "")
    b = P("Innova Star Destroyer", "Some Reseller")
    assert ident(a) == ident(b) == ("matched", "Innova", "Destroyer", "Star")


# ---------------------------------------------------------------- short mold names / substrings of words

@pytest.mark.parametrize("title,vendor,mold", [
    ("Rocket Star Wraith 170g", "Innova", "Wraith"),         # Roc is not in Rocket
    ("Innova Rock Star Wraith", "", "Wraith"),               # ...nor in Rock
    ("Star Aviary Birdie", "Innova", "Birdie"),              # Aviar is not in Aviary
    ("Pigeon Pig Star", "Innova", "Pig"),
    ("Heat Wheat ESP", "Discraft", "Heat"),
    ("Innova Intern Pattern Tern", "", "Tern"),
    ("Discraft Lunar Luna Z", "", "Luna"),
    ("Discraft Forced Force ESP", "Discraft", "Force"),
    ("Discraft Magnetic Magnet ESP", "Discraft", "Magnet"),
    ("Million Ion Neutron", "MVP", "Ion"),
    ("Innova Beastly Beast Champion", "Innova", "Beast"),
    ("Innova Star Darth Dart", "Innova", "Dart"),
    ("Ozone Zone ESP", "Discraft", "Zone"),
    ("Dynamic Discs Lucid Judgement Judge", "Dynamic Discs", "Judge"),
])
def test_mold_names_never_match_inside_longer_words(title, vendor, mold):
    p = P(title, vendor)
    assert p.mold == mold and p.status == "matched", p


@pytest.mark.parametrize("title,vendor", [
    ("Rocket Star", "Innova"), ("Innova Rock Star", "Innova"), ("Innova Star Aviary", "Innova"),
    ("Discraft Forced ESP", "Discraft"), ("Axiom Atomic Neutron", "Axiom"), ("RPM Atomic", "RPM Discs"),
    ("Innova Star Pigeon", "Innova"), ("Innova Star Eagles", "Innova"),
])
def test_words_that_merely_contain_a_mold_do_not_produce_a_match(title, vendor):
    p = P(title, vendor)
    assert p.status != "matched", p


def test_short_mold_without_any_support_is_only_review():
    for t in ("Pig", "Roc", "Hex", "King", "TL", "Ion"):
        assert P(t).status != "matched", t
    assert P("Star Pig").status == "matched"          # a plastic from the same maker backs it
    assert P("Pig", "Innova").status == "matched"     # so does the vendor


# ---------------------------------------------------------------- molds with digits and spelling variants

@pytest.mark.parametrize("title,mold", [
    ("Innova Champion Roc 3 175g", "Roc3"), ("Innova DX Roc3", "Roc3"), ("Innova DX Roc-3", "Roc3"),
    ("Innova Star ROC3", "Roc3"), ("Innova Star Roc4", "Roc4"), ("Innova Star Roc 4", "Roc4"),
    ("Innova Star Rocx3", "Rocx3"), ("Innova Star Roc X3", "Rocx3"),
    ("Innova Star Roc", "Roc"),
    ("Innova Star Leopard 3", "Leopard3"), ("Innova Star Leopard", "Leopard"),
    ("Innova Pro TeeBird3 172", "Teebird3"), ("Innova Star Tee Bird 3", "Teebird3"),
    ("Innova Star Teebird", "Teebird"), ("Innova Star Tee-Bird", "Teebird"),
    ("Innova GStar Aviar3", "Aviar3"), ("Innova Star Aviar 3", "Aviar3"), ("Innova Star Aviar", "Aviar"),
    ("Innova KC Pro Aviar X3", "AviarX3"), ("Innova Star AviarX3", "AviarX3"),
    ("Innova DX Aviar P&A", "Aviar P&A"), ("Innova DX Aviar Classic", "Aviar Classic"),
    ("Innova DX Wombat 3", "Wombat3"), ("Innova DX Mako 3", "Mako3"), ("Innova Star Shark 3", "Shark3"),
    ("Innova Star Shark", "Shark"), ("Innova Star TL3", "TL3"), ("Innova Star TL", "TL"),
    ("Innova Star Thunder Bird", "Thunderbird"), ("Innova Star Fire Bird", "Firebird"),
    ("Discraft Z Buzzz SS", "Buzzz SS"), ("Discraft Z Buzzz OS", "Buzzz OS"), ("Discraft Z Buzzz", "Buzzz"),
    ("Discraft Z Buzz", "Buzzz"), ("Discraft Z BUZZZ", "Buzzz"),
    ("Discraft ESP Zone OS", "Zone OS"), ("Discraft ESP Zone", "Zone"), ("Discraft ESP ZoneOS", "Zone OS"),
    ("Discraft Z Nuke OS", "Nuke OS"), ("Discraft Z Nuke", "Nuke"),
    ("Discraft ESP Ringer GT", "Ringer GT"), ("Discraft Z Challenger SS", "Challenger SS"),
    ("Prodigy 400 PA3", "PA-3"), ("Prodigy 400 PA 3", "PA-3"), ("Prodigy 400 D3 Max", "D3 Max"),
    ("Prodigy 400 D3", "D3"), ("Prodigy 400 H3 V2", "H3 V2"), ("Prodigy 400 H3V2", "H3 V2"),
    ("Discmania S-Line MD3", "MD3"), ("Discmania C-Line CD2", "CD2"), ("Discmania S-Line Cloudbreaker", "Cloud Breaker"),
    ("Latitude 64 Opto Ballista Pro", "Ballista Pro"), ("Latitude 64 Opto Ballista", "Ballista"),
    ("Latitude 64 Gold Saint Pro", "Saint Pro"), ("Latitude 64 Gold Saint", "Saint"),
    ("Westside VIP Swan 2", "Swan 2"), ("Westside VIP Swan 1", "Swan 1"),
])
def test_digit_and_spacing_variants_resolve_to_the_right_mold(title, mold):
    p = P(title)
    assert p.mold == mold and p.status == "matched", p


def test_longest_mold_wins_when_a_shorter_one_is_nested():
    assert P("Discraft Z Zone OS", "Discraft").mold == "Zone OS"
    assert P("Discraft Z Nuke SS", "Discraft").mold == "Nuke SS"
    assert P("Latitude 64 Opto Ballista Pro", "Latitude 64").mold == "Ballista Pro"
    assert P("Prodigy 400 D3 Max", "Prodigy").mold == "D3 Max"


# ---------------------------------------------------------------- same name, different manufacturer (synthetic collisions)

@pytest.fixture
def twin_zone(monkeypatch):
    """Pretend two makers both sell a 'Zone', and two sell a 'Gizmo', to exercise disambiguation."""
    idx = dict(parser._MOLD_IDX)
    idx["zone"] = (parser._Mold("Discraft", "Zone", "Approach", "zone"), parser._Mold("Axiom", "Zone", "", "zone"))
    idx["gizmo"] = (parser._Mold("Innova", "Gizmo", "", "gizmo"), parser._Mold("Prodigy", "Gizmo", "", "gizmo"))
    monkeypatch.setattr(parser, "_MOLD_IDX", idx)


@pytest.mark.parametrize("title,vendor,mfr", [
    ("Zone", "Discraft", "Discraft"),
    ("Zone", "Axiom Discs", "Axiom"),
    ("Axiom Zone", "", "Axiom"),
    ("Discraft Zone", "", "Discraft"),
    ("Discraft Zone", "Axiom", "Discraft"),            # the title names the brand; it outranks the vendor
    ("Z Zone", "", "Discraft"),                        # Z is Discraft's plastic
    ("ESP Zone", "", "Discraft"),
    ("Neutron Zone", "", "Axiom"),                     # Neutron only belongs to the MVP family; Axiom is the Zone owner
    ("Gizmo 400", "", "Prodigy"),
    ("Star Gizmo", "", "Innova"),
])
def test_same_mold_name_in_two_brands_is_decided_by_evidence(twin_zone, title, vendor, mfr):
    p = P(title, vendor)
    assert (p.status, p.manufacturer, p.mold) == ("matched", mfr, "Zone" if "Zone" in title else "Gizmo"), p


@pytest.mark.parametrize("title", ["Zone", "Zone 175g", "Gizmo", "Pink Zone"])
def test_same_mold_name_with_no_evidence_is_review_and_names_no_brand(twin_zone, title):
    p = P(title)
    assert p.status == "review" and p.manufacturer == "" and p.confidence < 0.7


def test_vendor_for_a_third_brand_cannot_pick_between_twins(twin_zone):
    p = P("Zone", "Innova")
    assert p.status == "review"


def test_plastic_words_that_look_like_other_molds_do_not_hijack_the_match():
    # Westside makes a "Burst"; Dynamic sells "Prime Burst" and "Fuzion Burst" plastics.
    assert ident(P("Prime Burst Judge 174g")) == ("matched", "Dynamic Discs", "Judge", "Prime Burst")
    assert P("Fuzion Burst Judge").manufacturer == "Dynamic Discs"
    assert P("Fuzion Burst Judge").mold == "Judge"


# ---------------------------------------------------------------- manufacturer: vendor vs title vs mold

def test_vendor_decides_when_title_has_no_brand():
    assert P("Star Destroyer", "Innova").manufacturer == "Innova"


def test_title_brand_beats_a_reseller_vendor():
    p = P("Dynamic Discs Lucid Judge", "Innova")
    assert ident(p) == ("matched", "Dynamic Discs", "Judge", "Lucid")


def test_mold_from_another_brand_than_the_vendor_is_a_conflict_not_a_match():
    p = P("Star Zone", "Innova")
    assert p.status == "review" and p.confidence <= 0.4
    assert p.manufacturer == "Discraft"          # best guess is kept for a human to confirm


def test_title_naming_one_brand_with_another_brands_mold_is_a_conflict():
    p = P("Discraft Star Destroyer", "Innova")
    assert p.status == "review" and p.confidence <= 0.4


def test_brand_inferred_from_a_unique_mold_is_matched_with_lower_confidence():
    inferred = P("Star Destroyer 175g")
    vendor = P("Star Destroyer 175g", "Innova")
    assert inferred.status == "matched" and inferred.manufacturer == "Innova"
    assert inferred.confidence < vendor.confidence


def test_a_store_name_in_the_vendor_field_does_not_block_inference():
    p = P("Star Destroyer 175g", "Gotta Go Gotta Throw")
    assert ident(p) == ("matched", "Innova", "Destroyer", "Star")


def test_champion_in_innova_champion_discs_is_not_a_plastic_but_champion_wraith_is():
    assert P("Innova Champion Discs Wraith").plastic == ""
    assert P("Innova Champion Wraith").plastic == "Champion"
    assert P("Wraith", "Innova Champion Discs").plastic == ""


def test_plastic_from_another_brand_is_not_applied():
    # "Lucid" is Dynamic's plastic; on an Innova disc it is just a word.
    assert P("Innova Lucid Star Wraith").plastic == "Star"
    assert P("Innova Z Wraith").plastic == ""


def test_longest_plastic_wins():
    assert P("Dynamic Discs Lucid Air Judge").plastic == "Lucid Air"
    assert P("Dynamic Discs Lucid Judge").plastic == "Lucid"
    assert P("Innova Champion Metal Flake Wraith").plastic == "Metal Flake Champion"
    assert P("Discraft Big Z Buzzz").plastic == "Big Z"
    assert P("Discraft Elite Z Buzzz").plastic == "Elite Z"
    assert P("Latitude 64 Opto Ice Ballista").plastic == "Opto Ice"
    assert P("Axiom Total Eclipse Envy").plastic == "Total Eclipse"


# ---------------------------------------------------------------- condition and grade

@pytest.mark.parametrize("title,product_type,tags,condition,grade", [
    ("[Used] DX Leopard3 - 168g", "", (), "used", None),
    ("Used Innova Star Destroyer 8/10", "", (), "used", 8.0),
    ("USED Innova DX Roc 168g 7/10", "", (), "used", 7.0),
    ("Innova Champion Wraith 171g Sleepy 7", "", (), "used", 7.0),
    ("Innova Star Destroyer Grade 9", "", (), "used", 9.0),
    ("Innova Star Destroyer 8.5/10", "", (), "used", 8.5),
    ("Innova Star Destroyer 9 out of 10", "", (), "used", 9.0),
    ("Innova Star Destroyer 10/10", "", (), "used", 10.0),
    ("Innova DX Roc beat in 150g", "", (), "used", None),
    ("Innova DX Roc beat-in", "", (), "used", None),
    ("Pre-Owned Discraft Z Buzzz", "", (), "used", None),
    ("Preowned Discraft Z Buzzz", "", (), "used", None),
    ("Second Hand Innova Star Destroyer", "", (), "used", None),
    ("Innova Star Destroyer", "", ("Used",), "used", None),
    ("Innova Star Destroyer", "", ("disc-condition:used",), "used", None),
    ("Innova Star Destroyer", "Used Discs", (), "used", None),
    ("Innova Star Destroyer", "", "Innova, Used, Distance Driver", "used", None),   # tags as a comma string
    ("Innova Star Destroyer 175g", "", (), "new", None),
    ("Innova Star Destroyer never used", "", (), "new", None),
    ("Innova Star Destroyer unused 175g", "", (), "new", None),
    ("Innova Star Destroyer", "", ("New", "Innova"), "new", None),
    ("Innova Star Destroyer", "", ("Unused",), "new", None),
])
def test_condition_and_grade(title, product_type, tags, condition, grade):
    p = P(title, "Innova", product_type, tags)
    assert (p.condition, p.grade) == (condition, grade), p


def test_used_discs_still_resolve_to_the_same_disc():
    new = P("Innova Star Destroyer 175g", "Innova")
    used = P("Used Innova Star Destroyer 175g 8/10", "Innova")
    assert disc_key(new) == disc_key(used)
    assert (new.condition, used.condition) == ("new", "used")


def test_grade_is_not_confused_with_a_weight_or_plastic():
    p = P("Innova Champion Roc3 175g 9/10", "Innova")
    assert ident(p) == ("matched", "Innova", "Roc3", "Champion")
    assert p.grade == 9.0


# ---------------------------------------------------------------- edition / player / year / flags

def test_tour_series_player_after_year():
    p = P("Innova Champion Valkyrie Tour Series 2018 Ricky Wysocki", "Innova")
    assert (p.edition, p.player, p.year) == ("tour series", "Ricky Wysocki", 2018)
    assert p.mold == "Valkyrie"


def test_tour_series_player_before_year_and_marker():
    p = P("Calvin Heimburg 2019 Tour Series Star Destroyer", "Innova")
    assert (p.edition, p.player, p.year) == ("tour series", "Calvin Heimburg", 2019)
    assert ident(p) == ("matched", "Innova", "Destroyer", "Star")


def test_team_series():
    p = P("Ricky Wysocki Team Series Star Boss 2020", "Innova")
    assert (p.edition, p.player, p.year, p.mold) == ("team series", "Ricky Wysocki", 2020, "Boss")


@pytest.mark.parametrize("title,vendor,player", [
    ("Discraft Paul McBeth Signature Series Z Luna 2021", "Discraft", "Paul McBeth"),
    ("Discraft Z Buzzz Signature Series Paul McBeth", "Discraft", "Paul McBeth"),
    ("Eagle McMahon Signature Series Z Luna", "Discraft", "Eagle McMahon"),     # Eagle is also an Innova mold
    ("Discraft Signature Series Z Luna Anthony Barela", "Discraft", ""),        # not adjacent to the marker: no guess
    ("Hailey King Tour Series Star Destroyer", "Innova", "Hailey King"),        # King is a Westside mold
    ("Innova Star Destroyer - Tour Series - Ricky Wysocki", "Innova", "Ricky Wysocki"),
    ("INNOVA STAR DESTROYER RICKY WYSOCKI TOUR SERIES", "Innova", "Ricky Wysocki"),
    ("innova star destroyer ricky wysocki tour series", "Innova", "Ricky Wysocki"),
    ("INNOVA STAR DESTROYER PAUL MCBETH TOUR SERIES", "Innova", "Paul McBeth"),
    ("Innova Star Destroyer Tour Series", "Innova", ""),
    ("Innova Star Destroyer Ricky Wysocki", "Innova", ""),                      # no marker, no player
    ("Latitude 64 Opto Ballista Pro Tour Series", "Latitude 64", ""),          # two mold words are not a person
    ("Innova Champion Rhyno Tour Series", "Innova", ""),                       # one unknown capitalised word is not a person
    ("Innova Star Wraith 175g Tour Series", "Innova", ""),
])
def test_player_extraction(title, vendor, player):
    assert P(title, vendor).player == player


def test_player_words_are_not_read_as_molds_or_plastics():
    p = P("Eagle McMahon Signature Series Z Luna", "Discraft")
    assert ident(p) == ("matched", "Discraft", "Luna", "Z")        # Eagle did not become the mold
    p = P("Hailey King Tour Series Star Destroyer", "Innova")
    assert ident(p) == ("matched", "Innova", "Destroyer", "Star")  # King did not become the mold


@pytest.mark.parametrize("title,edition", [
    ("Discraft Elite Z Buzzz First Run", "first run"),
    ("Discraft Elite Z Buzzz 1st Run", "first run"),
    ("Discraft Elite Z Buzzz Second Run", "second run"),
    ("Innova Star Wraith Prototype 175g", "prototype"),
    ("Innova Star Destroyer Factory Second 175g", "factory second"),
    ("Innova Star Destroyer Factory Seconds", "factory second"),
    ("Innova Champion Teebird Misprint", "misprint"),
    ("Innova Champion Teebird Stamp Error", "misprint"),
    ("Innova Star Destroyer Limited Edition", "limited edition"),
    ("Innova Star Destroyer Special Edition", "special edition"),
    ("Innova Star Destroyer 25th Anniversary", "anniversary edition"),
    ("Innova Glow Champion Roc3", "glow"),
    ("Innova Champion Glow in the Dark Roc3", "glow"),
    ("Innova Champion GITD Roc3", "glow"),
    ("Discraft ESP Glo Luna", "glow"),
    ("Innova Star Destroyer", ""),
])
def test_editions(title, edition):
    p = P(title, "Innova")
    assert p.edition == edition and p.status == "matched", p


def test_proto_glow_is_a_plastic_not_a_glow_edition():
    p = P("Innova Proto Glow Champion Thunderbird", "Innova")
    assert (p.plastic, p.edition) == ("Proto Glow", "")
    assert P("Innova Color Glow Champion Roc", "Innova").edition == ""


def test_edition_priority_when_several_apply():
    p = P("Innova Champion Glow Roc3 Tour Series Ricky Wysocki", "Innova")
    assert p.edition == "tour series" and p.player == "Ricky Wysocki"


@pytest.mark.parametrize("title,year", [
    ("Innova Star Destroyer 2015", 2015), ("2019 Innova Star Destroyer", 2019),
    ("Innova Star Destroyer 1990", 1990), ("Innova Star Destroyer 2035", 2035),
    ("Innova Star Destroyer 1989", None), ("Innova Star Destroyer 2036", None),
    ("Innova Star Destroyer 175g", None), ("Innova Star Destroyer 1750", None),
])
def test_year(title, year):
    p = P(title, "Innova")
    assert p.year == year and p.mold == "Destroyer"


@pytest.mark.parametrize("title,flags", [
    ("Innova Champion Shryke Signed Dyed Inked OOP", ["dyed", "ink", "oop", "signed"]),
    ("Innova Champion Shryke Autographed", ["signed"]),
    ("Innova Champion Shryke Tie Dye", ["dyed"]),
    ("Innova Champion Shryke Hydro Dipped", ["dyed"]),
    ("Innova Champion Shryke Out of Production", ["oop"]),
    ("Innova Champion Shryke Prototype", ["prototype"]),
    ("Innova Champion Shryke Misprint", ["stamped_error"]),
    ("Innova Champion Shryke Wrong Stamp", ["stamped_error"]),
    ("Innova Champion Shryke Sharpie", ["ink"]),
    ("Innova Champion Shryke", []),
])
def test_flags(title, flags):
    assert P(title, "Innova").flags == flags


def test_flags_are_also_read_from_tags():
    assert P("Innova Champion Shryke", "Innova", "", ("OOP", "Signed")).flags == ["oop", "signed"]


# ---------------------------------------------------------------- non-disc products are ignored

@pytest.mark.parametrize("title,vendor,product_type,tags", [
    ("Innova Discs Dart Disc Golf Bag", "Innova", "", ()),
    ("Dynamic Discs Backpack - Black", "Dynamic Discs", "", ()),
    ("Disc Golf Basket Practice Portable", "", "", ()),
    ("MVP Disc Sports Competition Basket", "MVP Disc Sports", "", ()),
    ("Microfiber Towel Black", "", "", ()),
    ("Discraft Buzzz Towel", "Discraft", "", ()),
    ("Dynamic Discs Hat - Truth", "Dynamic Discs", "", ()),
    ("Trucker Cap", "", "Hats", ()),
    ("Innova T-Shirt Star Destroyer", "Innova", "", ()),
    ("Innova Hoodie", "Innova", "", ()),
    ("Latitude 64 Opto River Shirt", "Latitude 64", "", ()),
    ("Discraft Sticker", "Discraft", "", ()),
    ("MVP Sticker Pack", "MVP Disc Sports", "", ()),
    ("Gift Card", "", "", ()),
    ("$50 Gift Card", "", "Gift Cards", ()),
    ("E-Gift Card", "", "", ()),
    ("Discraft Buzzz Mini Marker Disc", "Discraft", "", ()),
    ("Mini Marker", "", "", ()),
    ("Innova Star Aviar Marker", "Innova", "", ()),
    ("Lucid Judge 3-Pack", "Dynamic Discs", "", ()),
    ("Innova Star Aviar 3 Pack", "Innova", "", ()),
    ("Innova Star Aviar 3pk", "Innova", "", ()),
    ("Disc Golf Starter Set", "", "", ()),
    ("Innova Starter Set - 3 Discs", "Innova", "", ()),
    ("Discraft Set of 3 Buzzz", "Discraft", "", ()),
    ("Lot of 10 Used Discs", "", "", ()),
    ("Mystery Disc", "", "", ()),
    ("Grab Bag Mystery Box", "", "", ()),
    ("Innova Pulsar Ultimate Disc", "Innova", "", ()),
    ("Discraft UltraStar 175g", "Discraft", "", ()),
    ("Bag Tag", "", "", ()),
    ("Disc Golf Cart", "", "", ()),
    ("Innova Star Destroyer", "Innova", "Accessories", ()),
    ("Innova Star Destroyer", "Innova", "Bags", ()),
    ("Innova Star Destroyer", "Innova", "", ("Bags",)),
    ("Innova Star Destroyer", "Innova", "", ("Accessories",)),
    ("Innova Star Destroyer", "Innova", "", ("Apparel",)),
    ("Innova Star Destroyer", "Innova", "", ("Disc Golf Baskets",)),
    ("Route Package Protection", "Route", "", ()),
])
def test_non_disc_products_are_ignored(title, vendor, product_type, tags):
    p = P(title, vendor, product_type, tags)
    assert p.status == "ignored", p
    assert p.mold == "" and p.manufacturer == "" and disc_key(p) is None


@pytest.mark.parametrize("title,vendor,product_type,tags", [
    ("Innova Star Destroyer", "Innova", "Distance Driver", ("Bag Builder", "Wraith")),   # a tag that merely mentions a bag
    ("Innova Star Destroyer", "Innova", "Discs", ()),
    ("Innova Star Destroyer", "Innova", "", ("Distance Drivers", "Innova")),
    ("Discraft Magnet ESP", "Discraft", "Putter", ()),                                    # Magnet is not a "net"
    ("Innova Star Dart", "Innova", "", ()),                                               # Dart is not a "cart"
    ("Innova Star Mako3", "Innova", "", ()),
    ("Innova Star Beast", "Innova", "", ()),
    ("Innova Star Destroyer Reset", "Innova", "", ()),
    ("Latitude 64 Opto Compass Sunset", "Latitude 64", "", ()),
    ("Prodigy 400 Series D2", "Prodigy", "", ()),
])
def test_real_discs_are_not_ignored(title, vendor, product_type, tags):
    assert P(title, vendor, product_type, tags).status == "matched"


# ---------------------------------------------------------------- vendor only / review / unparsed

@pytest.mark.parametrize("title,vendor", [
    ("175g", "Innova"), ("Star 175g Blue", "Innova"), ("Champion", "Innova Champion Discs"), ("Disc", "Discraft"),
    ("Innova Star", "Innova"), ("Prodigy 400 Series", "Prodigy"), ("Latitude 64 Opto", "Latitude 64"),
    ("Wham-O Frisbee Classic", "Wham-O"), ("Lucid Fuzion", "Dynamic Discs"),
])
def test_vendor_and_plastic_with_no_mold_is_unparsed(title, vendor):
    p = P(title, vendor)
    assert p.status == "unparsed" and p.mold == "" and disc_key(p) is None
    assert p.manufacturer != ""


def test_unparsed_still_reports_what_was_understood():
    p = P("Innova Star 175g Used", "Innova")
    assert (p.manufacturer, p.plastic, p.condition, p.status) == ("Innova", "Star", "used", "unparsed")


def test_an_unknown_mold_for_a_known_brand_is_a_review_with_a_guess():
    p = P("Innova Star Frobnicator 175g", "Innova")
    assert p.status == "review" and p.manufacturer == "Innova" and p.mold == "Frobnicator"
    assert p.plastic == "Star" and p.confidence < 0.5 and disc_key(p) is None


def test_two_unknown_words_do_not_make_a_guess():
    assert P("Innova Star Frobnicator Whatsit Gizmotron 175g", "Innova").status == "unparsed"


def test_no_brand_and_no_mold_is_unparsed():
    for t in ("Star 175g", "Blue Disc", "Some Random Thing", "Z", "Neutron"):
        assert P(t).status == "unparsed", t


@pytest.mark.parametrize("title,vendor,expected_mold", [
    ("Innova Star Destoyer", "Innova", "Destroyer"),         # dropped letter
    ("Innova Star Destroyr", "Innova", "Destroyer"),
    ("Innova Star Thunderbrid", "Innova", "Thunderbird"),    # transposed letters
    ("Innova Star Thundrbird", "Innova", "Thunderbird"),
    ("Latitude 64 Opto Ballsta", "Latitude 64", "Ballista"),
    ("Innova Star Leapord", "Innova", None),                 # scores too low to claim a mold
    ("Discraft Z Buzzzz", "Discraft", None),                 # Buzzz + a letter: an extension, not trusted
    ("Innova Star Aviary", "Innova", None),                  # a different word, not a typo
    ("Discraft ESP Forced", "Discraft", None),
])
def test_fuzzy_typos(title, vendor, expected_mold):
    p = P(title, vendor)
    if expected_mold:
        assert (p.status, p.mold) == ("matched", expected_mold)
        assert p.confidence <= 0.85                       # never as sure as an exact hit
    else:
        assert p.status != "matched"


@pytest.mark.parametrize("title,vendor,mold", [
    ("Dynamic Discs Lucid Trespas", "Dynamic Discs", "Trespass"),   # dropped last letter
    ("Innova Star Destroye", "Innova", "Destroyer"),
    ("Innova Star Wraiths", "Innova", "Wraith"),                    # plural / extra letters
    ("Innova Star Eagles", "Innova", "Eagle"),
])
def test_fuzzy_extensions_are_review_with_the_guess_kept(title, vendor, mold):
    p = P(title, vendor)
    assert (p.status, p.mold, p.manufacturer) == ("review", mold, vendor)


def test_fuzzy_is_only_review_when_the_match_is_borderline():
    p = P("Innova Star Destroyed", "Innova")               # a real word, one letter off
    assert p.status == "review" and p.mold == "Destroyer"


def test_fuzzy_never_runs_on_short_words():
    assert P("Innova Star Rocks", "Innova").status != "matched"
    assert P("Innova Star Piggy", "Innova").status != "matched"


def test_fuzzy_without_a_brand_is_never_matched():
    p = P("Star Destoyer")
    assert p.status != "matched"


@pytest.mark.parametrize("title", [
    "Discraft Z Zone GT", "Innova Star Roc Plus", "Innova Star Roc 5", "Innova Star Wraith Max 175g",
    "Innova Star Teebird XL", "Discraft Z Buzzz 2",
])
def test_sibling_molds_we_may_not_know_are_review_not_a_wrong_match(title):
    p = P(title, "Innova" if "Innova" in title else "Discraft")
    assert p.status == "review", p


def test_max_weight_is_not_a_mold_variant():
    assert P("Innova Star Wraith Max Weight 175g", "Innova").status == "matched"
    assert P("Innova Star Wraith Max Weights", "Innova").status == "matched"


# ---------------------------------------------------------------- odd input

def test_empty_and_none_inputs():
    for args in [("",), ("   ",), (None,), ("", ""), ("", "Innova"), ("!!!", "Innova"), ("---",)]:
        p = P(*args)
        assert p.status in {"unparsed"} and p.mold == ""
    assert P("").confidence == 0.0


def test_non_string_inputs_do_not_raise():
    p = P(12345, 99, ["x"], None)
    assert p.status == "unparsed"
    assert P("Innova Star Destroyer", "Innova", None, None).status == "matched"


@pytest.mark.parametrize("title,vendor,mold", [
    ("Ｉｎｎｏｖａ Ｓｔａｒ Ｄｅｓｔｒｏｙｅｒ", "", "Destroyer"),        # fullwidth
    ("Kastaplast K1 Stål Berg", "Kastaplast", "Berg"),
    ("  ***  Innova   Star\tDestroyer!!! 175g  ***  ", "Innova", "Destroyer"),
    ("Innova Star Destroyer ​175g", "Innova", "Destroyer"),           # zero-width space
    ("Innova Star Destroyer", "Innova", "Destroyer"),            # non-breaking spaces
    ("\U0001F525 Innova Star Destroyer \U0001F525", "Innova", "Destroyer"),
    ("Latitude 64° Opto River", "Latitude 64°", "River"),
    ("Innova Star Destroyer (175g) [Blue] {New}", "Innova", "Destroyer"),
    ("Innova—Star—Destroyer", "Innova", "Destroyer"),
    ("Innova / Star / Destroyer / 175g", "Innova", "Destroyer"),
    ("innova star destroyer", "INNOVA", "Destroyer"),
    ("INNOVA STAR DESTROYER", "innova", "Destroyer"),
    ("Innova Star Destroyer – Disc Golf Distance Driver", "Innova", "Destroyer"),
    ("Dynamic Discs “Lucid” Judge", "Dynamic Discs", "Judge"),
    ("日本語 Innova Star Destroyer", "Innova", "Destroyer"),
])
def test_odd_punctuation_and_unicode(title, vendor, mold):
    p = P(title, vendor)
    assert (p.status, p.mold) == ("matched", mold), p


def test_very_long_and_repetitive_titles_are_handled():
    p = P("Innova Star Destroyer " + "Wraith " * 500, "Innova")
    assert p.status in STATUSES
    p = P("x" * 10000, "Innova")
    assert p.status == "unparsed"
    p = P(" ".join(["Innova"] * 300), "Innova")
    assert p.status == "unparsed"


def test_overlong_whitespace_and_junk_titles_do_not_crash():
    assert P(" " * 5000, "Innova").status == "unparsed"
    assert P("\n\t" * 3000).status == "unparsed"
    assert P("a " * 5000, "Innova").status in STATUSES
    long_but_valid = "Innova Star Destroyer " + "word " * 1000
    assert P(long_but_valid, "Innova").status in STATUSES


@pytest.mark.parametrize("title", ["Lucid Destroyer", "Neutron Destroyer", "Classic Roc3", "Z Destroyer"])
def test_another_brands_plastic_blocks_brand_inference(title):
    p = P(title)
    assert p.status == "review", p
    assert P(title, "Innova").status == "matched"      # a vendor makes the brand explicit again


def test_a_plastic_of_the_inferred_brand_still_allows_inference():
    assert P("Neutron Volt").status == "matched"        # shared by the MVP family, and Volt is MVP's
    assert P("Z Luna").status == "matched"


def test_plastic_plus_one_unknown_word_without_a_brand_is_a_review_guess():
    p = P("Star Frobnicator")
    assert (p.status, p.manufacturer, p.mold) == ("review", "", "Frobnicator")
    assert P("Frobnicator").status == "unparsed"          # nothing says it is a disc
    assert P("Blue Disc Thing").status == "unparsed"


# ---------------------------------------------------------------- invariants

def _corpus(n: int, seed: int = 7) -> list[tuple[str, str, str, tuple]]:
    rng = random.Random(seed)
    molds = [(m.mfr, m.name) for ms in parser._MOLD_IDX.values() for m in ms]
    plastics = ["Star", "Champion", "DX", "ESP", "Z", "Lucid", "Opto", "Neutron", "400", "Pro", "Tournament", "VIP", "K1"]
    junk = ["Disc Golf Bag", "Basket", "T-Shirt Blue", "Towel", "Gift Card", "Frobnicator", "Mystery", "OOP", "9/10"]
    out = []
    for i in range(n):
        mfr, mold = rng.choice(molds)
        r = rng.random()
        if r < 0.55:
            t = f"{mfr} {rng.choice(plastics)} {mold} {rng.randint(150, 175)}g"
        elif r < 0.65:
            t = f"{rng.choice(plastics)} {mold.upper()} - {rng.randint(150, 175)}g Ricky Wysocki Tour Series 2019 OOP 9/10"
        elif r < 0.75:
            t = f"{mfr} {rng.choice(junk)} {i}"
        elif r < 0.85:
            t = f"{mfr} {rng.choice(plastics)} Unknownmold{i % 97} {rng.randint(150, 175)}g"
        elif r < 0.93:
            t = f"{mfr} {rng.choice(plastics)} {mold[:-1]}x {rng.randint(150, 175)}g"
        else:
            t = rng.choice(["", "   ", "175g", "Disc", "!!!", mfr, f"{mfr} {rng.choice(junk)}"])
        out.append((t, mfr if rng.random() < 0.7 else "", "", ()))
    return out


def test_invariants_over_a_generated_corpus():
    for title, vendor, ptype, tags in _corpus(4000):
        p = parse_listing(title, vendor, ptype, tags)
        assert p.status in STATUSES, (title, p)
        assert 0.0 <= p.confidence <= 1.0, (title, p)
        assert p.condition in {"new", "used"}, (title, p)
        assert set(p.flags) <= FLAGS and p.flags == sorted(set(p.flags)), (title, p)
        assert p.disc_type in TYPES
        assert p.year is None or 1990 <= p.year <= 2035
        assert p.grade is None or 1 <= p.grade <= 10
        if p.status == "matched":
            assert p.manufacturer and p.mold, (title, p)
            assert p.confidence >= 0.7, (title, p)
            key = disc_key(p)
            assert key is not None and len(key.split("|")) == 5, (title, p)
        else:
            assert disc_key(p) is None
        if p.status == "ignored":
            assert p.manufacturer == p.mold == p.plastic == ""
        if p.status == "unparsed":
            assert p.mold == ""


def test_parse_is_deterministic_and_has_no_hidden_state():
    corpus = _corpus(500, seed=3)
    first = [parse_listing(*c) for c in corpus]
    second = [parse_listing(*c) for c in reversed(corpus)][::-1]
    assert first == second
    assert [parse_listing(*c) for c in corpus] == first


def test_returned_objects_are_independent():
    a = P("Innova Champion Shryke OOP", "Innova")
    a.flags.append("junk")
    assert P("Innova Champion Shryke OOP", "Innova").flags == ["oop"]


def test_parse_is_pure_no_network_and_no_file_access(monkeypatch):
    import builtins
    import socket

    def boom(*a, **k):
        raise AssertionError("parse_listing must not do I/O")

    with monkeypatch.context() as m:
        m.setattr(socket, "socket", boom)
        m.setattr(socket, "create_connection", boom)
        m.setattr(builtins, "open", boom)
        m.setattr(Path, "open", boom)
        m.setattr(Path, "read_text", boom)
        assert P("Innova Star Destroyer 175g", "Innova").status == "matched"
        assert P("Innova Star Destroyr", "Innova").status == "matched"      # the fuzzy path too
        assert P("Disc Golf Bag", "Innova").status == "ignored"
        assert parse_weight("173g") == 173


def test_confidence_orders_exact_above_inferred_above_review():
    exact = P("Innova Star Destroyer 175g", "Innova").confidence
    inferred = P("Star Destroyer 175g").confidence
    fuzzy = P("Innova Star Destroyr 175g", "Innova").confidence
    review = P("Innova Star Roc 5", "Innova").confidence
    assert exact > inferred and exact > fuzzy > review
    assert P("Innova Star 175g", "Innova").confidence < review


# ---------------------------------------------------------------- regressions from the adversarial review

@pytest.mark.parametrize("title", [
    "Innova Star Roc+", "Innova Star Roc3+", "Innova Star Aviar+", "Innova DX Aviar3+",
])
def test_a_plus_glued_to_a_mold_names_a_different_mold(title):
    # Roc+ is its own disc; the tokenizer used to drop the "+" and call it a Roc
    p = P(title, "Innova")
    assert p.status == "review" and disc_key(p) is None


def test_a_plus_elsewhere_is_not_a_variant():
    assert P("Innova Star Destroyer + Free Shipping", "Innova").status == "matched"
    assert P("Innova Star Destroyer 175g+", "Innova").status == "matched"


@pytest.mark.parametrize("title,vendor", [
    ("Innova Star Eagle X", "Innova"), ("Kastaplast K1 Berg X", "Kastaplast"), ("Kastaplast K1 Kaxe Z", "Kastaplast"),
    ("Kastaplast K1 Grym X", "Kastaplast"), ("Kastaplast K1 Reko X", "Kastaplast"),
])
def test_a_lone_x_or_z_after_a_mold_may_be_a_sibling_mold(title, vendor):
    assert P(title, vendor).status == "review"


@pytest.mark.parametrize("title", [
    "Innova Star Destroyer w/ Dye Options", "Innova Star Destroyer w Free Shipping",
    "Innova Star Destroyer - Pink", "Innova Star Destroyer & Free Shipping", "Innova Star Destroyer (L)",
])
def test_other_lone_letters_after_a_mold_are_filler(title):
    assert P(title, "Innova").status == "matched"


def test_a_plastic_after_the_mold_is_not_a_sibling_suffix():
    assert ident(P("Discraft Buzzz Z", "Discraft")) == ("matched", "Discraft", "Buzzz", "Z")
    assert ident(P("Discraft Buzzz Z Lite", "Discraft")) == ("matched", "Discraft", "Buzzz", "Z Lite")


@pytest.mark.parametrize("title,vendor,plastic", [
    ("Innova Star Destroyer Premium Plastic", "Innova", "Star"),
    ("Innova Premium Star Destroyer", "Innova", "Star"),
    ("Innova DX Destroyer Recycled", "Innova", "DX"),
    ("Discraft ESP Buzzz Premium", "Discraft", "ESP"),
    ("Dynamic Discs Premium Lucid Judge", "Dynamic Discs", "Lucid"),
    ("Innova Wraith Premium Disc", "Innova", "Premium"),            # nothing of the maker's own: the generic word stays
])
def test_a_makers_own_plastic_beats_a_generic_one(title, vendor, plastic):
    p = P(title, vendor)
    assert p.status == "matched" and p.plastic == plastic, p


@pytest.mark.parametrize("title", [
    "Prodigy PA-1 200 - 175g", "Prodigy PA-1 200 / 175g", "PA-1 200 - 175g", "Prodigy 200 PA-1 - 175g",
])
def test_prodigy_200_plastic_is_not_eaten_by_a_following_weight(title):
    # "200 - 175g" looked like a weight range and masked the 200
    p = P(title)
    assert ident(p) == ("matched", "Prodigy", "PA-1", "200"), p


def test_weight_ranges_are_still_masked_in_titles():
    assert ident(P("Innova Star Destroyer 170-175g", "Innova")) == ("matched", "Innova", "Destroyer", "Star")
    assert ident(P("Prodigy 400 PA-3 160 - 200g")) == ("matched", "Prodigy", "PA-3", "400")


@pytest.mark.parametrize("tags", [5, 3.5, True, {"a": 1}, b"Used, Bags", frozenset({"Used", "Bags"}),
                                  (t for t in ["Used"]), [1, None, ["x"], 2.5], object()])
def test_tags_of_any_shape_never_raise(tags):
    assert P("Innova Star Destroyer", "Innova", "", tags).status in STATUSES


def test_tags_given_as_bytes_or_a_set_are_still_read():
    assert P("Innova Star Destroyer", "Innova", "", b"Innova, Used").condition == "used"
    assert P("Innova Star Destroyer", "Innova", "", {"Used", "Innova"}).condition == "used"


def test_a_set_of_tags_is_read_in_a_fixed_order():
    # only the first 50 tags are read; a set has no order, so they are taken in sorted order
    # ("Used" sorts before "t0"), not wherever the hash seed happens to put them
    tags = {f"t{i}" for i in range(80)} | {"Used"}
    assert P("Innova Star Destroyer", "Innova", "", tags).condition == "used"
    assert parser._tag_list(tags) == sorted(tags)[:50]


@pytest.mark.parametrize("tags", [
    ("used_disc",), ("condition_used",), ("Used_Discs",), ("used_disc", "NEW"), ("pre_owned",), ("beat_in",), ("grade_8",),
])
def test_underscore_tags_are_read_as_words(tags):
    # \b treats "_" as a letter, so "used_disc" used to look like no word at all
    assert P("Innova Star Destroyer", "Innova", "", tags).condition == "used"


def test_underscore_tag_for_new_stays_new():
    assert P("Innova Star Destroyer", "Innova", "", ("unused_disc", "never_used")).condition == "new"
    assert P("Innova Star Destroyer", "Innova", "", ("disc_used_up_pack_of_none",)).condition == "used"


@pytest.mark.parametrize("title,grade", [
    ("Innova Star Destroyer Sleepy Scale 4", 4.0), ("Innova Star Destroyer Sleepy Scale: 6", 6.0),
    ("Innova Star Destroyer Sleepy scale 7.5", 7.5), ("Innova Star Destroyer Sleepy Scale 4/10", 4.0),
    ("Innova Star Destroyer Sleepy 7", 7.0),
])
def test_sleepy_scale_grade(title, grade):
    p = P(title, "Innova")
    assert (p.condition, p.grade) == ("used", grade)


@pytest.mark.parametrize("title,vendor", [
    ("Innova Logo Cap", "Innova"), ("Discraft Buzzz Logo Cap", "Discraft"), ("Dynamic Discs Judge Cap - Black", "Dynamic Discs"),
    ("Innova Champion Roc3 Sweater", "Innova"), ("Discraft Z Buzzz Sweatpants", "Discraft"),
    ("Innova Roc3 Long Sleeve Tee", "Innova"), ("Discraft Buzzz Ladies Tank Top", "Discraft"),
    ("Discraft Buzzz Gloves", "Discraft"), ("Dynamic Discs Fuzion Truth Disc Golf Glove", "Dynamic Discs"),
    ("Innova Wraith Pin", "Innova"), ("Innova Star Destroyer Coin", "Innova"),
    ("Innova Star Destroyer Phone Case", "Innova"), ("Innova Star Destroyer Rain Cover", "Innova"),
    ("Innova Destroyer Wristband", "Innova"), ("Innova Destroyer Neck Gaiter", "Innova"),
    ("Innova Destroyer Belt", "Innova"), ("Innova Destroyer Pen", "Innova"), ("Innova Destroyer Pencil Set", "Innova"),
    ("Dynamic Discs Fuzion Truth Disc Golf Sunglasses", "Dynamic Discs"), ("Innova Disc Golf Shoes", "Innova"),
    ("Discraft Buzzz Disc Golf Book", "Discraft"), ("Innova Star Destroyer Disc Holder", "Innova"),
    ("Innova Star Destroyer Tee Pad", "Innova"), ("Discraft Buzzz Chalk", "Discraft"),
])
def test_more_non_disc_products_are_ignored(title, vendor):
    p = P(title, vendor)
    assert p.status == "ignored", p
    assert p.mold == "" and disc_key(p) is None


@pytest.mark.parametrize("title", [
    "Innova Star Destroyer Net Wt 175g", "Innova Star Destroyer (Net Weight 175g)",
    "Discraft Z Buzzz American Flag Stamp", "Innova Star Destroyer Flag Dye",
])
def test_net_weight_and_flag_stamps_are_discs(title):
    assert P(title, "Innova" if "Innova" in title else "Discraft").status == "matched"


@pytest.mark.parametrize("title", ["Disc Golf Practice Net", "Putting Net", "Innova Star Destroyer Net Wt Net"])
def test_a_practice_net_is_still_ignored(title):
    assert P(title, "Innova").status == "ignored"


@pytest.mark.parametrize("title,vendor,mold,player", [
    ("Innova DX Aviar P&amp;A", "Innova", "Aviar P&A", ""),
    ("Innova DX Aviar P&A", "Innova", "Aviar P&A", ""),
    ("Innova&nbsp;Star&nbsp;Destroyer &#8211; 175g", "Innova", "Destroyer", ""),
    ("Innova Star Destroyer Tour Series Ricky O&#39;Brien", "Innova", "Destroyer", "Ricky O'Brien"),
    ("Dynamic Discs &quot;Lucid&quot; Judge", "Dynamic Discs", "Judge", ""),
])
def test_html_entities_left_in_titles_are_decoded(title, vendor, mold, player):
    p = P(title, vendor)
    assert (p.status, p.mold, p.player) == ("matched", mold, player), p


def test_html_entities_in_vendor_and_tags_are_decoded_too():
    assert P("Star Destroyer", "Innova&nbsp;Discs").manufacturer == "Innova"
    assert P("Innova Star Destroyer", "Innova", "", ("Used&nbsp;Discs", "x")).condition == "used"
    assert P("Innova Star Destroyer", "Innova", "Used&nbsp;Discs").condition == "used"


def test_no_ignore_word_is_part_of_a_real_mold_plastic_edition_or_brand():
    """Self-consistency: an ignore word that is also a catalogue word would delete real discs."""
    vocabulary = parser._MOLD_WORDS | parser._PLASTIC_WORDS | parser._EDITION_WORDS | parser._MFR_WORDS
    assert not (parser._IGNORE_TITLE_WORDS & vocabulary)
    for phrase in parser._IGNORE_PHRASES:
        assert phrase not in parser._MOLD_IDX and phrase not in parser._PLASTIC_ALL


@pytest.mark.parametrize("title,vendor,player", [
    ("Innova Star Destroyer Tour Series Ricky Jones-Smith", "Innova", "Ricky Jones-Smith"),
    ("Innova Star Destroyer Anna-Maria Jones Tour Series", "Innova", "Anna-Maria Jones"),
    ("Innova Star Destroyer Tour Series Ricky O'Brien", "Innova", "Ricky O'Brien"),
    ("Innova Star Destroyer Ryan O'Neill Tour Series 2019", "Innova", "Ryan O'Neill"),
    ("INNOVA STAR DESTROYER TOUR SERIES RICKY O'BRIEN", "Innova", "Ricky O'Brien"),
    ("Innova Star Destroyer Tour Series Ricky De La Hoya", "Innova", "Ricky De La Hoya"),
    ("Innova Star Destroyer Tour Series Vanessa Van Dyken", "Innova", "Vanessa Van Dyken"),
    ("Innova Star Destroyer Vanessa Van Dyken Tour Series", "Innova", "Vanessa Van Dyken"),
    ("Innova Star Destroyer Tour Series Ricky Wysocki", "Innova", "Ricky Wysocki"),
])
def test_player_names_with_hyphens_apostrophes_and_particles(title, vendor, player):
    p = P(title, vendor)
    assert (p.status, p.mold, p.player) == ("matched", "Destroyer", player), p


@pytest.mark.parametrize("title,vendor,mold,plastic", [
    ("Innova Star Destroyer Wysocki Tour Series", "Innova", "Destroyer", "Star"),
    ("Innova Star Wraith Wysocki Tour Series", "Innova", "Wraith", "Star"),
    ("Discraft ESP Buzzz McBeth Signature", "Discraft", "Buzzz", "ESP"),
    ("Discraft Z Zone Barela Signature Series", "Discraft", "Zone", "Z"),
    ("Discraft Zone Barela Signature Series", "Discraft", "Zone", ""),
    ("Discraft Signature Series Zone Barela", "Discraft", "Zone", ""),
    ("Discraft ESP Buzzz Lizotte Signature", "Discraft", "Buzzz", "ESP"),
])
def test_the_only_mold_is_never_swallowed_as_part_of_a_player_name(title, vendor, mold, plastic):
    # "Destroyer Wysocki" used to be taken for a person, leaving no mold: matched -> unparsed
    p = P(title, vendor)
    assert (p.status, p.mold, p.plastic, p.player) == ("matched", mold, plastic, ""), p


@pytest.mark.parametrize("tail", [
    "Free Shipping", "Sold Out", "In Stock", "Great Condition", "Great Deal", "Best Offer", "Fast Shipping", "Back Order",
    "Excellent Condition", "Hot Item", "Cyber Monday", "Holiday Sale", "Open Box", "Seller Choice", "Ships Free", "Low Stock",
    "Clearance Sale", "Flight Numbers", "Coming Soon", "Just Released", "Hard To Find", "Out Of Stock", "Like New",
])
@pytest.mark.parametrize("layout", [
    "Innova Star Destroyer Tour Series {}", "Innova Star Destroyer {} Tour Series", "Tour Series {} Innova Star Destroyer",
])
def test_marketing_phrases_beside_a_marker_are_not_a_player(layout, tail):
    # "Tour Series Free Shipping" used to mint a disc whose player is "Free Shipping"
    p = P(layout.format(tail), "Innova")
    assert (p.status, p.mold, p.player) == ("matched", "Destroyer", ""), p


def test_filler_words_never_shadow_a_mold_word():
    assert parser._NOISE & parser._MOLD_WORDS <= {"a", "driver", "max"}


def test_a_mold_word_is_still_a_name_when_another_mold_is_in_the_title():
    assert P("Eagle McMahon Signature Series Z Luna", "Discraft").player == "Eagle McMahon"
    assert P("Innova Star Eagle Team Series Eagle McMahon", "Innova").player == "Eagle McMahon"
    assert ident(P("Innova Star Eagle Team Series Eagle McMahon", "Innova")) == ("matched", "Innova", "Eagle", "Star")
    assert P("Hailey King Team Series VIP Harp", "Westside Discs").player == "Hailey King"


@pytest.mark.parametrize("text,expected", [
    ("95-110g", None), ("98-110g", None), ("90 - 105 g", None), ("150g-160", None), ("170g-175", None),
    ("170‑175g", None), ("170‒175g", None), ("170―175g", None), ("170−175g", None),
    ("170~175g", None), ("170〜175g", None), ("170‐175g", None),
    ("175g / 10/10", 175), ("175g 10/10", 175), ("10/10 175g", 175), ("175g 5-10", 175),
    ("Weight: 168g", 168), ("168g (pink)", 168),
])
def test_parse_weight_range_edge_cases(text, expected):
    assert parse_weight(text) == expected


def test_parse_weight_survives_a_long_run_of_blanks():
    for text in ("170" + " " * 200_000 + "x", "170 -" + " " * 200_000, "1 " * 100_000, "170" + "\t" * 100_000 + "175g"):
        start = time.perf_counter()
        parse_weight(text)
        assert time.perf_counter() - start < 0.5


def test_parse_listing_survives_a_long_run_of_blanks():
    for title in ("170" + " " * 1990, "grade" + " " * 1990 + "9", "9/10" + " " * 1990 + "x"):
        start = time.perf_counter()
        P(title, "Innova")
        assert time.perf_counter() - start < 0.5


@pytest.mark.parametrize("title,vendor", [
    ("Discraft ESP Zones", "Discraft"), ("Discraft ESP Challenges", "Discraft"), ("Discraft Z Nukes", "Discraft"),
    ("Discraft ESP Surges", "Discraft"), ("Innova Star Aviars", "Innova"),
])
def test_a_plural_or_other_ending_is_not_a_typo(title, vendor):
    # "zones" is one edit away from the key "zoneos"; it is not a Zone OS. (A review row may still
    # keep the base mold as its guess, like "Wraiths" -> Wraith.)
    p = P(title, vendor)
    assert p.status != "matched", p
    assert p.mold not in {"Zone OS", "Nuke OS", "Nuke SS", "Surge SS", "Challenger OS", "Challenger SS"}


def test_typos_inside_a_word_are_still_recovered():
    assert ident(P("Innova Star Destroyr", "Innova")) == ("matched", "Innova", "Destroyer", "Star")
    assert ident(P("Innova Star Destoyer", "Innova")) == ("matched", "Innova", "Destroyer", "Star")
    assert P("Discraft Z Challngr", "Discraft").status != "matched"


def test_suffix_molds_are_not_fuzzy_candidates_but_their_base_still_is():
    keys = {k for buckets in parser._FUZZY_BUCKETS.values() for ks in buckets.values() for k in ks}
    assert "zoneos" not in keys and "nukess" not in keys and "d1max" not in keys
    assert "challenger" in keys and "destroyer" in keys


@pytest.mark.parametrize("title,vendor,plastic", [
    ("Eclipse Glow Wave", "MVP Disc Sports", "Eclipse"), ("MVP Total Eclipse Glow Wave", "MVP Disc Sports", "Total Eclipse"),
    ("Dynamic Discs Moonshine Glow Raider", "Dynamic Discs", "Moonshine"),
    ("Axiom Eclipse Glow Envy", "Axiom Discs", "Eclipse"),
])
def test_glow_is_not_also_an_edition_on_a_glow_line(title, vendor, plastic):
    p = P(title, vendor)
    assert (p.plastic, p.edition) == (plastic, ""), p
    assert disc_key(p) == disc_key(P(title.replace(" Glow", ""), vendor))


def test_glow_stays_an_edition_on_an_ordinary_plastic():
    assert P("Innova Star Glow Dart", "Innova").edition == "glow"
    assert P("Innova Nexus Glow Dart", "Innova").edition == "glow"
    assert P("Discraft ESP Glo Luna", "Discraft").edition == "glow"


def test_every_glow_flag_in_plastics_json_names_a_real_plastic():
    data = json.loads((DATA / "plastics.json").read_text("utf-8"))["manufacturers"]
    flagged = {(b, e["name"]) for b, es in data.items() for e in es if e.get("glow")}
    assert flagged and {n for _b, n in flagged} >= {"Eclipse", "Total Eclipse", "Moonshine", "Proto Glow"}
    for _b, e in ((b, e) for b, es in data.items() for e in es):
        assert set(e) <= {"name", "aliases", "glow"}, e


def test_emac_truth_is_its_own_mold():
    assert ident(P("Dynamic Discs Lucid EMAC Truth", "Dynamic Discs")) == ("matched", "Dynamic Discs", "EMAC Truth", "Lucid")
    assert ident(P("Dynamic Discs Lucid Emac Truth 175g", "Dynamic Discs"))[2] == "EMAC Truth"
    assert ident(P("Dynamic Discs Lucid Truth", "Dynamic Discs")) == ("matched", "Dynamic Discs", "Truth", "Lucid")


@pytest.mark.parametrize("ch", ["‐", "‑", "‒", "–", "—", "―", "−", "﹘", "－"])
def test_every_dash_look_alike_is_a_hyphen(ch):
    assert parser._fold(f"a{ch}b") == "a-b"
    assert P(f"Innova Star Destroyer 170{ch}175g", "Innova").mold == "Destroyer"


def test_the_version_was_bumped_for_the_review_fixes():
    assert PARSER_VERSION >= 2


# ---------------------------------------------------------------- eBay hardening (DESIGN.md section 10.5)
# The curated eBay title set, its precision measurement and the generated-title properties live in
# tests/test_parser_ebay.py; these are the rule-level checks that belong next to the retail ones.

def test_the_version_was_bumped_for_ebay():
    assert PARSER_VERSION >= 3


@pytest.mark.parametrize("title", [
    "Innova Star Destroyer Lot of 3", "Innova Star Destroyer x2", "Innova Star Destroyer 2x", "(2) Innova Star Destroyer",
    "Innova Star Destroyer Pair", "Innova Star Destroyer Set of 2", "Innova Star Destroyer 2 Discs",
    "Innova Star Destroyer Mystery", "Pick Your Disc Innova Star Destroyer", "Assorted Innova Star Destroyer Discs",
    "Innova Star Destroyer Bundle", "3 Innova Star Destroyer", "Innova Star Destroyer Qty 4",
])
def test_lots_pairs_and_bundles_are_never_a_single_disc(title):
    p = P(title, "Innova")
    assert p.status == "ignored" and disc_key(p) is None, p


@pytest.mark.parametrize("title", [
    "Innova Star Destroyer Choose Your Weight", "Innova Star Destroyer Random Color", "Innova Star Destroyer Assorted Colors",
    "Innova Star Aviar X3", "Innova Star Roc 3", "Innova Star Teebird 3 Disc Golf", "Kastaplast K1 Lots",
    "Innova Star Destroyer Max Distance Driver", "Innova Star Destroyer Qty 1", "Innova Star Destroyer (1)",
])
def test_things_that_look_like_a_lot_but_are_not_stay_discs(title):
    assert P(title, "Innova" if "Kastaplast" not in title else "Kastaplast").status != "ignored", title


@pytest.mark.parametrize("title,vendor", [
    ("Disc Golf Dog Toy Frisbee", ""), ("Disc Golf Trading Card Paul McBeth", ""), ("Disc Golf Basket Chains", ""),
    ("Innova Discatcher Pro", "Innova"), ("Disc Golf Rule Book", ""), ("Innova Ultra-Star 175g", "Innova"),
])
def test_more_marketplace_non_discs_are_ignored(title, vendor):
    assert P(title, vendor).status == "ignored", title


@pytest.mark.parametrize("title,tags,ptype,condition", [
    ("Innova Star Destroyer", ("condition:used",), "", "used"),
    ("Innova Star Destroyer", ("condition:new",), "", "new"),
    ("Pre-Owned Innova Star Destroyer", ("condition:new",), "New", "used"),
    ("Innova Star Destroyer Unthrown", ("condition:used",), "", "used"),
    ("Innova Star Destroyer NIB", (), "", "new"),
    ("Innova Star Destroyer Like New", (), "", "used"),
    ("Innova Star Destroyer Like New", ("condition:new",), "", "new"),
])
def test_ebay_condition_tags_and_titles(title, tags, ptype, condition):
    assert P(title, "", ptype, tags).condition == condition


def test_z_buzzz_and_buzzz_z_are_the_same_disc():
    a, b = P("Discraft Z Buzzz 177g"), P("Discraft Buzzz Z 177g")
    assert ident(a) == ident(b) == ("matched", "Discraft", "Buzzz", "Z")
    assert ident(P("Discraft Buzz Z Line")) == ("matched", "Discraft", "Buzzz", "Z")
    assert ident(P("Discraft Buzzz")) == ("matched", "Discraft", "Buzzz", "")          # a lone spelling is untouched


def test_flight_numbers_do_not_make_a_match_risky():
    assert ident(P("Discraft Buzzz 5/4/-1/1", "Discraft")) == ("matched", "Discraft", "Buzzz", "")
    assert ident(P("Innova Aviar 2/3/0/1 DX", "Innova")) == ("matched", "Innova", "Aviar", "DX")


def test_plastics_added_for_ebay_resolve():
    assert P("Discraft ESP FLX Buzzz").plastic == "ESP FLX"
    assert P("Discraft Z FLX Zone").plastic == "Z FLX"
    assert P("Discraft ESP Buzzz").plastic == "ESP"        # the shorter line is untouched
    assert P("Innova Pro KC Aviar").plastic == "KC Pro"


def test_two_lines_of_one_maker_in_a_title_are_review_not_a_guess():
    assert P("Latitude 64 Opto Ballista Gold Stamp").status == "review"
    assert P("Prodigy 500 Spectrum D2 Max", "Prodigy Disc").status == "matched"   # a numbered line + Spectrum is one plastic


def test_a_second_brands_mold_without_its_brand_is_review():
    assert P("Discraft ESP Buzzz and Wraith").status == "review"
    assert P("Discraft ESP Buzzz and Innova Wraith").status == "review"
    assert P("Discraft ESP Buzzz Pure Plastic").status == "matched"      # "Pure" is too short to count as a second disc


# ---------------------------------------------------------------- eBay review round (parser version 4)
# The eBay-specific cases live in tests/test_parser_ebay.py; these pin what the same rules do to a retail
# title that carries a vendor.

@pytest.mark.parametrize("title,vendor", [
    ("Z SS Buzzz 175g", "Discraft"), ("Prodigy 400 Max D2", "Prodigy Disc"), ("Opto Pro Ballista 172g", "Latitude 64"),
    ("Classic Aviar DX", "Innova"), ("Buzzz Z 175g SS", "Discraft"), ("Aviar DX 170g Classic", "Innova"),
])
def test_a_sibling_mold_written_apart_is_review_for_a_retail_title_too(title, vendor):
    assert P(title, vendor).status == "review", title


@pytest.mark.parametrize("title,vendor,plastic", [
    ("Lucid-X Glimmer Maverick", "Dynamic Discs", "Lucid-X"),            # Glimmer is not treated as a qualifier
    ("Z Buzzz 175g", "Discraft", "Z"), ("Z Zone OS", "Discraft", "Z"),
    ("Discraft Z Buzzz American Flag Stamp", "", "Z"),
])
def test_retail_titles_with_an_ordinary_extra_word_stay_matched(title, vendor, plastic):
    p = P(title, vendor)
    assert p.status == "matched" and p.plastic == plastic, p


def test_the_new_lot_vocabulary_applies_to_retail_titles_too():
    for title in ("Innova Star Destroyer Starter Kit", "Innova DX Roadrunner x3", "Two Disc Innova DX Aviar Set"):
        assert P(title, "Innova").status == "ignored", title


# ---------------------------------------------------------------- performance

def test_fifty_thousand_titles_parse_in_seconds():
    corpus = _corpus(50_000, seed=11)
    start = time.perf_counter()
    counts: dict[str, int] = {}
    for c in corpus:
        s = parse_listing(*c).status
        counts[s] = counts.get(s, 0) + 1
    elapsed = time.perf_counter() - start
    assert sum(counts.values()) == 50_000
    assert counts.get("matched", 0) > 20_000          # sanity: the corpus really exercised matching
    assert elapsed < 20, f"{elapsed:.1f}s for 50k titles"   # ~3s locally; the margin is for slow CI
