#!/usr/bin/env python3
"""Rarity rules and TCGPlayer subset maps for tracked Pokémon singles."""

from __future__ import annotations

import re

# Extra TCGPlayer groups that belong to a catalogued set (TG, RC, shiny vault, GG).
EXTRA_GROUPS = {
    "Generations": [{"groupId": 1729, "subset": "radiant-collection"}],
    "Hidden Fates": [{"groupId": 2594, "subset": "shiny-vault"}],
    "Shining Fates": [{"groupId": 2781, "subset": "shiny-vault"}],
    "Brilliant Stars": [{"groupId": 3020, "subset": "trainer-gallery"}],
    "Astral Radiance": [{"groupId": 3068, "subset": "trainer-gallery"}],
    "Lost Origin": [{"groupId": 3172, "subset": "trainer-gallery"}],
    "Silver Tempest": [{"groupId": 17674, "subset": "trainer-gallery"}],
    "Crown Zenith": [{"groupId": 17689, "subset": "galarian-gallery"}],
}

XY_FAMILIES = {"xy"}
SM_FAMILIES = {"sun-and-moon"}
SWSH_FAMILIES = {"sword-and-shield"}
SV_FAMILIES = {"scarlet-and-violet", "mega-evolution"}

EX_RE = re.compile(r"\bEX\b")
GX_RE = re.compile(r"\bGX\b", re.I)
V_RE = re.compile(r"(?:\bVMAX\b|\bVSTAR\b|\bV-UNION\b|(?<![A-Za-z])V(?=[\s\(\[]|$))", re.I)
SV_EX_RE = re.compile(r"\bex\b")
BREAK_RE = re.compile(r"\bBREAK\b")
PRISM_RE = re.compile(r"prism star", re.I)
SHINING_RE = re.compile(r"^Shining\b|\bShining\b", re.I)
RADIANT_RE = re.compile(r"^Radiant\b", re.I)
TAG_TEAM_RE = re.compile(r"tag team", re.I)

SKIP_NAME = re.compile(
    r"code card|\bcase\b|booster box|booster pack|booster bundle|elite trainer|"
    r"theme deck|\btin\b|pin collection|poster collection|knock out collection|"
    r"build & battle|binder collection|premium collection|ultra-premium|"
    r"figure collection|tech sticker|mini tin|battle deck|display|"
    r"jumbo|oversize|half booster|set of 2",
    re.I,
)
SKIP_PATTERN = re.compile(r"poke ball pattern|master ball pattern", re.I)

SV_RARITIES = {
    "double rare",
    "ultra rare",
    "illustration rare",
    "special illustration rare",
    "hyper rare",
    "mega hyper rare",
    "ace spec rare",
    "futuristic rare",
    "black white rare",
    "shiny rare",
    "shiny ultra rare",
}

SWSH_RARITIES = {
    "ultra rare",
    "secret rare",
    "rainbow rare",
    "amazing rare",
    "radiant rare",
}

SM_RARITIES = {
    "prism rare",
    "rainbow rare",
    "secret rare",
}

SUBSET_LABEL = {
    "radiant-collection": "Radiant Collection",
    "trainer-gallery": "Trainer Gallery",
    "galarian-gallery": "Galarian Gallery",
    "shiny-vault": "Shiny Vault",
}


def extended_value(row: dict, name: str) -> str:
    target = (name or "").lower()
    for item in row.get("extendedData") or []:
        if str(item.get("name") or "").lower() == target:
            return str(item.get("value") or "").strip()
    return ""


def rarity_of(row: dict) -> str:
    return extended_value(row, "Rarity")


def number_of(row: dict) -> str:
    return extended_value(row, "Number")


def has_ex(name: str) -> bool:
    return bool(EX_RE.search(name or ""))


def has_gx(name: str) -> bool:
    return bool(GX_RE.search(name or ""))


def has_v(name: str) -> bool:
    return bool(V_RE.search(name or ""))


def has_sv_ex(name: str) -> bool:
    return bool(SV_EX_RE.search(name or ""))


def is_skipped_product(name: str, rarity: str, number: str) -> bool:
    text = name or ""
    if SKIP_NAME.search(text) or SKIP_PATTERN.search(text):
        return True
    if (rarity or "").lower() == "code card":
        return True
    if not number and not rarity:
        return True
    return False


def include_card(family_id: str, name: str, rarity: str, number: str, subset: str | None = None) -> bool:
    """True when this TCGPlayer product matches the requested singles bands."""
    if is_skipped_product(name, rarity, number):
        return False

    family = family_id or ""
    rarity_key = (rarity or "").strip().lower()
    subset = subset or ""

    if subset == "radiant-collection":
        return bool(number)
    if subset in {"trainer-gallery", "galarian-gallery"}:
        return bool(number)
    if subset == "shiny-vault":
        if family in SM_FAMILIES:
            return has_gx(name)
        if family in SWSH_FAMILIES:
            return has_v(name)
        return False

    if family in XY_FAMILIES:
        if rarity_key == "rare break" or BREAK_RE.search(name or ""):
            return True
        if rarity_key in {"ultra rare", "secret rare"} and has_ex(name):
            return True
        return False

    if family in SM_FAMILIES:
        if rarity_key in SM_RARITIES or rarity_key == "prism rare":
            return True
        if PRISM_RE.search(name or ""):
            return True
        if rarity_key == "ultra rare" and (has_gx(name) or TAG_TEAM_RE.search(name or "")):
            return True
        if has_gx(name) and rarity_key in {"ultra rare", "secret rare", "rainbow rare", "shiny holo rare"}:
            return True
        if rarity_key == "shiny holo rare" and (has_gx(name) or SHINING_RE.search(name or "")):
            return True
        if has_gx(name) and rarity_key in {"ultra rare", "secret rare", "rainbow rare", "shiny holo rare"}:
            return True
        return False

    if family in SWSH_FAMILIES:
        if rarity_key in SWSH_RARITIES:
            return True
        if RADIANT_RE.search(name or "") or has_v(name):
            return True
        return False

    if family in SV_FAMILIES:
        if rarity_key in SV_RARITIES:
            return True
        if "ace spec" in rarity_key or "ace spec" in (name or "").lower():
            return True
        if has_sv_ex(name) and rarity_key in SV_RARITIES | {"double rare", "ultra rare"}:
            return True
        return False

    return False


def product_url(row: dict) -> str:
    product_id = str(row.get("productId") or "")
    url = row.get("url") or f"https://www.tcgplayer.com/product/{product_id}"
    if "Language=English" not in url:
        joiner = "&" if "?" in url else "?"
        url = f"{url}{joiner}page=1&Language=English"
    return url


def number_sort_key(number: str) -> tuple:
    text = (number or "").upper()
    prefix_rank = 0
    if text.startswith("RC"):
        prefix_rank = 1
    elif text.startswith("TG"):
        prefix_rank = 2
    elif text.startswith("GG"):
        prefix_rank = 3
    elif text.startswith("SV"):
        prefix_rank = 4
    digits = re.search(r"(\d+)", text)
    return (prefix_rank, int(digits.group(1)) if digits else 10**9, text)


def catalog_row(row: dict, set_name: str, family_id: str, group_id: int, subset: str | None = None) -> dict:
    product_id = str(row["productId"])
    rarity = rarity_of(row)
    number = number_of(row)
    return {
        "productId": product_id,
        "name": row.get("name"),
        "setName": set_name,
        "familyId": family_id,
        "kind": "single",
        "rarity": rarity or None,
        "number": number or None,
        "subset": subset,
        "url": product_url(row),
        "imageUrl": row.get("imageUrl") or f"https://tcgplayer-cdn.tcgplayer.com/product/{product_id}_200w.jpg",
        "groupId": group_id,
    }
